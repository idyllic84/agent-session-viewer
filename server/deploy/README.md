# Server 部署：本地 Docker / VM + Cloudflare Tunnel

项目上游：[idyllic84/agent-session-viewer](https://github.com/idyllic84/agent-session-viewer)。
本目录只负责 Server；本地进程启动见 [deamon/README.md](../../deamon/README.md)，
旧本地服务见 [local/README.md](../../local/README.md)。所有命令从**仓库根目录**执行。

## 配置与数据

| 文件 | 用途 |
|---|---|
| `Dockerfile`、`Dockerfile.dockerignore` | 仅 Server 代码/契约/页面的白名单镜像 |
| `docker-compose.local.yml` | 宿主机 loopback 41880，直连演示，无 Tunnel |
| `docker-compose.yml` | VM：Viewer + cloudflared，应用不发布任何端口 |
| `.env.example` | VM HTTPS 域名与 Tunnel token 模板 |

历史 Compose 项目名 `claudeviewer-local` / `claudeviewer-cloud` 与数据卷名
`viewer-local-data` / `viewer-cloud-data` 保持兼容，避免仓库更名导致误换数据卷。
镜像名为 `agent-session-viewer:latest`。两套配置的数据互不共享，也不要启动两个
SQLite writer 访问同一卷。

镜像为 Debian trixie 官方签名运行时包：Python 3.13、Flask 3.1、Waitress 3.0。
构建不依赖 PyPI artifact CDN，不关闭 TLS 或包签名校验。运行 UID/GID
10001:10001，根文件系统只读，仅 `/data` 和临时 tmpfs 可写；日志轮转。
`local/`、`deamon/`、测试、Git、所有数据库/sidecar/密钥/历史均不进镜像。

## 一、本地直连

```bash
docker compose -f server/deploy/docker-compose.local.yml up -d --build --wait
docker compose -f server/deploy/docker-compose.local.yml ps
```

精确访问 **http://localhost:41880**。只绑定 `127.0.0.1`，不是局域网服务。
容器内部监听 `0.0.0.0:8080` 仅用于端口转发；HTTP 例外只接受 loopback public
origin。若端口被占用，启动前设置 `VIEWER_LOCAL_PORT`，浏览器和 Deamon 都用
同一个新 URL。错误 Host/Origin 会被拒绝，不要关掉校验解决 403。

### 登录密钥

首次启动在持久卷创建 `/data/owner.key`、`/data/session.key`，之后一直复用，
没有写死默认密码，不把值打印到服务日志。只在你自己的终端读取 owner key：

```bash
docker compose -f server/deploy/docker-compose.local.yml exec -T viewer \
  python -c "from pathlib import Path; print(Path('/data/owner.key').read_text(), end='')"
```

也可复制到已忽略的私有目录：

```bash
python -c "from pathlib import Path; Path('server/deploy/secrets').mkdir(parents=True, exist_ok=True)"
docker compose -f server/deploy/docker-compose.local.yml cp \
  viewer:/data/owner.key server/deploy/secrets/local-owner.key
```

Linux 对副本 `chmod 600`，Windows 使用仅当前用户可读的 ACL；不要把密钥贴进
聊天/Git/日志。Owner key、session key、设备 token、Tunnel token 是不同凭据。

登录后创建设备并勾选远程控制权限，保存一次性显示的设备 token。在本机启动：

```bash
python -m pip install -r deamon/requirements.txt
python -m deamon --cloud http://localhost:41880 --allow-http-loopback \
  --token-file /private/laptop.token --roots-file /private/roots.json
```

默认每分钟 GET 网页任务，不发送 heartbeat、不自动扫描；GET 不出队，Deamon
POST 收到确认后服务端才出队；完成当前任务后才能领取下一项。Server 保存全部
进度/队列/执行记录，Deamon 没有本地进度库。
上传是四类分析消息（用户、主 Agent 正文、调用、结果状态），无 thinking/系统
事件/附件/原始记录，不做敏感信息脱敏。

### 停止、重启和升级

```bash
docker compose -f server/deploy/docker-compose.local.yml logs --tail 50 viewer
docker compose -f server/deploy/docker-compose.local.yml restart viewer
docker compose -f server/deploy/docker-compose.local.yml up -d --build --wait
docker compose -f server/deploy/docker-compose.local.yml down
```

正常重启/镜像重建不删除数据。**不要对真实数据用 `down -v`，不要全局 prune
volumes，不为本项目重启整个 Docker daemon 或停止其他项目。**

## 二、VM + Cloudflare

```text
浏览器 / Deamon → HTTPS → Cloudflare → 出站 Tunnel → cloudflared
                                                    ↓ Docker 内网 HTTP
                                               viewer:8080 → /data
```

前提：VM 安装 Docker/Compose、启用 Docker 开机启动，域名已托管到 Cloudflare。
不需为应用开放 VM 的 80/443/8080 入站端口。Viewer 仅在 internal backend 网络，
cloudflared 另接 egress 网络建立出站隧道。

### 配置

```bash
cp server/deploy/.env.example server/deploy/.env
chmod 600 server/deploy/.env
```

填写：

```dotenv
VIEWER_PUBLIC_ORIGIN=https://history.example.com
CLOUDFLARE_TUNNEL_TOKEN=<该 Tunnel 提供的 token>
```

不要把 `.env` 加入 Git 或镜像。Tunnel token 经环境变量而非 argv 传入；Docker
管理员仍可查看环境，所以 VM/Docker socket 权限必须受控。只用 `config --quiet`
验证，别公开含真实 token 的展开配置。可固定 `CLOUDFLARED_IMAGE` / `VIEWER_IMAGE`
的 tag/digest；构建标识可设置 `VCS_REF`。

Cloudflare remotely-managed Tunnel 的 Published application route：

| 项 | 值 |
|---|---|
| 公共域名 | 与 `VIEWER_PUBLIC_ORIGIN` 一致 |
| 类型 | HTTP |
| 服务 URL | `viewer:8080`（即 `http://viewer:8080`） |
| HTTP Host Header | `history.example.com`，与 public origin host/port 一致 |

不能填 localhost/127.0.0.1：这是两个容器。内部服务不是 HTTPS，公网 TLS 在
Cloudflare 终止。不要为了消除 403 而去掉 Host/Origin 校验。

```bash
docker compose --env-file server/deploy/.env -f server/deploy/docker-compose.yml config --quiet
docker compose --env-file server/deploy/.env -f server/deploy/docker-compose.yml up -d --build --wait
docker compose --env-file server/deploy/.env -f server/deploy/docker-compose.yml logs --tail 50 viewer cloudflared
```

Viewer healthy 后 Tunnel 才启动。通过 HTTPS 域名登录，Owner key 的读取命令
换用 VM Compose 参数。生产 Cookie 自动 Secure；不要打开 local HTTP 例外。
Deamon 用 HTTPS URL，不带 `--allow-http-loopback`。

### Cloudflare 注意事项

- 对应用域名禁用缓存，尤其 `/auth/*`、`/api/*`、`/sync/*` 和私有 HTML。
- 对 `/auth/login` POST 在边缘限流，避免记录正文/凭据。
- 不要对 `/sync/v1/*` 加 JS/CAPTCHA challenge；机器客户端无法交互。
- 不要直接给全站套交互式 Access。当前 Deamon 没有 Access service-token
  headers；如要额外 Access，先仅保护网页或明确增加机器凭据支持。
- 默认分析批次约 1 MiB，应用请求上限 32 MiB；也需匹配套餐/WAF/body/timeout
  限制。增大应用配置不会绕过边缘限制。
- Tunnel 不替代用户和设备鉴权，也不让单用户预览自动变成多租户 SaaS。

## 三、历史迁移与备份

原本地数据库现在在 `local/`，均不进 Git。不能直接上传详细的 `local/viewer.db`
或 `local/claude.db`。先在**本机**构建新的分析工件：

```bash
python -m server.manage adopt --source local/viewer.db --database server/state/cloud.db
```

源只读，目标必须是新文件。仅将完成验证的分析工件导入已停止服务的新卷。
原始事件、thinking、附件和结果正文不会复制过去，legacy-only 正文/调用可保留。
非空旧策略云库拒绝启动，不会自动清洗旧卷。恢复/切换需新 instance 和明确重授权，
不要在 daemon 活跃时悄悄回滚。

SQLite 备份用 backup API，或先正常停止单实例再备份完整卷；不要忽略 WAL 复制
正在写的 `.db`。DB、进度、回执和私有 key 文件需一致备份，加密并存到 VM 外。

## 四、测试与排查

```bash
curl http://localhost:41880/healthz
# VM: curl https://history.example.com/healthz
python -m unittest discover -s server/tests
```

健康接口只返回状态/协议/构建标识，仍受 Host 校验。登录页使用 `same-origin`
Referrer-Policy，避免导航表单 `Origin: null`；未放宽 Origin/CSRF。

跨项目 E2E **代码位于根目录 `tests/e2e/`**。以下仅限自己创建的合成隔离实例：

```bash
# Bash: export VIEWER_LOCAL_PORT=41881
# PowerShell: $env:VIEWER_LOCAL_PORT='41881'
docker compose -p claudeviewer-smoke -f server/deploy/docker-compose.local.yml up -d --build --wait
python -c "from pathlib import Path; Path('server/deploy/secrets').mkdir(parents=True, exist_ok=True)"
docker compose -p claudeviewer-smoke -f server/deploy/docker-compose.local.yml cp \
  viewer:/data/owner.key server/deploy/secrets/smoke-owner.key
python tests/e2e/smoke.py --origin http://localhost:41881 \
  --owner-key-file server/deploy/secrets/smoke-owner.key --restart-project claudeviewer-smoke \
  --report server/deploy/artifacts/smoke.json
# 只删除合成测试卷，绝不对真实卷执行：
docker compose -p claudeviewer-smoke -f server/deploy/docker-compose.local.yml down -v
```

空白演示实例的浏览器检查（Node 22+ / Edge）：

```bash
node tests/e2e/browser_smoke.mjs http://localhost:41880 server/deploy/secrets/local-owner.key
```

测试不扫描真实历史，Windows 子进程无窗口；报告不含 token/正文。
常见问题：权限错检查数据卷 UID10001；502 检查 `viewer:8080` 与 healthy；403 检查
public origin/HTTP Host Header；机器收到 HTML 检查 Access/challenge；重启丢进度
检查是不是换卷或用了 `down -v`。当前运行的旧容器不会因源码目录调整自动重启。
