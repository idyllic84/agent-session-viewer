# Server

Agent Session Viewer 的中心服务：Viewer、鉴权、分析消息归档、全文检索、文件进度、
设备与任务 API。主项目说明见 [根目录 README](../README.md)。

## 部署

**[Docker / Cloudflare 部署文档](deploy/README.md)**，包括本地直连和 VM Tunnel 两套
Compose。所有部署文件均放在 `server/deploy/`，构建上下文为仓库根目录。

```bash
# 在仓库根目录执行
docker compose -f server/deploy/docker-compose.local.yml up -d --build --wait
# 精确访问 http://localhost:41880
```

镜像只复制 Server 的 Python、纯数据契约和自有前端，不复制 `local/`、`deamon/`、
测试、数据库、历史、日志、环境变量文件或密钥。非 root、只读根文件系统，只有
`/data` 数据卷可持久写入；日志轮转，健康检查不暴露会话信息。

Compose 历史项目名仍保留 `claudeviewer-local` / `claudeviewer-cloud`，以免仅因
仓库更名就悄悄换一套数据卷。镜像名更新为 `agent-session-viewer:latest`。

## 不使用 Docker 的本机启动

```bash
python -m pip install -r server/requirements.txt
python -m server.manage init-keys --directory server/state
python -m server --database server/state/cloud.db \
  --admin-key-file server/state/owner.key --session-key-file server/state/session.key \
  --public-origin http://127.0.0.1:8080 --allow-http-loopback --development
```

公网模式必须使用 HTTPS public origin，经可信反向代理到 Waitress。外部 HTTP
不允许。登录 key 与 session key 各自独立，至少 32 字符；首次 Docker 启动可自动
生成到卷中。不要把 key/token 放入 URL、Git 或浏览器 localStorage。

## 内容及存储契约

协议 v4，内容策略 `analysis-v1`，仅保存：

1. 用户消息正文。
2. 主 Agent 回答正文。
3. 工具/subagent 调用：名称、调用 ID、命令和参数。
4. 工具结果：成功/失败标志；来源不足以判断时 `unknown`。

每条消息有时间戳和来源标记；缺失时明确用 `file_mtime`，不冒充事件时间。
Session metadata 可以保存；不做敏感信息脱敏。Thinking、系统事件、附件、原始
事件、结果正文、过程日志和 subagent 内部对话不入库。

- `server/contracts/` 是不依赖 Flask/SQLite 的纯消息/分析契约，供 Deamon 使用。
- `db.py` / `codec.py` 只由 Server 使用。沿用兼容 schema v2 的归档基础存储，
  但分析服务不向原始记录表写数据，也不保存完整 source engine state。
- `projection_state` 仅保存分析 scope 的 digest/count；检查点仅格式、游标/hash、
  快照字节边界。API 的原始记录/分支重放明确返回不支持。
- Server 不导入或运行 Deamon 的扫描器，不读取客户端传来的本地文件路径。

## 主要接口

浏览器须登录，写操作校验 CSRF；`/sync/v1/*` 只接受设备 Bearer token，不接受
浏览器 cookie 代替。设备只可访问自己绑定的文件/session。

| 接口 | 作用 |
|---|---|
| `GET /healthz` | 最小 readiness（仍校验 Host） |
| `/auth/login`, `/auth/logout` | Owner 会话 |
| `/api/sessions`, `/api/sessions/<id>/messages` | 检索与 2000 条消息分页 |
| `/api/devices`, `/api/devices/<id>/jobs` | 设备管理与下达任务 |
| `/api/devices/<id>/jobs/<job>/<action>` | pause/run/cancel |
| `GET /sync/v1/capabilities` | 协议、请求上限、设备能力 |
| `POST /sync/v1/progress/query` | 1..128 个文件描述信息的云端进度查询 |
| `POST /sync/v1/context` | 当前文件的权威上下文/分析 scope digest |
| `POST /sync/v1/batches` | 普通 JSON 分析批次的原子提交 |
| `GET /sync/v1/receipts/<key>` | 查询已提交回执 |
| `/sync/v1/jobs/claim`, `/sync/v1/jobs/<id>/pulse` | 领取/续租/上报任务 |

`Idempotency-Key` + `If-Match` 保护重试与并发；数据、搜索、计数、检查点和成功
回执同一事务提交。没有 create-upload/chunk/seal 协议，断线后客户端重新查询
云端进度。Unexpected rewrite/truncation 不会静默覆盖更完整的既有内容。

## 迁移与备份

```bash
python -m server.manage adopt --source local/viewer.db --database server/state/cloud.db
```

必须在源历史仍位于本地时生成新的分析工件；源库只读，目标必须是新文件。
不要直接把详细 `viewer.db`、`claude.db` 上传到 VM。非空旧策略数据库拒绝自动
启动，需验证后显式切换；当前容器不会因源码重排而自动重启或迁移。

备份用 SQLite backup API，保留 DB、进度、回执与 key 文件的一致性。不要漏掉
活跃 WAL 直接复制 `.db`，也不要在 daemon 活跃时悄悄回滚实例。

## Server 自测

```bash
python -m unittest discover -s server/tests
```

仅临时数据：协议白名单、原子回滚、回执、权限、Cookie、健康、gzip、密钥持久化
和构建上下文规则。跨项目 HTTP / 历史解析 / Docker / 浏览器测试在根 `tests/`。
