# Agent Session Viewer

[GitHub](https://github.com/idyllic84/agent-session-viewer) ·
[本地 Web Server](local/README.md) · [云端 Web Server](server/README.md) ·
[云端 Docker / Cloudflare 部署](server/deploy/README.md) ·
[设计文档](docs/two-web-servers-design.md)

把本机 Claude Code、Codex、Copilot CLI、pi 和 VS Code Chat 的历史增量导入
**本地分析库**，在本机网页里查看；需要时由用户在本地控制台把数据同步到
**云端**，做集中的内容分析。

```text
Agent 原始历史（只读）
        ↓ 启动时按本地游标增量导入（无监控/定时任务）
本地 Web Server（无需登录，仅 localhost）  ── 用户点击“同步到云端” ──→  云端 Web Server（密码登录）
  本地分析库 + 历史页 + 云同步控制台          HTTPS + API Key，普通批量 POST     归档 + 历史页 + Key 管理
```

## 两个应用

| 目录 | 职责 | 登录 | Docker | 测试 |
|---|---|---|---|---|
| [`local/`](local/README.md) | 五种来源增量导入、本地分析库、本地查看页、云同步控制台 | 不需要（仅 loopback） | 不需要 | `local/tests/` |
| [`server/`](server/README.md) | 云端归档、按机器区分的查看页、Upload/Agent API Key 管理、ingest 与只读 Agent API | 控制台密码 | 本机直连 / VM + Cloudflare 两套 Compose | `server/tests/` |
| [`tests/`](tests/README.md) | 跨端集成、迁移、真实 HTTP / Docker / 浏览器 E2E | — | — | 仓库根目录 |

`server/contracts/` 是两端共用的纯数据契约（不导入 Flask/SQLite）。本地服务
不导入云端运行时；云端镜像只包含 `server/`。

## 快速开始

### 1. 本地 Web Server（无 Docker）

```bash
python -m pip install -r local/requirements.txt
python -m local            # 先增量导入历史，完成后才打开 http://localhost:8000
python -m local --port 8010   # 8000 被其他程序占用时换端口
```

数据库在 `local/state/local.db`（Git 忽略）。首次导入全部历史可能需要十几到几十分钟，
之后每次启动只处理变化的文件。旧的完整历史库 `local/viewer.db` 保持只读，可在停止
本地服务后一次性导入：`python -m local.migrate --source local/viewer.db`。

### 2. 云端 Web Server（Docker）

```bash
docker compose -f server/deploy/docker-compose.local.yml up -d --build --wait
docker compose -f server/deploy/docker-compose.local.yml exec -T viewer cat /data/initial-admin-password
```

用该初始密码登录 **http://localhost:41880**，点右上角 **Console** 修改密码，并为每台电脑
生成一个 **Upload** 类型的 Key（只显示一次，Key 名称即机器名）；给分析 agent 再生成一个
只读的 **Agent** Key，调用 `/agent/v1/*`（Owner 登录后也可直接调用）。VM + Cloudflare 部署见
[server/deploy/README.md](server/deploy/README.md)。

### 3. 连接并同步

在本地页面展开 **Cloud sync console**：填写云端地址和**上传** API Key → Test →
Save → **Sync to cloud**。保存连接不会上传；同步只在点击时运行。

云端页面的会话列表会显示每个会话来自哪台机器（上传 Key 的名称），并可用 **Machine**
下拉框筛选。云端页面适配手机：窄屏时一次显示一栏（会话 → 对话 → 消息详情），
支持返回键，筛选条件收在 **Filters** 里。

### 4. 分析 agent 读取历史

Agent API 只做三件事：列出全部会话、查看指定会话的 metadata、分页读取历史消息。

```bash
curl -H "Authorization: Bearer <Agent Key>" "http://localhost:41880/agent/v1/sessions?limit=200"
curl -H "Authorization: Bearer <Agent Key>" "http://localhost:41880/agent/v1/sessions/<URL 编码的会话 id>"
curl -H "Authorization: Bearer <Agent Key>" "http://localhost:41880/agent/v1/sessions/<URL 编码的会话 id>/messages?limit=500"
```

返回格式见 [server/README.md](server/README.md#agent-api只读)。

## 同步行为

- 两套独立进度：来源文件 → 本地库（导入游标），本地库 → 云端（变更序列）。
- 每次本地提交在同一事务里写入消息、导入游标和不可变变更日志。
- 上传读取本地库，不重新扫描 Agent 文件；按实际 JSON 字节动态组批（默认约 1 MiB），
  每批是连续的提交组；超过 1 MiB 的单个提交组（如超大会话的首次导入）按变更边界拆分。
  云端单请求上限 16 MiB，只为极少数单条超大消息兜底。
- 云端按 `expectedSeq` 比较并设置（CAS），消息、索引、计数、序列和幂等回执原子提交；
  响应丢失时用同一 Idempotency-Key 重发同一字节，不会重复。
- 每次同步开始重新读取云端已确认位置；本次运行的高水位固定，期间新导入的数据留待下次。
- 没有云端任务、轮询、heartbeat、文件监控或定时上传。
- 每台电脑使用自己的 API Key；首次同步时 Key 与该电脑本地库的随机 `datasetId` 绑定，
  云端按数据集隔离会话和上传进度，同一 Key 不能被另一台电脑复用。详见
  [server/README.md](server/README.md#如何区分不同客户端)。
- 一小时内修改过的文件（含 workspace sidecar）默认推迟导入；不修改或删除任何 Agent 历史。

## 内容口径（两端相同）

仅四类：用户正文、主 Agent 回答正文、工具/subagent 调用（名称、ID、完整参数）、
工具结果的成功/失败（无法判断时 `unknown`）。每条带时间戳及来源标记。不保存
thinking、系统事件、附件、原始记录、结果正文、过程日志或 subagent 内部对话；
**不做敏感信息脱敏**——请只把数据上传到你信任的云端。

## 测试

```bash
python -m unittest discover -s local/tests
python -m unittest discover -s server/tests
python -m unittest discover -s tests
node --experimental-default-type=module tests/e2e/local_browser.mjs
```

Docker / 浏览器 E2E 见 [server/deploy/README.md](server/deploy/README.md#四测试与排查)。
