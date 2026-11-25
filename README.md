# ziroom-monitor

自如房源监控：匿名调用自如 App 网关，定时扫指定区域内的在租房源，对比 SQLite 历史快照，把「新上架 / 下架 / 调价 / 状态变化」推到飞书。

**仓库内不含任何密钥或个人信息**，见文末「脱敏说明」。

## 原理

自如 App 的业务请求体和响应体都是 **DES-CBC 密文**（8 字节分组、PKCS#7 填充），密钥硬编码在 App 内，公开逆向仓库已有。本项目的 `ziroom_api.py` 负责加解密，网关 `https://ztoread.ziroom.com` 对房源列表类是**匿名可用**的，不需要登录。

周边房源不是靠翻全城列表，而是先用地图聚合接口（`/ymerApi/v1/map/room-search.json`）枚举中心点周边的楼栋（带起价、经纬度），再按楼栋逐个拉房源（`/ymerListApi/v4/room/list.json` + `resblock_id`）——请求数少一个数量级，且能用楼栋起价提前剪枝。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install pycryptodome
```

首次运行会从公开仓库抓取加解密密钥并缓存到 `~/.config/ziroom/key.json`（0600）。也可用环境变量覆盖：`ZIROOM_KEY_HEX` / `ZIROOM_IV_HEX`（16 位 hex）。

## 用法

```bash
# 附近房源：默认中心=东畔科创中心，半径 4km（≈骑行 20 分钟），价格上限 3700
.venv/bin/python monitor.py once --near --max-price 3700 --db near.db

# 全城 / 指定区
.venv/bin/python monitor.py once --city 110000 --pages 5
.venv/bin/python monitor.py once --district 朝阳 --price 1500,3000 --pages 2

# 看库、看事件
.venv/bin/python monitor.py list --limit 20 --db near.db
.venv/bin/python monitor.py events --limit 50 --db near.db

# 生成区 code（首次；结果写入 districts.json）
.venv/bin/python monitor.py districts --city 110000
```

`watch` 子命令是常驻循环（`--interval 600`）；长期跑建议用 launchd，见下。

## 变化判定

| 类型 | 判定 |
|---|---|
| `new` | 首次出现 |
| `price` | 价格与上次快照不同 |
| `status` | 可签约状态文字变化 |
| `gone` | 连续 `--misses` 次扫描未出现（默认 3） |

> 列表接口的排序不稳定（同样条件两次请求 20 条会差 3 条左右），所以「下架」必须靠连续缺席计数，不能一次没见到就判死。

## 飞书推送

配置写在 `~/.config/ziroom/feishu.json`（0600），两种后端二选一：

```json
// 群自定义机器人（最省事，群里只有自己也可以）
{"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/…", "secret": "可选，开启签名校验才填"}

// 企业自建应用，私聊推送
{"app_id": "cli_…", "app_secret": "…", "mobile": "13800138000"}
```

自建应用需要：开启机器人能力、权限 `im:message:send_as_bot` + `contact:user.id:readonly`、发布版本并把用户加入可用范围。`open_id` 首次用手机号换取后自动缓存回配置文件。

```bash
.venv/bin/python monitor.py test    # 发一条自测消息
```

推送范围由 `--notify-kinds` 控制，默认 `new,gone`，可加 `price,status`。

## 飞书多维表格

把库内房源同步成一张可以直接排序、筛选的表（字段：房源ID / 小区 / 户型 / 几居 / 价格 / 面积㎡ / 距离km / 楼层 / 状态 / 链接 / 首次发现 / 最近更新）。

一次性建好：应用需要 `bitable:app` 权限，创建一个多维表格，把应用加成「可编辑」协作者，然后把 `app_token`（`/base/<app_token>`）、`table_id`（`?table=tbl…`）写进 `feishu.json`：

```json
{"app_id": "cli_…", "app_secret": "…", "open_id": "ou_…",
 "bitable_app_token": "bascn…", "bitable_table_id": "tbl…"}
```

```bash
.venv/bin/python monitor.py sheet            # 全量 upsert：新行插入，价格/距离变动才更新
.venv/bin/python monitor.py sheet --dry-run  # 只算差异不写入
.venv/bin/python monitor.py once --near --sheet   # 每次扫描后顺带同步
```

- 字段不存在的会自动创建（`ensure_sheet_fields`）；主字段会被改名为 `房源ID`，这是 upsert 的键，别手动改。
- 只有新增行、价格或距离变化才写，避免每 2 小时刷 419 行；用户自己加的列不会被覆盖。
- 下架房源不删行，状态列写成 `已下架`。
- 「标记」是单选列（候选 / 待看 / 排除），新行默认 **候选**；同步只补空值，**永不覆盖你手改的值**，所以放心在表里筛「待看」「排除」。

## 房源图

房源图来自详情接口 `/thorApi/v1/detail/media.json`（要 `id` + `house_code` + `inv_no` 三个字段，缺一个返回 500），按卧室/起居室/厨房等分组。图存两处：

```bash
.venv/bin/python monitor.py images --db near.db --image-limit 20   # 抓 20 套（每套 3 张）
.venv/bin/python monitor.py sheet  --db near.db --images           # 同步前先补图
.venv/bin/python monitor.py once --near --sheet --images           # 扫描 + 抓图 + 同步
```

- 本地：`images/<房源ID>/1.jpg`（CDN 上把 URL 的 `.webp` 换成 `.jpg` 就是同一张图的 JPEG，飞书预览兼容性更好）。
- 表格：附件列「房源图」，每套 3 张。上传走 `/open-apis/drive/v1/medias/upload_all`（`parent_type=bitable_image`），换回的 `file_token` 记在 SQLite `images` 表里复用，不重复上传。
- 增量：`images` 表里已有的房源不再抓；`--image-limit 500` 可一次补齐（并发由 `--workers` 控制）。
- 并发：扫楼、下图、传图都走线程池，`--workers 16` 时一轮扫描约 20 秒、400 套图约 3 分钟。

## 舍友信息

列表接口的 `brief.室友` 直接带每个卧室的性别和昵称，`normalize()` 会算出「几男几女」：

```
2男0女（01男、02男）     # 排除自己那间（按 code 匹配）
```

写进表里的「合租舍友」列，同时补「朝向」（`brief.朝向`）。老库第一次跑会写 NULL，下一轮扫描或 `once --near` 就会补齐。

## 定时任务（macOS）

```bash
./install_launchd.sh          # 每 2 小时一轮，RunAtLoad
launchctl kickstart -k gui/$(id -u)/com.ziroom.monitor   # 立刻跑一轮
launchctl bootout gui/$(id -u)/com.ziroom.monitor        # 停掉
```

日志：`logs/monitor.log`、`logs/monitor.err`。

## 部署到服务器（Linux）

4C8G 之类的云主机（Ubuntu/Debian/CentOS 都行）上：

```bash
sudo apt install -y python3-venv        # CentOS: dnf install python3
git clone <repo> ~/ziroomer && cd ~/ziroomer
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/ziroom && chmod 700 ~/.config/ziroom
# 把本机 ~/.config/ziroom/{key.json,feishu.json} 拷过去，权限 600
./install_systemd.sh                    # 用户级 timer，每 2 小时，不需 sudo
loginctl enable-linger "$USER"          # 让 timer 在未登录时也跑
```

- 资源占用：一轮约 120 秒、常驻内存 < 100MB、CPU 几乎为 0，1C1G 都够；图片缓存 `images/` 全量约 90MB（400 套 × 3 张 × 70KB）。
- 服务器上不需要 launchd 那套；`monitor.py login` 需要浏览器拖滑块，在服务器上不方便，建议在本地登录后把 `~/.config/ziroom/token.json` 拷过去（房源列表是匿名接口，token 过期不影响扫描）。
- 时区：日志与「首次发现/最近更新」用本机时区，服务器是 UTC 时先 `timedatectl set-timezone Asia/Shanghai`。

## 登录（可选）

房源列表不需要 token。带 token 的请求会附 `token` 头，业务码 `40005` 时抛 `TokenExpired`。

```bash
.venv/bin/python monitor.py token   # 看已保存 token 的 uid 与有效期（JWT payload）
```

token 存在 `~/.config/ziroom/token.json`（0600）。

### 短信验证码登录

```bash
.venv/bin/python monitor.py login --phone 13800138000
```

1. 终端会起一个本地页面（`http://127.0.0.1:<随机端口>/`）并自动打开浏览器；
2. 在页面里**拖动滑块**完成极验，页面下方会出一行 JSON，复制粘回终端；
3. 收到短信后输入 6 位验证码；
4. token 自动写进 `~/.config/ziroom/token.json`。

链路（全部走 ztoread 网关，请求体同样是 DES-CBC 密文）：

| 步骤 | 接口 | 产出 |
|---|---|---|
| 滑块校验 | `POST /vcenter/captcha/validate?type=geetest` | `cvcode` |
| 发短信 | `POST /vcenter/identity/vcode/get` | 短信（必须带 `cvcode`，否则 `40001 cvcode is expired or invalid`） |
| 校验短信 | `POST /vcenter//identity/validate?type=message`（双斜杠） | `ivcode` |
| 换 token | `POST /dp/dynamic/login/v6` | `data.resp.token` |

极验的 `captcha_id` 固定为 `9dba4221bf3a7e527a553e4971e98b4c`，滑块必须在浏览器里人工拖一次（脚本拿不到 `captcha_output`/`pass_token`）。

## 脱敏说明

- 仓库内**没有**任何凭据：加解密密钥运行时抓取，飞书/自如 token 都在 `~/.config/ziroom/`（0600），`.gitignore` 覆盖。
- 无抓包文件、无数据库、无日志。
- 无手机号、open_id、app_secret、webhook 地址。

## 注意

绕开 App 的传输加密来拉数据，很可能违反自如的用户协议；本项目仅作个人学习与自用研究。请保持默认的 `--delay 0.4`，不要高频全量扫，避免给对方服务器造成压力。
