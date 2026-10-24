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
