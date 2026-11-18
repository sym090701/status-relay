# Status Relay

一个面向公网项目和家庭 NAS 的轻量状态中转服务。

它把“公网项目上报少量运行状态”和“家庭 NAS 保存状态”拆成两条机器链路：公网项目向 Relay 提交签名事件，NAS Agent 通过 Tailscale 主动拉取、落盘并确认。NAS 不需要开放公网入站端口，也不需要把 SMB 或管理接口暴露到互联网。

## 特性

- HMAC-SHA256 Webhook 签名；
- 每个上报密钥绑定一个项目，禁止跨项目上报；
- 时间戳和 Nonce 防重放；
- `event_id` 幂等，重复事件不会重复入队；
- SQLite 事件队列；
- 事件持久化、游标拉取和 ACK；
- Agent 断线后可在事件过期前继续拉取未确认事件；
- 仅使用 Python 标准库，无第三方运行时依赖；
- systemd 服务模板；
- 不包含设备命令下发，默认只传状态数据。

## 架构

```text
公网项目 --HMAC Webhook--> Relay (公网服务器/Tailscale)
                              |
                              | SQLite 队列
                              |
                    NAS Agent --主动拉取/ACK-->
                              |
                         NAS SQLite
```

生产部署时，Relay 绑定公网服务器的 Tailscale 地址。公网项目如果不在同一个 Tailnet，应通过 HTTPS 反向代理访问 Relay，或先把项目服务器加入 Tailnet。

## API

### `POST /v1/events`

公网项目提交事件。请求体必须是 JSON 对象：

```json
{
  "event_id": "uuid",
  "project": "xiaomiao",
  "source": "quota-server",
  "kind": "runtime_status",
  "payload": {
    "healthy": true
  }
}
```

签名头：

```text
X-Relay-Key-Id
X-Relay-Timestamp
X-Relay-Nonce
X-Relay-Signature: sha256=<hex>
```

签名内容为：

```text
timestamp + "\n" + nonce + "\n" + sha256(body)
```

其中 `\n` 表示换行符，`timestamp` 是 Unix 秒级时间戳，`sha256(body)` 是对原始请求体计算的十六进制摘要；再用对应密钥对上述内容计算 HMAC-SHA256。

### `GET /v1/events?cursor=0&limit=50`

NAS Agent 使用 Bearer 服务令牌拉取未确认事件。服务端返回事件和 `next_cursor`。

### `POST /v1/ack`

NAS Agent 提交已保存的 `event_id` 数组：

```json
{
  "event_ids": ["uuid-1", "uuid-2"]
}
```

### `GET /healthz`

返回服务状态和当前未确认事件数量。它不包含密钥或事件内容。

## 本地运行

```bash
cd status-relay
python3 -m unittest -v
python3 -m py_compile relay.py agent.py send_event.py test_relay.py
```

启动 Relay：

```bash
export RELAY_BIND=127.0.0.1
export RELAY_PORT=8780
export RELAY_DB="$PWD/data/relay.sqlite3"
export RELAY_INGEST_KEYS='demo=replace-with-a-long-random-secret'
export RELAY_INGEST_PROJECTS='demo=smoke-test'
export RELAY_AGENT_TOKENS='nas158=replace-with-a-long-random-token'
python3 relay.py
```

发送测试事件：

```bash
export RELAY_INGEST_URL='http://127.0.0.1:8780/v1/events'
export RELAY_KEY_ID='demo'
export RELAY_KEY_SECRET='replace-with-a-long-random-secret'
python3 send_event.py \
  --project smoke-test \
  --source local \
  --kind runtime_status \
  --payload '{}'
```

运行一次 Agent：

```bash
export RELAY_URL='http://127.0.0.1:8780'
export RELAY_AGENT_TOKEN='replace-with-a-long-random-token'
export AGENT_DATA_DIR="$PWD/data/nas-agent"
python3 agent.py --once
```

## systemd 部署

`deploy/` 中提供了：

- `status-relay.service.example`
- `status-relay-agent.service.example`
- `relay.env.example`
- `agent.env.example`

生产环境应当：

1. 为每个公网项目生成独立的 `key_id` 和 HMAC secret；`RELAY_INGEST_KEYS` 使用 `key_id=secret`，`RELAY_INGEST_PROJECTS` 使用 `key_id=project`，多个项分别用逗号分隔；每个 key 必须且只能对应一个项目；
2. 为 NAS Agent 使用独立 token，格式为 `agent_id=token`；
3. 将环境文件设置为仅服务用户可读；
4. 将 Relay 绑定在 Tailscale 地址或本机回环地址；
5. 用 Nginx、Caddy 或其他反向代理提供 HTTPS；
6. 备份 Relay 的 SQLite 数据库；
7. 把 NAS Agent 数据目录限制在指定子目录，不递归修改 NAS 父目录权限。

常用变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RELAY_BIND` / `RELAY_PORT` | `127.0.0.1` / `8780` | Relay 的监听地址和端口 |
| `RELAY_DB` | `./data/relay.sqlite3` | Relay 队列数据库 |
| `RELAY_INGEST_KEYS` | 无，必填 | Webhook 签名密钥映射 |
| `RELAY_INGEST_PROJECTS` | 无，必填 | 上报 key 到项目名的授权映射；所有 key 均需配置 |
| `RELAY_AGENT_TOKENS` | 无，必填 | Agent Bearer token 映射 |
| `RELAY_MAX_BODY_BYTES` | `65536` | 请求体大小上限，单位字节 |
| `RELAY_TIMESTAMP_SKEW` | `300` | 签名时间戳允许的偏差，单位秒 |
| `RELAY_EVENT_TTL` | `604800` | 事件投递有效期，默认 7 天 |
| `RELAY_MAX_EVENTS` | `100000` | Relay 数据库事件行数上限，包括已确认但尚未过期和未确认事件 |
| `RELAY_URL` | 无，必填 | Agent 连接的 Relay 基础地址 |
| `RELAY_AGENT_TOKEN` | 无，必填 | Agent 的 Bearer token |
| `AGENT_DATA_DIR` | `./data/nas-agent` | Agent 本地 SQLite 目录 |
| `AGENT_POLL_SECONDS` | `30` | Agent 轮询间隔，单位秒 |

部署后可检查：

```bash
systemctl status status-relay.service
systemctl status status-relay-agent.service
journalctl -u status-relay.service -n 50 --no-pager
journalctl -u status-relay-agent.service -n 50 --no-pager
```

143 的 Relay 和 158 的 Agent 分别运行在不同主机上，应在各自主机执行对应命令。SQLite 数据库应使用 SQLite 的备份机制或在停止写入后备份，运行时注意 WAL 文件的一致性。

**升级提示：**旧配置没有 `RELAY_INGEST_PROJECTS`。升级 Relay 前必须根据实际项目添加每个 key 的映射，否则服务会拒绝启动；项目不匹配的上报会返回 403。先备份代码和配置，再执行升级和验证。

## 当前版本限制

- NAS 端目前将所有项目的事件保存在同一个 `events.sqlite3` 中，按 `project` 字段区分；尚无历史记录或最新状态的远程查询接口。
- 事件默认有效期为 7 天。超过有效期的未确认事件不会再投递；如 NAS 可能离线更久，应调整 `RELAY_EVENT_TTL` 并监控队列。
- 当前 Agent token 共享同一个队列和 ACK 状态，**不是**多 NAS 独立消费或读取隔离。上报密钥绑定项目，但这不等于完整的多租户权限系统。
- Relay 自身提供 HTTP；Tailscale 内传输由 Tailnet 保护。若通过公网访问，必须另行配置 HTTPS 反向代理，且不应公开 Agent 拉取和 ACK 接口。
- `event_id` 重复时以首次入队记录为准；客户端重试应保留原 `event_id`，但使用新的 Nonce 和签名。`send_event.py` 每次执行都会生成新 `event_id`。
- 队列达到 `RELAY_MAX_EVENTS` 时，新事件返回 503，需要发送端保留原 `event_id` 并稍后重试；已存在的事件仍可幂等重试。上限按事件条数而非数据库字节数计算，不能替代磁盘配额与告警。已确认且过期的事件可在后续请求中清理；过期但未确认的事件被保留，需人工检查和处理，避免静默丢数据。
- Agent 每批最多拉取 100 条，遇到满批会立即拉取下一批；不足一批时按 `AGENT_POLL_SECONDS` 等待。默认轮询间隔为 30 秒，正常情况下会带来最多约一个轮询周期的额外延迟，网络和故障会增加延迟。
- 本项目尚未提供管理界面、自动备份、死信队列或独立的多消费者投递状态。

## 计划：按项目存储与远程查询

以下是待实现的需求，当前版本尚不支持：

- NAS 按项目分目录和 SQLite 文件保存历史事件，例如 `xiaomiao/events.sqlite3` 和 `cellnest/events.sqlite3`。已有数据按 `project` 迁移，迁移前备份并验证数据完整性。
- 提供历史记录查询，支持按项目、时间范围、来源和事件类型筛选，并分页返回结果。
- 提供最新状态查询：按项目及来源、事件类型获取最近的状态，同时保留历史记录。状态过期与离线的判断规则需要在实现时明确。
- NAS 提供只读查询服务，通过 Tailscale 供 143 服务器访问；其他公网项目可通过 143 上经过 HTTPS 和鉴权的入口查询。NAS 无需直接开放公网入站端口。
- 按项目配置独立的读写凭据和授权范围，限制跨项目访问；对项目名做安全校验，避免其被用作任意文件路径。中转队列现有的拉取和 ACK 接口不作为历史查询接口。
- 记录查询访问日志，保护凭据和敏感数据；远程请求只允许读取，不提供任意文件访问或设备控制。

建议的数据流：

```text
写入：公网项目 → 143 Relay → Tailscale → 158 NAS，按项目保存历史事件
读取：公网项目 → 143 查询入口 → Tailscale → 158 NAS 查询历史或最新状态
```

## 安全边界

状态事件不应包含密码、私钥、Bearer Token、任意文件路径、shell 命令或完整设备凭据。设备控制、OTA、拨号和配置修改属于另一类高风险操作，需要独立的授权、审计、幂等和确认流程。

交互式管理员登录不由本项目实现。需要管理页面时，可以在 Relay 前接入 Authentik、ZITADEL 或其他 OIDC 身份提供者。Webhook 使用 HMAC；mTLS 需要由外部代理提供，本项目本身没有内置 mTLS。

## 当前验证

已验证：

- 事件签名接收；
- 重复事件幂等；
- 重放事件拒绝；
- NAS 拉取、落盘和 ACK；
- Agent 停止期间事件进入队列；
- Agent 恢复后补拉并清空队列；
- Tailscale 私网链路；
- systemd 服务启动和重启。

运行时数据、环境文件、SQLite 数据库和密钥均不应提交到 Git。

## 小喵家外状态和预热（状态读取已部署）

`home_relay.py` 是独立的单项目服务，使用独立 SQLite 数据库和凭据，不接入 `/v1/events` 或 NAS Agent 的 ACK 队列。`home_agent.py` 在家庭侧主动连接该服务，读取 Ubuntu 额度服务并提交经过白名单筛选的状态；它只支持固定的 `preheat` 操作，不能执行 Shell、任意 URL 或文件操作。小喵在家庭 Wi-Fi 沿用原有局域网接口；SSID 只是网络选择条件，不能作为身份认证。

**部署快照（2026-11-18）：** 独立中继、受限 HTTPS 反代、家庭 Agent 和 Ubuntu guard 已部署；公网状态读取已验证。远程预热执行开关仍关闭：签名请求已在关闭执行的条件下完成链路测试，未执行真实预热。小喵固件尚未接入或实机验证。部署状况会变化，后续升级前应重新核对。

### 凭据与边界

- 小喵状态及命令结果读取：`HOME_READ_TOKEN`，Bearer token，只能读取本服务的小喵数据；不能领取命令或访问 NAS 队列。
- 小喵提交预热：独立的 `HOME_DEVICE_ID` 与 `HOME_DEVICE_SECRET`，HMAC-SHA256。服务启动时要求 secret 至少 32 个字符且与其他凭据不同；密钥泄漏可在服务配置中替换并重启，设备同步更新。读凭据与命令凭据独立轮换。
- 家庭 Agent：单独的 `HOME_AGENT_TOKEN`；该令牌能写状态、领取命令并提交执行结果，只放在可信家庭主机和 143，不交给小喵或公开代理。家庭 Agent 与本服务使用 HTTPS；仅在 Tailscale 等已确认受保护的私网中才可明确设置 `HOME_ALLOW_PRIVATE_HTTP=1`。
- 读取和 Agent token 用独立、随机、至少 32 个字符的值。凭据通过受限环境文件或密钥文件配置，文件只允许服务账户读取，不写进固件源码、仓库、请求日志或 URL。小喵端使用受保护的设备配置；设备固件提取仍可能泄密，必要时吊销并轮换。
- Ubuntu 的 guard 补丁增加 `PREHEAT_RELAY_TOKEN_FILE`，供家庭 Agent 使用独立 Bearer token；原有 `PREHEAT_TOKEN_FILE` 或来源 IP 规则继续有效。该独立令牌的预热请求必须携带 guard 头，状态读取也使用它。Agent 用 `HOME_QUOTA_TOKEN_FILE` 指向仅相关服务账户可读的令牌文件；不要关闭 Ubuntu 原有鉴权。

### 状态查询

`GET https://<域名>/v1/home/state`，请求头 `Authorization: Bearer <读取令牌>`。响应例子：

```json
{"freshness":"fresh","updated_at":1800000000,"age_seconds":8,"data":{"quota":{"ok":true,"stale":false,"server_updated_epoch":1800000000,"five_hour":{"remaining_percent":90,"used_percent":10,"reset_at_epoch":1800010000}},"mac":{"online":true},"preheat":{"phase":"idle","cooldown_active":false,"last_result":"completed"}}}
```

数据由家庭 Agent 从 `/usage`、`/health` 和鉴权后的 `/api/preheat/status` 中提取白名单字段；不会上传线程 ID、账号凭据、Wi-Fi 密码或完整配置。状态请求体最多 8192 字节；未收到过状态时 `freshness=offline` 且 `data=null`。收到后由 Relay 接收时间计算：不超过 120 秒为 `fresh`，121–600 秒为 `stale`，超过 600 秒为 `offline`；后两者可能仍返回最后一次数据，但必须标记过期，不能显示成实时状态。额度本身还有 `stale` 和 `server_updated_epoch`，应分别检查。Mac 离线不等于家庭 Agent 离线。暂不提供趋势历史接口。

### 预热提交和查询

`POST https://<域名>/v1/home/commands`，JSON 请求体必须严格包含以下三个字段：

```json
{"request_id":"b3d35d79-04c7-4d33-bf98-f45ff5e0b4e3","operation":"preheat","expires_at":1800000060}
```

`request_id` 为客户端生成的小写标准 UUID；`expires_at` 为 Unix 秒，首次提交时必须大于服务端当前时间且最多提前 300 秒。保留原始请求体及其过期时间，超时重试使用同一 ID 和同一请求体；新建用户操作才生成新 ID。签名请求头：

```text
X-Device-Id: <设备ID>
X-Timestamp: <Unix秒>
X-Nonce: <16–96位随机字母、数字、下划线或短横线>
X-Signature: <小写HMAC-SHA256十六进制>
Content-Type: application/json
```

签名消息为 `POST + "\n" + "/v1/home/commands" + "\n" + timestamp + "\n" + nonce + "\n" + sha256(body)`；`sha256(body)` 是实际发送的原始字节的小写十六进制摘要，HMAC 密钥为 `HOME_DEVICE_SECRET` 的 UTF-8 字节。不要改变请求体序列化后再发送；每次传输包括重试使用新的时间戳、Nonce 和签名，服务端允许时间偏差 60 秒并持久化 Nonce 防重放。设备需先安全同步时间并验证服务端 TLS 证书的主机名、有效期和可信 CA；使用硬编码证书时必须规划轮换，不能关闭验证或信任所有证书。

接收响应 `202`：`{"request_id":"...","status":"pending","reason":"accepted","expires_at":1800000060}`。同 ID、相同内容的幂等重试返回 200 和当前状态；同 ID 不同有效期返回 409。`401` 签名无效或超出时间窗，`409` 也可表示 Nonce 重放，`429` 表示已有待处理、执行中或待人工处理的不确定命令，或速率限制。客户端应退避，不自动生成新 ID 规避限制。每设备最多 5 次新命令/分钟，同一时间最多一个未完成命令。

`GET https://<域名>/v1/home/commands/<request_id>` 使用读取令牌查询，示例：

```json
{"request_id":"b3d35d79-04c7-4d33-bf98-f45ff5e0b4e3","status":"completed","reason":"turn_completed","expires_at":1800000060,"window":{"result":"reset_changed_unconfirmed","before_reset":1800000000,"after_reset":1800018000}}
```

状态含义：`accepted` 写入审计记录，提交响应中的 `pending` 表示等待家庭 Agent；`running` 表示 Agent 已领取，并不证明本地请求已发出；`completed` 表示 Ubuntu 报告 turn 完成及随后的额度刷新成功；`failed` 表示未执行或确定拒绝；`expired` 表示家庭 Agent 在有效期内未领取；`unknown` 表示无法确认是否执行。**接受不等于完成；turn 完成和 `reset_changed_unconfirmed` 都不证明新 5 小时窗口已经激活。** 小喵可每 5–10 秒轮询，设置有限超时，网络中断时保留 ID，恢复后查询。结果为 `unknown` 时不要自动重新提交；检查 Ubuntu 和 Relay 审计后，只有本机运维人员可用 `HOME_RELAY_DB=... python3 home_admin.py <request_id> --reason reviewed` 解除后续命令阻塞；原结果仍保留为 `unknown`。

家庭侧 SQLite journal 在调用前持久化执行标记，调用后若进程崩溃、响应超时或结果不明则不再自动调用。Relay 领取时先持久化 `running`；300 秒内没有结果会转为 `unknown`，不会重新领取。结果在家庭侧已持久化但上报丢失时，只重发结果。Relay `audit` 表保存无密钥的请求 ID、设备 ID、时间、状态变化、原因及可获得的窗口观察，不能仅凭它证明新的额度窗口。

### Ubuntu 原子 guard 与部署前置条件

现有 Ubuntu `Preheater.run()` 的手动模式没有在锁内检查活跃窗口；外部预检查无法消除与自动预热或其他请求的竞态。因此 `home_agent.py` 默认 `HOME_ENABLE_PREHEAT` 不开启，并要求 Ubuntu `/api/preheat/status` 返回 `remote_preheat_guard: 1`；未提供 guard 时即便显式开启也会拒绝调用。`deploy/quota-remote-guard.patch` 针对所检查的 Ubuntu 源码：通过 `X-Preheat-Guard: 1` 选择 `source=remote`，在现有预热锁内重新检查额度新鲜度和活跃窗口；手动局域网流程保持原样。已在目标服务上比对源码、备份并部署；以后升级前仍需重新核对版本、备份并验证旧接口，如源码不匹配不要强行应用。守卫与其他预热共享锁和已有成功/失败冷却；它无法阻止非预热的其他 Codex 会话在检查后独立创建窗口，所以仍不能承诺窗口状态不会变化。

实际启用之前，必须核实 Ubuntu 状态 token 的配置、授权 Agent 访问方式、服务身份和数据库权限。预热是实质性操作，会消耗额度；部署和模拟测试不自动调用真实预热。要进行一次真实验证须事先获得用户明确确认。

### 网络部署原则

建议新服务默认监听 143 的回环或 Tailscale 地址及单独端口 8781。公网 HTTPS 代理只允许 `GET /v1/home/state`、`POST /v1/home/commands` 和 `GET /v1/home/commands/<UUID>`；拒绝其他方法及所有 `/v1/home/agent/*`、现有 `/v1/events`、`/v1/ack` 和管理路径。家庭 Agent 通过 Tailscale 或单独受保护的内网路径访问 agent 接口，不能把 agent token 发给公网客户端。Nginx 使用独立限流区限制源 IP 的连接和请求、请求体不超过 8192 字节、合理读写超时，关闭敏感请求头和请求体日志；服务内的每设备速率限制不能替代代理层限流或磁盘监控。当前公网地址为 `https://statusrelay.hkz.241214.top`。该站点使用覆盖域名的通配符证书，公开链包含 Let's Encrypt YE2、ISRG Root YE 及 ISRG Root X2 交叉签名；可使用 [ISRG Root X1](certs/isrg-root-x1.pem) 作为信任锚，并核验主机名、有效期和完整链。该文件是公开根证书，不是私钥；需保留信任锚及证书链轮换方案。实际 TLS 客户端兼容性仍需在设备上验证。
