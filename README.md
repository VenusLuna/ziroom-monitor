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

## 定时任务（macOS）

```bash
./install_launchd.sh          # 每 2 小时一轮，RunAtLoad
launchctl kickstart -k gui/$(id -u)/com.ziroom.monitor   # 立刻跑一轮
launchctl bootout gui/$(id -u)/com.ziroom.monitor        # 停掉
```

日志：`logs/monitor.log`、`logs/monitor.err`。

## 登录（可选）

房源列表不需要 token。带 token 的请求会附 `token` 头，业务码 `40005` 时抛 `TokenExpired`。

```bash
.venv/bin/python monitor.py token   # 看已保存 token 的 uid 与有效期（JWT payload）
```

token 存在 `~/.config/ziroom/token.json`（0600）。短信验证码登录流程尚未实现——登录接口路径无法靠猜（网关按完整路径注册路由，不存在的子路径一律 404）。

## 脱敏说明

- 仓库内**没有**任何凭据：加解密密钥运行时抓取，飞书/自如 token 都在 `~/.config/ziroom/`（0600），`.gitignore` 覆盖。
- 无抓包文件、无数据库、无日志。
- 无手机号、open_id、app_secret、webhook 地址。

## 注意

绕开 App 的传输加密来拉数据，很可能违反自如的用户协议；本项目仅作个人学习与自用研究。请保持默认的 `--delay 0.4`，不要高频全量扫，避免给对方服务器造成压力。
