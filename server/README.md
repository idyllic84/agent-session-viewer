# 云端 Web Server

接收本地 Web Server 上传的分析数据，提供密码保护的查看页与控制台（上传 API Key、
密码）。云端从不向本地电脑下发任务。主说明见 [根目录 README](../README.md)，
设计见 [docs/two-web-servers-design.md](../docs/two-web-servers-design.md)。

## 部署

**[Docker 部署文档](deploy/README.md)**：本机直连（`127.0.0.1:41880`）与 VM + Cloudflare
Tunnel 两套 Compose。构建上下文为仓库根目录。

```bash
docker compose -f server/deploy/docker-compose.local.yml up -d --build --wait
```

## 不用 Docker 的本机启动（开发）

```bash
python -m pip install -r server/requirements.txt
python -m server.manage init-session-key --directory server/state
python -m server.manage set-password --database server/state/cloud.db
python -m server --database server/state/cloud.db --session-key-file server/state/session.key \
  --public-origin http://127.0.0.1:8080 --allow-http-loopback --development
```

公网必须使用 HTTPS public origin（经可信反向代理/Tunnel 到 Waitress）。

## 鉴权

| 对象 | 凭据 | 能做什么 |
|---|---|---|
| 浏览器（Owner） | 密码 → 8 小时 Cookie 会话（HttpOnly、SameSite=Strict、HTTPS 下 Secure） | 查看、检索、管理 Key、改密码 |
| 本地 Web Server | 上传 API Key（`Authorization: Bearer asv_<id>_<secret>`） | 仅 `/ingest/v1/*`：查询/绑定/上传自己的数据集 |

- 密码以 scrypt 加盐哈希保存；修改/重置密码会使所有已有会话失效。登录有限流与表单 nonce。
- 写操作校验 Origin、`Sec-Fetch-Site` 和 CSRF token。
- API Key 只显示一次，库中只存 SHA-256；可设置过期、轮换（保留上传进度）、撤销。
- API Key 不能登录控制台，浏览器 Cookie 不能调用 ingest。
- 每个 Key 是一个“客户端槽位”，首次同步绑定到一台电脑的本地数据集；同一 Key 不能
  被另一台电脑复用（`DATASET_MISMATCH`），除非 Owner 在控制台显式“Allow a new computer”。

## 如何区分不同客户端

云端用两层身份区分电脑：**上传 API Key（客户端槽位）** 和 **本地数据集 ID**。

### 1. API Key = 客户端槽位

控制台生成 Key 时创建一条 `cloud_clients` 记录：

| 字段 | 说明 |
|---|---|
| `clientId` | 云端内部 ID，轮换 Key 时不变 |
| 名称 | 例如 “work laptop”，会话列表的 `uploadedBy` 显示它 |
| Key ID + 秘密哈希 | Key 形如 `asv_<keyId>_<secret>`，只存 secret 的 SHA-256 |
| `dataset_id` | 首次同步时绑定的本地数据集 |
| `ack_seq` | 该客户端已被云端确认的变更序列 |

每个 ingest 请求都带 `Authorization: Bearer <Key>`。云端按 Key ID 找到记录、
常量时间比对秘密，再检查启用/过期；请求体中的任何字段都不能冒充其他客户端。

### 2. 数据集 ID = 某台电脑的本地库

- 每个本地库首次创建时随机生成 32 位十六进制 `datasetId`（存于 `local_settings`）。
- 第一次同步时，Key 与该 `datasetId` 绑定（`POST /ingest/v1/bind`）。
- 之后该 Key 只接受这个数据集；另一台电脑用同一 Key 会得到 `DATASET_MISMATCH`。
- 一个数据集也不能同时绑定两把 Key。
- 只有 Owner 在控制台点 **Allow a new computer**（解绑）后，该 Key 才能换绑新数据集；
  已上传的数据保留。

### 3. 数据按数据集隔离

- 云端会话 ID 为 `<datasetId>/<source>:<原会话ID>`，消息 ID 同样加前缀；两台电脑上的
  同名会话不会互相覆盖。
- `cloud_sessions` 记录每个会话属于哪个客户端；其他 Key 写同一会话会被拒绝
  （`REPLICA_CONFLICT`）。
- 每个客户端有独立的 `ack_seq`，上传必须满足 `expectedSeq == ack_seq`，各电脑进度互不影响。

### 常见操作的影响

| 操作 | 结果 |
|---|---|
| 轮换 Key | 旧 Key 立即失效；同一客户端，`ack_seq` 与数据保留，本地换新 Key 后从原位置继续 |
| 撤销 Key | 该电脑无法再上传；已上传数据保留 |
| 增加一台电脑 | 为它生成新 Key |
| 重装后新建本地库 | `datasetId` 变了：用新 Key，或在控制台对旧 Key 执行 Allow a new computer |

## Ingest 协议（v6）

```text
GET  /ingest/v1/status?datasetId=…   → 实例、内容策略、请求上限、绑定数据集、已确认 ackSeq
POST /ingest/v1/bind                  → 首次绑定数据集
POST /ingest/v1/batches               → 一个普通批量 POST（Idempotency-Key 头）
GET  /ingest/v1/receipts/{key}        → 查询回执
```

批次体：`protocolVersion`、`datasetId`、`expectedSeq`、`throughSeq`、`groups[]`。每个组
是本地一次提交，包含连续的 `session` / `message` / `delete_message` 变更。云端：

1. 校验 Key、数据集、版本、连续序列、完整组和严格的内容形状；
2. 同 Key 同内容返回原回执，同 Key 不同内容拒绝；
3. `expectedSeq` 必须等于已确认位置（否则 `SEQ_CONFLICT`）；
4. 在**一个事务**中应用会话/消息 upsert、删除、检索索引、计数、序列和回执。

会话在云端以 `<datasetId>/<source>:<id>` 命名，不同电脑互不覆盖。云端不保存本地
文件路径、导入游标或原始记录。

## 内容契约

`analysis-v1`，只保存四类消息：用户正文、主 Agent 正文、工具/subagent 调用（名称、ID、
完整参数）、工具结果成功/失败/unknown；每条带时间戳与来源标记。会话 metadata 白名单。
不脱敏。原始记录/分支接口返回 410/400。

## 管理命令

```bash
python -m server.manage set-password --database PATH [--stdin]   # 设置/重置密码
python -m server.manage create-key --database PATH --name NAME    # 生成上传 Key（打印一次）
python -m server.manage init-session-key --directory DIR
```

## 旧版本

协议 5 的“设备 + 任务队列”云库（含 `dist_settings` 表）不会被打开或修改。新版本
默认使用新的数据库文件（Docker 中 `/data/cloud-v6.db`）；数据由本地服务重新上传。

## 测试

```bash
python -m unittest discover -s server/tests
```
