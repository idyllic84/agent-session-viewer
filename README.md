# Agent Session Viewer

[GitHub](https://github.com/idyllic84/agent-session-viewer) ·
[Server](server/README.md) · [Deamon](deamon/README.md) ·
[Docker / Cloudflare 部署](server/deploy/README.md) · [原本地服务](local/README.md)

把本机 Claude Code、Codex、Copilot CLI、pi 和 VS Code Chat 的历史增量汇集到
一个中心服务，用于**内容分析**，而不是完整还原 Agent 的执行细节。

```text
本机历史文件（只读）                    中心服务（本地 Docker / VM）
        ↓                                      ↑
 deamon：等待云端任务 ── HTTPS / 设备鉴权 ── server：Viewer + API + DB
        ↓                                      ↑
 批量查询云端进度 → 按大小组批增量上传       网页登录 / 任务控制 / 内容检索
```

## 三个项目

| 目录 | 职责 | 测试 |
|---|---|---|
| [`server/`](server/README.md) | Viewer、鉴权、消息归档、云端进度、任务 API；部署配置在 `server/deploy/` | `server/tests/` |
| [`deamon/`](deamon/README.md) | 本地无状态同步进程、五种来源解析器、本地启动说明 | `deamon/tests/` |
| [`local/`](local/README.md) | 原 all-in-one 本地服务、完整历史归档、原页面及本地数据库 | `local/tests/` |
| [`tests/`](tests/README.md) | 跨项目集成测试与真实 HTTP / Docker / 浏览器 E2E | 仓库根目录 |

`server/contracts/` 是双方共用的纯数据契约，不启动服务，也不导入 Flask/SQLite。
Deamon 不依赖本地服务 `local/`；Server 的镜像只包含 `server/`，不包含本机历史、
Deamon 或旧本地版。

## 快速开始：本地 Docker 模拟中心服务

在仓库根目录执行：

```bash
docker compose -f server/deploy/docker-compose.local.yml up -d --build --wait
```

访问 **http://localhost:41880**。只绑定宿主机 `127.0.0.1`，没有 Tunnel。
数据、owner 登录密钥、浏览器 session key 位于独立命名卷 `/data`，重建镜像和
正常重启不清除它们。不要使用 `down -v` 删除真实数据。

在你自己的终端获取首次自动生成的 owner key（不要发到聊天、日志或 Git）：

```bash
docker compose -f server/deploy/docker-compose.local.yml exec -T viewer \
  python -c "from pathlib import Path; print(Path('/data/owner.key').read_text(), end='')"
```

完整说明、VM 的 Cloudflare Tunnel 配置和备份注意事项：
**[server/deploy/README.md](server/deploy/README.md)**。

## 接入本机 Deamon

1. 登录网页，展开 **Cloud devices / sync control**。
2. 创建设备并允许远程同步控制，保存只显示一次的设备 token。
3. 在本机安装客户端依赖、配置允许访问的历史目录并启动：

```bash
python -m pip install -r deamon/requirements.txt
python -m deamon --cloud http://localhost:41880 --allow-http-loopback \
  --token-file /private/laptop.token --roots-file /private/roots.json
```

公网中心服务改用 `https://history.example.com`，不带 HTTP 例外参数。
本地进程默认只认证、心跳和等任务；**网页点 Sync now 后才扫描**。
配置和 Windows 后台启动见 **[deamon/README.md](deamon/README.md)**。

### 同步行为

- 所有任务、文件指纹、游标、版本和幂等回执都保存在 Server。
- Deamon 不创建本地进度 DB、checkpoint 文件、outbox 或持久解析缓存。
- 文件描述信息分小窗口批量查询云端进度（默认 64，最多 128）；不先扫描全部正文。
- 正文逐文件处理。同一文件可发送多个 POST，默认按实际分析 JSON 约 1 MiB
  动态组批，不固定消息条数，不跨文件凑包。
- 每批数据、索引、计数、进度和成功回执原子提交；重试幂等，版本冲突不覆盖。
- 一小时内改动的文件/sidecar 默认暂缓；不修改或删除任何 Agent 历史。
- 重启后重新查 Server 的进度再继续。某些来源需要在本机内存重放已提交前缀，
  但不会把旧原始记录上传或写到本地进度文件。

## 上传哪些内容？

协议 v4 / `analysis-v1` 只保留四类消息：

| 类型 | 保存内容 |
|---|---|
| 用户消息 | 用户正文 |
| 主 Agent 回答 | 回答正文 |
| 工具 / subagent 调用 | 名称、调用 ID、完整命令/参数/任务描述 |
| 工具结果 | 调用 ID、成功/失败标志，不保存输出或详细错误 |

- 每条消息带毫秒时间戳及时间来源。原时间缺失时使用文件修改时间并标注；
  来源没有可靠结果标志时记 `unknown`，不靠输出文字猜测。
- 保留 session 级标题、来源、项目路径、模型/provider、用量等 metadata。
- 不上传 thinking/reasoning、系统事件、附件、工具过程/日志、subagent 内部对话、
  原始记录或完整解析快照。云端不提供原始记录/分支重放。
- **不做敏感信息脱敏**：正文与调用参数中的凭据、代码、路径、签名不会自动遮罩。
  请仅同步你明确批准上传的目录。

## 原本地服务及现有数据库

原有代码、页面、测试和数据库都已归入 `local/`：

```bash
python -m pip install -r local/requirements.txt
python -m local.server --no-sync
```

这会浏览 `local/viewer.db`；去掉 `--no-sync` 才执行本地启动同步。旧 `claude.db`、
Viewer 备份及 sidecar 也保留在 `local/`，没有因重组而删除或改写。
本地模式与 Server 的内容策略不同：本地完整归档不是直接可上传的分析库。

**不要把原始数据库直接传到云端**。如需带上仅存在旧库的历史，在本机生成新的
分析工件：

```bash
python -m server.manage adopt --source local/viewer.db --database server/state/cloud.db
```

工具读取源库但不覆盖它；迁移需要一个新的目标文件。非空旧版详细云库不会被
启动时自动清洗，须备份后显式切换到新工件并重新授权设备。
所有数据库、WAL/SHM、日志、密钥与运行产物均被 Git 忽略；真实数据库不属于源码。

## 验证

三个项目分别测试，跨项目测试留在根目录：

```bash
# 开发机执行全部测试时先安装三个项目的依赖；Docker 用户不需安装到宿主机
python -m pip install -r server/requirements.txt -r deamon/requirements.txt -r local/requirements.txt
python -m unittest discover -s server/tests
python -m unittest discover -s deamon/tests
python -m unittest discover -s local/tests
python -m unittest discover -s tests
node --experimental-default-type=module local/tests/test_utils.mjs
```

浏览器 / Docker E2E（Node 22+ / Edge 或 Chromium）：

```bash
node --experimental-default-type=module tests/e2e/local_browser.mjs
# Docker 中心服务的隔离合成历史测试见 server/deploy/README.md
```

测试使用合成历史和临时数据库。Windows 下测试子进程无窗口启动。

## 当前边界

这是**单用户、多设备预览版**，不是公开注册的多租户 SaaS。已实现用户/设备鉴权、
任务控制、动态批量、云端进度和幂等提交；尚未实现 OIDC/SSO、refresh-token
轮换、多实例数据库写入及完整运营配额。SQLite 应使用单写服务和持久本地/块存储。
Cloudflare Tunnel 不替代应用鉴权，也不会自动让机器客户端通过交互式 Access。
