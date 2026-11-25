"""自如房源监控：定时拉取列表，对比 SQLite 里的历史快照，输出变化事件。

用法:
    python monitor.py once  --city 110000 --pages 5          # 单次扫描并打印变化
    python monitor.py watch --city 110000 --pages 5 --interval 600
    python monitor.py list  --limit 20                        # 看库里现有房源
    python monitor.py events --limit 50                       # 看历史变化
    python monitor.py token                                   # 看 token 剩余有效期
    python monitor.py login --phone 13800138000               # 短信登录并保存 token

变化类型:
    new     新上架 / 首次出现
    price   价格变动（记录旧价 → 新价，涨跌都记）
    status  可签约状态变化（可签约 / 已租 / 配置中 …）
    gone    连续 --misses 次扫描未出现，判定下架或已租出

推送（二选一，配置写在 ~/.config/ziroom/feishu.json，0600）:
    群机器人  {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/...", "secret": "可空"}
    应用私聊  {"app_id": "cli_...", "app_secret": "...", "open_id": "ou_...",
              "mobile": "13800138000"}   # open_id 可空，首次用 mobile 换并缓存
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ziroom_api import (TokenExpired, Ziroom, ZiroomError, jwt_payload, load_token,
                        normalize, save_token)

DB_DEFAULT = "ziroom.db"
DISTRICTS_FILE = "districts.json"
# 东畔科创中心（北京市海淀区学院路街道 16 号院），OSM 反查坐标
DEFAULT_CENTER = (40.0018453, 116.3595008)
# 各区 code 靠地图聚合反查，这里给几个常用城市的扫描范围（min_lng,min_lat,max_lng,max_lat）
CITY_BBOX = {
    "110000": (115.42, 39.44, 117.51, 41.06),   # 北京
    "310000": (120.85, 30.68, 122.12, 31.88),   # 上海
    "440100": (112.95, 22.55, 114.05, 23.93),   # 广州
    "440300": (113.75, 22.40, 114.63, 22.87),   # 深圳
    "330100": (118.35, 29.18, 120.72, 30.57),   # 杭州
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    price        INTEGER,
    district     TEXT,
    bizcircle    TEXT,
    resblock     TEXT,
    area         TEXT,
    sale_status  INTEGER,
    sale_status_text TEXT,
    url          TEXT,
    first_seen   TEXT,
    last_seen    TEXT,
    miss_count   INTEGER DEFAULT 0,
    distance_km  REAL,
    active       INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events (
    ts      TEXT,
    kind    TEXT,
    room_id TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_rooms_active ON rooms(active);
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rooms)")}
    if "distance_km" not in cols:  # 老库平滑升级
        conn.execute("ALTER TABLE rooms ADD COLUMN distance_km REAL")
        conn.commit()
    return conn


def fetch_pages(client: Ziroom, city: str, pages: int, size: int, delay: float,
                extra: dict | None = None) -> tuple[list[dict], int]:
    seen, total = [], 0
    for page in range(1, pages + 1):
        rooms, total = client.list_rooms(city_code=city, page=page, size=size, **(extra or {}))
        if not rooms:
            break
        seen.extend(normalize(r) for r in rooms)
        if page < pages:
            time.sleep(delay)
    # 同一房源可能因排序漂移重复出现，按 id 去重
    uniq = {r["id"]: r for r in seen}
    return list(uniq.values()), total


def record(conn: sqlite3.Connection, rooms: list[dict], threshold: int) -> list[dict]:
    ts = now()
    events = []
    seen_ids = set()

    for room in rooms:
        rid = room["id"]
        seen_ids.add(rid)
        row = conn.execute("SELECT * FROM rooms WHERE id = ?", (rid,)).fetchone()

        if row is None:
            conn.execute(
                """INSERT INTO rooms (id,name,price,district,bizcircle,resblock,area,
                       sale_status,sale_status_text,url,first_seen,last_seen,miss_count,distance_km,active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,1)""",
                (rid, room["name"], room["price"], room["district"], room["bizcircle"],
                 room["resblock"], room["area"], room["sale_status"], room["sale_status_text"],
                 room["url"], ts, ts, room.get("distance_km")),
            )
            events.append({"kind": "new", "id": rid, "name": room["name"],
                           "price": room["price"], "district": room["district"],
                           "resblock": room["resblock"], "url": room["url"],
                           "distance_km": room.get("distance_km")})
            continue

        if room["price"] is not None and row["price"] is not None and room["price"] != row["price"]:
            delta = room["price"] - row["price"]
            events.append({"kind": "price", "id": rid, "name": room["name"],
                           "old": row["price"], "new": room["price"], "delta": delta,
                           "url": room["url"]})

        if room["sale_status"] != row["sale_status"]:
            events.append({"kind": "status", "id": rid, "name": room["name"],
                           "old": row["sale_status_text"], "new": room["sale_status_text"],
                           "old_code": row["sale_status"], "new_code": room["sale_status"],
                           "url": room["url"]})

        conn.execute(
            """UPDATE rooms SET name=?, price=?, district=?, bizcircle=?, resblock=?, area=?,
                   sale_status=?, sale_status_text=?, url=?, last_seen=?, miss_count=0, active=1,
                   distance_km=COALESCE(?, distance_km)
               WHERE id=?""",
            (room["name"], room["price"], room["district"], room["bizcircle"], room["resblock"],
             room["area"], room["sale_status"], room["sale_status_text"], room["url"], ts,
             room.get("distance_km"), rid),
        )

    # 未出现的房源：累计 miss，达到阈值判定下架
    stale = conn.execute(
        "SELECT id,name,price,miss_count,url FROM rooms WHERE active=1 AND last_seen < ?",
        (ts,),
    ).fetchall()
    for row in stale:
        if row["id"] in seen_ids:
            continue
        miss = row["miss_count"] + 1
        if miss >= threshold:
            conn.execute("UPDATE rooms SET miss_count=?, active=0 WHERE id=?", (miss, row["id"]))
            events.append({"kind": "gone", "id": row["id"], "name": row["name"],
                           "price": row["price"], "url": row["url"], "misses": miss})
        else:
            conn.execute("UPDATE rooms SET miss_count=? WHERE id=?", (miss, row["id"]))

    for e in events:
        conn.execute("INSERT INTO events (ts,kind,room_id,detail) VALUES (?,?,?,?)",
                     (ts, e["kind"], e["id"], json.dumps(e, ensure_ascii=False)))
    conn.commit()
    return events


WEBHOOK_FILE = Path.home() / ".config" / "ziroom" / "feishu.json"


FEISHU_API = "https://open.feishu.cn/open-apis"


def load_notify(args) -> dict:
    """合并推送配置。优先级：命令行 > 环境变量 > 配置文件。"""
    path = Path(args.webhook_file) if args.webhook_file else WEBHOOK_FILE
    cfg: dict = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            print(f"[warn] 配置文件 {path} 解析失败: {e}", file=sys.stderr)
    for key, val in (
        ("webhook", args.webhook or os.environ.get("ZIROOM_WEBHOOK")),
        ("secret", args.webhook_secret or os.environ.get("ZIROOM_WEBHOOK_SECRET")),
        ("app_id", args.feishu_app_id or os.environ.get("FEISHU_APP_ID")),
        ("app_secret", args.feishu_app_secret or os.environ.get("FEISHU_APP_SECRET")),
        ("open_id", args.feishu_open_id or os.environ.get("FEISHU_OPEN_ID")),
        ("mobile", args.feishu_mobile or os.environ.get("FEISHU_MOBILE")),
    ):
        if val:
            cfg[key] = val
    cfg["_file"] = str(path)
    return cfg


def save_notify(cfg: dict) -> None:
    """回写配置文件（含解析出的 open_id），权限 0600。"""
    path = Path(cfg["_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in cfg.items() if not k.startswith("_") and v}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def _feishu_post(url: str, payload: dict, token: str | None = None) -> dict:
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def _feishu_token(app_id: str, app_secret: str) -> str:
    resp = _feishu_post(f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
                        {"app_id": app_id, "app_secret": app_secret})
    if resp.get("code") or not resp.get("tenant_access_token"):
        raise RuntimeError(f"tenant_access_token 获取失败: {resp}")
    return resp["tenant_access_token"]


def _feishu_open_id(token: str, mobile: str) -> str:
    mobile = mobile.strip()
    if len(mobile) == 11 and mobile.isdigit():
        mobile = "+86" + mobile
    resp = _feishu_post(f"{FEISHU_API}/contact/v3/users/batch_get_id?user_id_type=open_id",
                        {"mobiles": [mobile]}, token)
    users = (resp.get("data") or {}).get("user_list") or []
    open_id = users[0].get("user_id") if users else None
    if not open_id:
        raise RuntimeError(f"手机号换 open_id 失败（需要 contact:user.id:readonly 权限，"
                           f"且你在应用可见范围内）: {resp}")
    return open_id


def send_feishu_dm(cfg: dict, text: str) -> str:
    """用企业自建应用给指定用户发私聊消息，返回 open_id。"""
    token = _feishu_token(cfg["app_id"], cfg["app_secret"])
    open_id = cfg.get("open_id")
    if not open_id:
        if not cfg.get("mobile"):
            raise RuntimeError("配置里需要 open_id 或 mobile")
        open_id = _feishu_open_id(token, cfg["mobile"])
        cfg["open_id"] = open_id
        save_notify(cfg)
    resp = _feishu_post(
        f"{FEISHU_API}/im/v1/messages?receive_id_type=open_id",
        {"receive_id": open_id, "msg_type": "text",
         "content": json.dumps({"text": text}, ensure_ascii=False)},
        token,
    )
    if resp.get("code"):
        raise RuntimeError(f"发私聊失败: {resp}")
    return open_id


def _feishu_sign(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode(), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def build_message(events: list[dict], max_items: int = 30) -> str:
    new = [e for e in events if e["kind"] == "new"]
    gone = [e for e in events if e["kind"] == "gone"]
    price = [e for e in events if e["kind"] == "price"]
    status = [e for e in events if e["kind"] == "status"]
    head = f"🏠 自如监控 {now()}  新增 {len(new)} · 下架 {len(gone)}"
    if price:
        head += f" · 调价 {len(price)}"
    if status:
        head += f" · 状态 {len(status)}"

    lines, shown = [head], 0
    for e in new:
        if shown >= max_items:
            break
        dist = f" · {e['distance_km']:.2f}km" if e.get("distance_km") is not None else ""
        lines.append(f"🆕 {e['name']} ¥{e['price']}/月 {e['district']} {e['resblock']}{dist}\n{e['url']}")
        shown += 1
    for e in price:
        if shown >= max_items:
            break
        arrow = "↓" if e["delta"] < 0 else "↑"
        lines.append(f"{arrow} {e['name']} ¥{e['old']} → ¥{e['new']}\n{e['url']}")
        shown += 1
    if status:
        for e in status:
            if shown >= max_items:
                break
            lines.append(f"⚪ {e['name']} {e['old']} → {e['new']}\n{e['url']}")
            shown += 1
    for e in gone:
        if shown >= max_items:
            break
        lines.append(f"❌ 下架 {e['name']} ¥{e['price']}\n{e['url']}")
        shown += 1
    rest = len(events) - shown
    if rest > 0:
        lines.append(f"…另有 {rest} 条变化，见 ziroom.db")
    return "\n\n".join(lines)


def send_webhook(url: str, secret: str | None, text: str) -> None:
    if "feishu.cn" in url or "larksuite.com" in url:
        payload = {"msg_type": "text", "content": {"text": text}}
        if secret:
            ts = int(time.time())
            payload["timestamp"] = str(ts)
            payload["sign"] = _feishu_sign(secret, ts)
    else:
        payload = {"text": text}
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read() or b"{}")
    code = resp.get("code", resp.get("StatusCode", 0))
    if code not in (0, None):
        print(f"[warn] webhook 返回异常: {resp}", file=sys.stderr)
    elif resp.get("msg") and resp.get("msg") != "success":
        print(f"[warn] webhook 返回: {resp}", file=sys.stderr)


def notify(cfg: dict | None, events: list[dict]) -> None:
    """按配置选后端：有 webhook 走群机器人，否则用自建应用发私聊。"""
    if not cfg or not events:
        return
    text = build_message(events)
    try:
        if cfg.get("webhook"):
            send_webhook(cfg["webhook"], cfg.get("secret"), text)
        elif cfg.get("app_id") and cfg.get("app_secret"):
            open_id = send_feishu_dm(cfg, text)
            print(f"  已推送飞书私聊 → {open_id}")
        else:
            print("[warn] 未配置推送：feishu.json 需要 webhook 或 app_id/app_secret",
                  file=sys.stderr)
    except Exception as e:  # 通知失败不应该中断监控
        print(f"[warn] 推送失败: {e}", file=sys.stderr)


def print_events(events: list[dict]) -> None:
    if not events:
        print("  无变化")
        return
    for e in events:
        if e["kind"] == "new":
            dist = f" {e['distance_km']:.2f}km" if e.get("distance_km") is not None else ""
            print(f"  🆕 {e['name']}  ¥{e['price']}{dist}  {e['district']}/{e['resblock']}  {e['url']}")
        elif e["kind"] == "price":
            print(f"  {'↓' if e['delta'] < 0 else '↑'} {e['name']}  ¥{e['old']} → ¥{e['new']}  {e['url']}")
        elif e["kind"] == "status":
            print(f"  ⚪ {e['name']}  {e['old']} → {e['new']}  {e['url']}")
        elif e["kind"] == "gone":
            print(f"  ❌ 下架 {e['name']}  ¥{e['price']}  {e['url']}")


def resolve_filters(args) -> dict:
    """把 --district / --price 翻译成接口参数。"""
    extra = {}
    if args.price:
        extra["price"] = args.price
    if args.district:
        table = {}
        p = Path(DISTRICTS_FILE)
        if p.exists():
            table = json.loads(p.read_text()).get(args.city, {})
        code = table.get(args.district)
        if not code:
            raise SystemExit(
                f"没有「{args.district}」的 district_code，先跑："
                f"python monitor.py districts --city {args.city}"
            )
        extra["district_code"] = code
    return extra


def cmd_districts(args):
    client = Ziroom()
    bbox = tuple(float(x) for x in args.bbox.split(",")) if args.bbox else CITY_BBOX.get(args.city)
    if not bbox:
        raise SystemExit("未知城市，请用 --bbox min_lng,min_lat,max_lng,max_lat 指定范围")
    found = client.district_codes(bbox, city_code=args.city, cells=args.cells)
    p = Path(DISTRICTS_FILE)
    table = json.loads(p.read_text()) if p.exists() else {}
    table.setdefault(args.city, {}).update(found)
    p.write_text(json.dumps(table, ensure_ascii=False, indent=2, sort_keys=True))
    for name, code in sorted(found.items()):
        print(f"  {name:<6} {code}")
    print(f"\n已写入 {DISTRICTS_FILE}（{len(found)} 个区，city={args.city}）")


def parse_center(args) -> tuple[float, float]:
    if args.center:
        try:
            lat, lng = (float(x) for x in args.center.split(","))
        except ValueError:
            raise SystemExit("--center 需要 \"lat,lng\" 格式")
        return lat, lng
    return DEFAULT_CENTER


def fetch_nearby(client: Ziroom, args) -> tuple[list[dict], str]:
    """半径内所有楼栋逐一拉房源（只查起价不超预算的楼栋）。"""
    lat, lng = parse_center(args)
    blds = client.buildings_near(lat, lng, args.radius, city_code=args.city)
    rooms: dict[str, dict] = {}
    queried = 0
    for b in blds:
        min_price = b.get("min_price")
        if args.max_price and min_price and min_price > args.max_price:
            continue
        try:
            page = 1
            while page <= 5:
                batch, _ = client.resblock_rooms(b["building_code"], city_code=args.city,
                                                 page=page, size=args.size)
                for r in batch:
                    n = normalize(r)
                    if args.max_price and (n["price"] or 0) > args.max_price:
                        continue
                    n["distance_km"] = b["distance_km"]
                    n["resblock"] = n["resblock"] or b.get("building_name") or ""
                    rooms[n["id"]] = n
                if len(batch) < args.size:
                    break
                page += 1
                time.sleep(args.delay)
        except ZiroomError as e:
            print(f"[warn] {b.get('building_name')} 拉取失败: {e}", file=sys.stderr)
            continue
        queried += 1
        time.sleep(args.delay)
    note = f"{len(blds)} 栋楼（{queried} 栋有在租且在预算内）"
    return list(rooms.values()), note


def sweep(args) -> list[dict]:
    client = Ziroom()
    t0 = time.time()
    if args.near:
        rooms, note = fetch_nearby(client, args)
        total = note
    else:
        rooms, total = fetch_pages(client, args.city, args.pages, args.size, args.delay,
                                   resolve_filters(args))
    conn = connect(args.db)
    events = record(conn, rooms, args.misses)
    active = conn.execute("SELECT COUNT(*) FROM rooms WHERE active=1").fetchone()[0]
    conn.close()
    print(f"[{now()}] 拉取 {len(rooms)} 条（{total}），库内在租 {active} 条，"
          f"耗时 {time.time() - t0:.1f}s")
    print_events(events)
    kinds = {k.strip() for k in (args.notify_kinds or "").split(",") if k.strip()}
    to_send = [e for e in events if not kinds or e["kind"] in kinds]
    notify(load_notify(args), to_send)
    return events


def cmd_token(args):
    tok = load_token()
    if not tok:
        print("还没有保存 token（~/.config/ziroom/token.json）")
        return
    info = jwt_payload(tok)
    uid = info.get("uid", "?")
    exp = info.get("exp")
    print(f"token {tok[:12]}…{tok[-6:]}  uid={uid}")
    if exp:
        left = exp - int(time.time())
        when = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M")
        print(f"过期时间 {when}（{left // 86400} 天 {left % 86400 // 3600} 小时后）"
              if left > 0 else f"已于 {when} 过期")
    else:
        print("payload 里没有 exp 字段")


# ---------------------------------------------------------------- 短信登录
# 登录链路（抓包解密所得，全部走 ztoread 网关）:
#   1. /vcenter/captcha/validate?type=geetest  极验滑块 → cvcode
#   2. /vcenter/identity/vcode/get             发短信（必须带 cvcode）
#   3. /vcenter//identity/validate?type=message  校验短信 → ivcode（注意双斜杠）
#   4. /dp/dynamic/login/v6                    换 token
CAPTCHA_ID = "9dba4221bf3a7e527a553e4971e98b4c"
LOGIN_BASE = {
    "scenario": "login", "uid": "0", "scene": "login_phone",
    "module": "ziroom_app_ios", "bizCode": "rent_o2o",
    "appId": "ziruke", "captchaID": CAPTCHA_ID,
}

CAPTCHA_HTML = """<!doctype html>
<meta charset="utf-8">
<title>自如登录验证</title>
<style>body{font:14px -apple-system,BlinkMacSystemFont,sans-serif;margin:24px;max-width:520px}
textarea{width:100%;height:110px;font:12px ui-monospace,monospace}</style>
<h3>拖动滑块完成验证</h3>
<div id="box"></div>
<p>验证通过后，把下面这一行整段复制回终端：</p>
<textarea id="out" readonly></textarea>
<script src="https://static.geetest.com/v4/gt4.js"></script>
<script>
initGeetest4({captchaId: "__CAPTCHA_ID__", product: "bind"}, function (g) {
  g.onReady(function () {
    g.appendTo("#box");
    if (g.showCaptcha) g.showCaptcha();
  });
  g.onSuccess(function () {
    document.getElementById("out").value = JSON.stringify(g.getValidate());
  });
  g.onError(function (e) {
    document.getElementById("out").value = "ERROR " + JSON.stringify(e);
  });
});
</script>
""".replace("__CAPTCHA_ID__", CAPTCHA_ID)


class _CaptchaServer:
    """本地起一个最小的 HTTP 服务。

    极验 SDK 会用 window.location.protocol 拼 gcaptcha4 的地址，file:// 下变成
    file://gcaptcha4.geetest.com/load… 直接失败，所以必须给个 http 源。
    """

    def __init__(self, html: str):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        body = html.encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _read_captcha(arg: str | None) -> dict:
    if arg:
        raw = arg
    else:
        srv = _CaptchaServer(CAPTCHA_HTML)
        try:
            subprocess.run(["open", srv.url], check=False)
            print(f"已打开验证页 {srv.url}")
            print("拖动滑块 → 复制框里的 JSON → 粘贴到下面回车")
            raw = input("captcha> ").strip()
        finally:
            srv.close()
    try:
        v = json.loads(raw)
    except ValueError as e:
        raise SystemExit(f"不是合法 JSON（{e}）：{raw[:80]}")
    return {
        "lotNumber": v.get("lot_number") or v.get("lotNumber", ""),
        "captchaOutput": v.get("captcha_output") or v.get("captchaOutput", ""),
        "passToken": v.get("pass_token") or v.get("passToken", ""),
        "genTime": str(v.get("gen_time") or v.get("genTime", "")),
    }


def cmd_login(args):
    phone = (args.phone or input("手机号> ")).strip()
    if not (len(phone) == 11 and phone.isdigit()):
        raise SystemExit("手机号要 11 位数字")
    z = Ziroom()

    cap = _read_captcha(args.captcha)
    r = z.call("/vcenter/captcha/validate?type=geetest", {**LOGIN_BASE, **cap},
               allow_http_error=True)
    data = (r or {}).get("data") or {}
    cvcode = data.get("cvcode") or ""
    if data.get("status") != "success" or not cvcode:
        raise SystemExit(f"滑块校验未通过：{data.get('reason') or json.dumps(r, ensure_ascii=False)[:200]}")
    print("✔ 滑块校验通过")

    r = z.call("/vcenter/identity/vcode/get",
               {**LOGIN_BASE, "phone": phone, "cvcode": cvcode})
    if "data" not in (r or {}):
        raise SystemExit(f"发验证码失败：{json.dumps(r, ensure_ascii=False)[:200]}")
    print(f"✔ 验证码已发送到 {phone[:3]}****{phone[-4:]}")
    code = (args.vcode or input("短信验证码> ")).strip()
    if not (len(code) == 6 and code.isdigit()):
        raise SystemExit("验证码要 6 位")

    r = z.call("/vcenter//identity/validate?type=message",
               {**LOGIN_BASE, "phone": phone, "vcode": code})
    ivcode = ((r or {}).get("data") or {}).get("ivcode") or ""
    if not ivcode:
        raise SystemExit(f"验证码校验失败：{json.dumps((r or {}).get('data'), ensure_ascii=False)}")
    print("✔ 短信校验通过")

    r = z.call("/dp/dynamic/login/v6",
               {"ivcode": ivcode, "phone": phone, "needDefaultPassword": 1, "uid": "0"},
               allow_http_error=True)
    tok = ((((r or {}).get("data") or {}).get("resp")) or {}).get("token") or ""
    if not tok:
        raise SystemExit(f"登录失败：{json.dumps(r, ensure_ascii=False)[:300]}")
    save_token(tok, phone)
    info = jwt_payload(tok)
    exp = info.get("exp")
    print(f"✔ 登录成功 uid={info.get('uid', '?')} token 已存 ~/.config/ziroom/token.json")
    if exp:
        print(f"  过期时间 {datetime.fromtimestamp(exp):%Y-%m-%d %H:%M}")


def cmd_test(args):
    cfg = load_notify(args)
    if not cfg.get("webhook") and not (cfg.get("app_id") and cfg.get("app_secret")):
        raise SystemExit("没有配置推送：写 ~/.config/ziroom/feishu.json "
                         "（webhook，或 app_id/app_secret + mobile）")
    sample = [{"kind": "new", "id": "00000000", "name": "自测房源（不是真实数据）",
               "price": 3000, "district": "海淀", "resblock": "测试小区",
               "distance_km": 1.23, "url": "https://www.ziroom.com/"}]
    notify(cfg, sample)
    print(f"测试消息已发出（配置 {cfg['_file']}）；没收到看上面的 [warn]")


def cmd_once(args):
    sweep(args)


def cmd_watch(args):
    print(f"开始监控 city={args.city} pages={args.pages} 间隔={args.interval}s "
          f"（Ctrl-C 退出，DB={args.db}）")
    try:
        while True:
            try:
                sweep(args)
            except TokenExpired:
                print(f"[{now()}] token 失效（40005），需要重新登录", file=sys.stderr)
            except ZiroomError as e:
                print(f"[{now()}] 扫描失败: {e}", file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已停止")


def cmd_list(args):
    conn = connect(args.db)
    rows = conn.execute(
        "SELECT * FROM rooms WHERE active=1 ORDER BY distance_km IS NULL, distance_km, price LIMIT ?",
        (args.limit,),
    ).fetchall()
    if not rows:
        print("库为空，先跑一次 once")
        return
    for r in rows:
        dist = f"{r['distance_km']:.2f}km" if r["distance_km"] is not None else "   -  "
        print(f"{r['id']:<10} ¥{r['price']:<6} {dist:>7}  {r['district'] or '':<4} "
              f"{(r['resblock'] or r['name'])[:22]:<24} {r['sale_status_text'] or ''}  {r['url']}")
    print(f"\n共 {len(rows)} 条（active）")


def cmd_events(args):
    conn = connect(args.db)
    rows = conn.execute("SELECT * FROM events ORDER BY rowid DESC LIMIT ?", (args.limit,)).fetchall()
    if not rows:
        print("暂无事件")
        return
    for r in rows:
        d = json.loads(r["detail"])
        d.pop("kind", None)
        print(f"{r['ts']}  {r['kind']:<6} {json.dumps(d, ensure_ascii=False)[:160]}")


def main():
    # 子命令和全局参数都可以写在任意位置：主解析器给默认值，
    # 子解析器用 SUPPRESS，避免把主解析器读到的值覆盖回默认值。

    def add_options(parser, sub: bool):
        d = lambda v: v if not sub else argparse.SUPPRESS  # noqa: E731
        parser.add_argument("--db", default=d(DB_DEFAULT))
        parser.add_argument("--city", default=d("110000"),
                            help="城市 code，北京 110000 / 上海 310000 …")
        parser.add_argument("--pages", type=int, default=d(5), help="每次扫描的页数（每页 20 条）")
        parser.add_argument("--size", type=int, default=d(20), help="每页条数，接口上限 20")
        parser.add_argument("--delay", type=float, default=d(0.4), help="翻页间隔秒数")
        parser.add_argument("--misses", type=int, default=d(3), help="连续多少次未出现判定下架")
        parser.add_argument("--interval", type=int, default=d(600), help="watch 模式扫描间隔秒")
        parser.add_argument("--webhook", default=d(None), help="事件推送 URL（飞书/钉钉/自定义）")
        parser.add_argument("--webhook-secret", default=d(None), help="飞书机器人「签名校验」密钥")
        parser.add_argument("--webhook-file", default=d(None),
                            help="推送配置文件，默认 ~/.config/ziroom/feishu.json")
        parser.add_argument("--feishu-app-id", default=d(None), help="自建应用 app_id（私聊推送）")
        parser.add_argument("--feishu-app-secret", default=d(None), help="自建应用 app_secret")
        parser.add_argument("--feishu-open-id", default=d(None), help="接收人 open_id（ou_...）")
        parser.add_argument("--feishu-mobile", default=d(None), help="接收人手机号（换 open_id 用）")
        parser.add_argument("--notify-kinds", default=d("new,gone"),
                            help="推送哪几类事件，默认 new,gone；加 price,status 可多推")
        parser.add_argument("--limit", type=int, default=d(20))
        parser.add_argument("--district", default=d(None), help="区名，如 朝阳（需先跑 districts 生成 code）")
        parser.add_argument("--price", default=d(None), help="价格区间，如 1500,3000")
        parser.add_argument("--bbox", default=d(None), help="districts 用：min_lng,min_lat,max_lng,max_lat")
        parser.add_argument("--cells", type=int, default=d(3), help="districts 用：网格边长")
        parser.add_argument("--near", action="store_true", default=d(False),
                            help="按半径扫楼（默认中心=东畔科创中心）")
        parser.add_argument("--center", default=d(None), help="near 用：\"lat,lng\"")
        parser.add_argument("--radius", type=float, default=d(4.0),
                            help="near 用：直线半径 km（自行车 20min ≈ 4km）")
        parser.add_argument("--max-price", type=int, default=d(None), help="near 用：价格上限")
        parser.add_argument("--phone", default=d(None), help="login 用：手机号")
        parser.add_argument("--vcode", default=d(None), help="login 用：短信验证码（不填则交互输入）")
        parser.add_argument("--captcha", default=d(None),
                            help="login 用：滑块 JSON（不填则打开验证页交互）")

    p = argparse.ArgumentParser(description="自如房源监控")
    add_options(p, sub=False)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, func in (("once", cmd_once), ("watch", cmd_watch),
                       ("list", cmd_list), ("events", cmd_events),
                       ("districts", cmd_districts), ("test", cmd_test),
                       ("token", cmd_token), ("login", cmd_login)):
        sp = sub.add_parser(name)
        add_options(sp, sub=True)
        sp.set_defaults(func=func)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
