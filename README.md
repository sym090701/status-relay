# Status Relay

一个面向公网项目和家庭 NAS 的轻量状态中转服务。

它把“公网项目上报少量运行状态”和“家庭 NAS 保存状态”拆成两条机器链路：公网项目向 Relay 提交签名事件，NAS Agent 通过 Tailscale 主动拉取、落盘并确认。NAS 不需要开放公网入站端口，也不需要把 SMB 或管理接口暴露到互联网。

## 特性

- HMAC-SHA256 Webhook 签名；
- 时间戳和 Nonce 防重放；
- `event_id` 幂等，重复事件不会重复入队；
- SQLite 事件队列；
- 至少一次投递、游标拉取和 ACK；
- Agent 断线后可继续拉取未确认事件；
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
timestamp + "\\n" + nonce + "\\n" + sha256(body)
```

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
export RELAY_AGENT_TOKENS='nas158=replace-with-a-long-random-token'
python3 relay.py
```

发送测试事件：

```bash
export RELAY_INGEST_URL='http://127.0.0.1:8780/v1/events'
export RELAY_KEY_ID='demo'
export RELAY_KEY_SECRET='replace-with-a-long-random-secret'
python3 send_event.py \
  --project smoke-test \\
  --source local \\
  --kind runtime_status \\
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

1. 为每个公网项目生成独立的 `key_id` 和 HMAC secret；
2. 为每台 NAS 使用独立 Agent token；
3. 将环境文件设置为仅服务用户可读；
4. 将 Relay 绑定在 Tailscale 地址或本机回环地址；
5. 用 Nginx、Caddy 或其他反向代理提供 HTTPS；
6. 备份 Relay 的 SQLite 数据库；
7. 把 NAS Agent 数据目录限制在指定子目录，不递归修改 NAS 父目录权限。

## 安全边界

状态事件不应包含密码、私钥、Bearer Token、任意文件路径、shell 命令或完整设备凭据。设备控制、OTA、拨号和配置修改属于另一类高风险操作，需要独立的授权、审计、幂等和确认流程。

交互式管理员登录不由本项目实现。需要管理页面时，可以在 Relay 前接入 Authentik、ZITADEL 或其他 OIDC 身份提供者；机器 Webhook 继续使用 HMAC 或 mTLS，不依赖浏览器登录。

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
