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
