"""自如 App API 客户端 —— DES-CBC 加解密 + 房源列表。

请求体和响应体都是 lowercase hex 的 DES-CBC 密文（8 字节分组，PKCS#7 填充）。
加解密密钥是 App 内硬编码的静态值，公开逆向仓库里已有：
    https://github.com/gadfly0x/signature_algorithm/blob/master/ziroom/app.py
首次运行会抓取并缓存到 ~/.config/ziroom/key.json（0600）。
也可用环境变量覆盖：ZIROOM_KEY_HEX / ZIROOM_IV_HEX（16 位 hex = 8 字节）。

网关不需要 token / 登录，匿名即可查询全城房源。
"""

import base64
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from Crypto.Cipher import DES
from Crypto.Util.Padding import pad, unpad

BASE = "https://ztoread.ziroom.com"
KEY_URL = (
    "https://raw.githubusercontent.com/gadfly0x/"
    "signature_algorithm/master/ziroom/app.py"
)
KEY_CACHE = Path.home() / ".config" / "ziroom" / "key.json"
UA = "ZiroomerProject/7.20.5 (iPhone; iOS 27.0.1; Scale/3.00)"

HEADERS = {
    "content-type": "application/json",
    "accept": "application/json;version=49",
    "user-agent": UA,
    "client-version": "7.20.5",
    "apptype": "1",
    "sys": "app",
    "ostype": "iOS",
    "osversion": "27.0.1",
    "phonename": "iPhone",
}


class ZiroomError(RuntimeError):
    pass


class TokenExpired(ZiroomError):
    """业务码 40005：token 失效，需要重新登录。"""


# ---------------------------------------------------------------- 密钥


def _fetch_key_from_source() -> tuple[bytes, bytes]:
    with urllib.request.urlopen(KEY_URL, timeout=20) as r:
        src = r.read().decode("utf-8")

    def grab(name: str) -> bytes:
        m = re.search(rf"{name}\s*=\s*b['\"]([^'\"]+)['\"]", src)
        if not m:
            raise ZiroomError(f"无法从源码中解析 {name}")
        return m.group(1).encode("latin-1")

    key, iv = grab("secret_key"), grab("iv")
    if len(key) != 8 or len(iv) != 8:
        raise ZiroomError(f"密钥长度异常 key={len(key)} iv={len(iv)}")
    return key, iv


TOKEN_FILE = Path.home() / ".config" / "ziroom" / "token.json"


def load_token() -> str:
    p = Path(TOKEN_FILE)
    if not p.exists():
        return ""
    try:
        return json.loads(p.read_text()).get("token") or ""
    except (OSError, ValueError):
        return ""


def save_token(token: str, phone: str = "") -> None:
    p = Path(TOKEN_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"token": token, "phone": phone, "saved_at": int(time.time())}
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    p.write_text(payload)
    p.chmod(0o600)


def jwt_payload(token: str) -> dict:
    """token 是 JWT（payload 里有 uid/exp）；解析失败返回空 dict。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, TypeError):
        return {}


def jwt_uid(token: str) -> str:
    return str(jwt_payload(token).get("uid") or "")


def load_key() -> tuple[bytes, bytes]:
    """返回 (key, iv)；优先环境变量，其次本地缓存，最后从公开源码抓取。"""
    env_key, env_iv = os.environ.get("ZIROOM_KEY_HEX"), os.environ.get("ZIROOM_IV_HEX")
    if env_key and env_iv:
        return bytes.fromhex(env_key), bytes.fromhex(env_iv)

    if KEY_CACHE.exists():
        d = json.loads(KEY_CACHE.read_text())
        return bytes.fromhex(d["key"]), bytes.fromhex(d["iv"])

    key, iv = _fetch_key_from_source()
    KEY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    KEY_CACHE.write_text(json.dumps({"key": key.hex(), "iv": iv.hex()}))
    KEY_CACHE.chmod(0o600)
    return key, iv


# ---------------------------------------------------------------- 加解密


class Ziroom:
    def __init__(self, base: str = BASE, timeout: int = 20, retries: int = 3):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.token = os.environ.get("ZIROOM_TOKEN") or load_token()
        self.key, self.iv = load_key()

    def encrypt(self, payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return DES.new(self.key, DES.MODE_CBC, self.iv).encrypt(pad(raw, 8)).hex()

    def decrypt(self, body: str) -> dict:
        try:
            raw = DES.new(self.key, DES.MODE_CBC, self.iv).decrypt(bytes.fromhex(body.strip()))
            return json.loads(unpad(raw, 8))
        except (ValueError, UnicodeDecodeError) as e:
            raise ZiroomError(f"解密失败：{e}") from e

    def headers(self) -> dict:
        h = dict(HEADERS)
        if self.token:
            h["token"] = self.token
            h["timestamp"] = str(int(time.time() * 1000))
        return h

    def call(self, path: str, payload: dict, allow_http_error: bool = False) -> dict:
        """发一次请求并解密，返回完整信封（不校验 code），用于探测新接口。

        allow_http_error=True 时 4xx 也尝试解出 body（登录类接口用 400 带业务错误）。
        """
        body = self.encrypt(payload).encode()
        url = f"{self.base}/{path.lstrip('/')}"
        last = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, data=body, headers=self.headers(), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    text = r.read().decode()
                break
            except urllib.error.HTTPError as e:
                if allow_http_error:
                    raw = e.read().decode(errors="replace").strip()
                    if raw:
                        try:
                            return self.decrypt(raw)
                        except ZiroomError:
                            pass
                    return {"code": str(e.code), "message": f"HTTP {e.code}"}
                # 网关对非法密文返回 500 且空 body；限流/网络问题重试
                last = e
                if e.code not in (429, 500, 502, 503, 504):
                    raise
            except (urllib.error.URLError, TimeoutError) as e:
                last = e
            if attempt + 1 < self.retries:
                time.sleep(1.5 * (attempt + 1))
        else:
            raise ZiroomError(f"{path} 请求失败：{last}")

        return self.decrypt(text)

    def post(self, path: str, payload: dict) -> dict:
        data = self.call(path, payload)
        code = str(data.get("code"))
        if code == "40005":
            raise TokenExpired(f"{path} token 失效（40005），需要重新登录")
        if code != "200":
            raise ZiroomError(f"{path} 业务错误 code={code} msg={data.get('message') or data.get('msg')}")
        return data.get("data") or {}

    # ------------------------------------------------------------ 业务接口

    def list_rooms(
        self,
        city_code: str = "110000",
        page: int = 1,
        size: int = 20,
        uid: str = "0",
        **extra,
    ) -> tuple[list[dict], int]:
        """城市房源列表，返回 (房源列表, total)。size 上限 20。"""
        payload = {
            "city_code": city_code,
            "page": page,
            "size": size,
            "uid": uid,
            "from_page": "search_list",
            "platformType": "APP",
            "appId": "com.ziroom.ZiroomProject",
            "product": "1",
            "scenes": "2",
            "channelType": "0",
            "navType": "0",
        }
        payload.update(extra)
        data = self.post("/ymerListApi/v4/room/list.json", payload)
        rooms = [r for r in data.get("rooms") or [] if isinstance(r, dict) and r.get("id")]
        return rooms, int(data.get("total") or 0)

    def resblock_rooms(self, resblock_id: str, city_code: str = "110000", **kw) -> tuple[list[dict], int]:
        """单个小区/楼栋的在租房源。"""
        return self.list_rooms(city_code=city_code, resblock_id=resblock_id, **kw)

    def map_regions(self, min_lng, min_lat, max_lng, max_lat,
                    center_lng=None, center_lat=None, city_code: str = "110000") -> dict:
        """地图视野内的楼栋聚合（含每栋在租套数、起价），用于发现小区。"""
        payload = {
            "city_code": city_code,
            "uid": "0",
            "agg_model": 1,
            "version_id": "mapB",
            "price": ",",
            "first_zoom": 1,
            "min_lng": min_lng,
            "max_lng": max_lng,
            "min_lat": min_lat,
            "max_lat": max_lat,
            "center_lng": center_lng if center_lng is not None else (min_lng + max_lng) / 2,
            "center_lat": center_lat if center_lat is not None else (min_lat + max_lat) / 2,
            "channelType": "0",
            "product": "1",
        }
        return self.post("/ymerApi/v1/map/room-search.json", payload)

    def buildings_near(self, lat: float, lng: float, radius_km: float,
                       city_code: str = "110000", cells: int = 3) -> list[dict]:
        """半径内的楼栋聚合，按直线距离升序，每项额外带 distance_km。"""
        dlat = radius_km / 111.0
        dlng = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.2))
        found: dict[str, dict] = {}
        step_x, step_y = 2 * dlng / cells, 2 * dlat / cells
        for i in range(cells):
            for j in range(cells):
                x0 = lng - dlng + i * step_x
                y0 = lat - dlat + j * step_y
                try:
                    data = self.map_regions(x0, y0, x0 + step_x, y0 + step_y, city_code=city_code)
                except ZiroomError:
                    continue
                for r in data.get("regions") or []:
                    code = r.get("building_code")
                    if code and r.get("lat") and code not in found:
                        r["distance_km"] = round(haversine(lat, lng, r["lat"], r["lng"]), 2)
                        found[code] = r
        return sorted(
            (r for r in found.values() if r["distance_km"] <= radius_km),
            key=lambda r: r["distance_km"],
        )

    def district_codes(self, bbox: tuple[float, float, float, float],
                       city_code: str = "110000", cells: int = 3) -> dict[str, str]:
        """网格扫描地图聚合，收集 区名 -> district_code（用于给列表接口传 district_code）。"""
        min_lng, min_lat, max_lng, max_lat = bbox
        found: dict[str, str] = {}
        step_x = (max_lng - min_lng) / cells
        step_y = (max_lat - min_lat) / cells
        for i in range(cells):
            for j in range(cells):
                try:
                    data = self.map_regions(
                        min_lng + i * step_x, min_lat + j * step_y,
                        min_lng + (i + 1) * step_x, min_lat + (j + 1) * step_y,
                        city_code=city_code,
                    )
                except ZiroomError:
                    continue
                for r in data.get("regions") or []:
                    code, name = r.get("district_code"), r.get("district_name")
                    if code and name:
                        found[name] = str(code)
        return found


# ---------------------------------------------------------------- 地理


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点直线距离（公里）。"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- 归一化


def normalize(room: dict) -> dict:
    """把接口里字段极多的房源对象压成监控关心的那几列。"""
    tags = [t.get("title") for t in room.get("tags") or [] if isinstance(t, dict) and t.get("title")]
    inv = room.get("inv_no")
    return {
        "id": str(room.get("id")),
        "name": room.get("name") or room.get("name_v2") or "",
        "price": room.get("price"),
        "area": room.get("area"),
        "house_type": room.get("house_type"),
        "bedroom": room.get("bedroom"),
        "floor": room.get("floor"),
        "floor_total": room.get("floor_total"),
        "district": room.get("district_name"),
        "bizcircle": room.get("bizcircle_name"),
        "resblock": room.get("resblock_name") or room.get("detail_name") or "",
        "sale_status": room.get("client_sale_status"),
        "sale_status_text": room.get("client_sale_status_text"),
        "code": room.get("code"),
        "inv_no": inv,
        "url": room.get("link") or (f"https://www.ziroom.com/x/{inv}.html" if inv else ""),
        "tags": tags,
    }
