# 云端部署：本机 Docker / VM + Cloudflare Tunnel

只部署**云端** Web Server；本地 Web Server 不需要 Docker，见
[local/README.md](../../local/README.md)。所有命令从**仓库根目录**执行。

## 配置与数据

| 文件 | 用途 |
|---|---|
| `Dockerfile`、`Dockerfile.dockerignore` | 仅 `server/` 代码/契约/页面的白名单镜像 |
| `docker-compose.local.yml` | 宿主机 `127.0.0.1:41880` 直连，无 Tunnel（与本机本地服务联调） |
| `docker-compose.yml` | VM：Viewer + cloudflared，应用不发布任何端口 |
| `.env.example` | VM HTTPS 域名与 Tunnel token 模板 |

镜像：Debian trixie 官方签名包（Python 3.13、Flask、Waitress），不关闭 TLS/签名校验。
UID/GID 10001:10001、只读根文件系统，仅 `/data` 与 tmpfs 可写；日志轮转；健康检查
不暴露数据。`local/`、测试、Git、数据库、历史、密钥、`.env` 均不进镜像。

卷 `/data` 内容：

| 文件 | 说明 |
|---|---|
| `cloud-v6.db` | 云端归档（新协议默认文件名；旧 `cloud.db` 不会被打开或修改） |
| `session.key` | 浏览器会话签名密钥，首次启动生成，0600 |
| `initial-admin-password` | 仅在未提供密码文件时生成的初始 Owner 密码；在控制台改密码后删除 |

## 一、本机直连

```bash
docker compose -f server/deploy/docker-compose.local.yml up -d --build --wait
docker compose -f server/deploy/docker-compose.local.yml exec -T viewer cat /data/initial-admin-password
```

打开 **http://localhost:41880**，用初始密码登录，然后在 **Cloud console** 中：

1. 修改 Owner 密码（12 位以上；修改后初始密码文件被删除，其它会话退出）。
2. 为每台电脑 **Generate API key**，复制只显示一次的 Key。
3. 在该电脑的本地页面填写云端地址 `http://localhost:41880` 与 Key，Test → Save → Sync。

只绑定 `127.0.0.1`。端口占用时先设置 `VIEWER_LOCAL_PORT`，浏览器和本地控制台都用
同一个新地址。错误 Host/Origin 会被拒绝，不要关掉校验来解决 403。

忘记密码（会让所有浏览器会话退出）：

```bash
docker compose -f server/deploy/docker-compose.local.yml exec -T viewer \
  python -m server.manage set-password --database /data/cloud-v6.db --stdin
# 在下一行输入新密码后回车（不会回显到日志）
```

Windows Git Bash 会把 `/data/...` 改写成 Windows 路径，执行上述命令前先
`export MSYS_NO_PATHCONV=1`（PowerShell 不需要）。

也可以在首次启动前通过 `VIEWER_ADMIN_PASSWORD_FILE` 指向挂载的私有密码文件
（Docker secret）；该文件只在库中尚无密码时使用，且从不被修改。

### 停止、重启和升级

```bash
docker compose -f server/deploy/docker-compose.local.yml logs --tail 50 viewer
docker compose -f server/deploy/docker-compose.local.yml restart viewer
docker compose -f server/deploy/docker-compose.local.yml up -d --build --wait
docker compose -f server/deploy/docker-compose.local.yml down
```

正常重启/重建不删除数据。**不要对真实数据用 `down -v`，不要全局 prune volumes。**

### 从协议 5（设备/任务队列）升级

旧版本的云库（`/data/cloud.db`，含设备、任务队列）**不会被打开、迁移或删除**。新版本
使用同一卷中的新文件 `cloud-v6.db`，并生成新的初始密码；旧 `owner.key` 不再使用。
各本地 Web Server 在本机导入后重新上传即可。确认无误后，旧文件可由你自行备份/删除。

## 二、VM + Cloudflare

```text
浏览器 / 本地 Web Server → HTTPS → Cloudflare → 出站 Tunnel → cloudflared
                                                       ↓ Docker 内网 HTTP
                                                  viewer:8080 → /data
```

前提：VM 安装 Docker/Compose 并开机启动，域名托管在 Cloudflare。不为应用开放 VM 的
80/443/8080 入站端口。Viewer 只在 internal backend 网络，cloudflared 另接 egress 网络。

```bash
cp server/deploy/.env.example server/deploy/.env
chmod 600 server/deploy/.env
```

```dotenv
VIEWER_PUBLIC_ORIGIN=https://history.example.com
CLOUDFLARE_TUNNEL_TOKEN=<该 Tunnel 提供的 token>
```

`.env` 不进 Git/镜像。Tunnel token 经环境变量传入；只用 `config --quiet` 验证，不要公开
展开后的配置。可固定 `CLOUDFLARED_IMAGE` / `VIEWER_IMAGE` 的 digest。

Cloudflare remotely-managed Tunnel 的 Published application route：

| 项 | 值 |
|---|---|
| 公共域名 | 与 `VIEWER_PUBLIC_ORIGIN` 一致 |
| 类型 | HTTP |
| 服务 URL | `viewer:8080` |
| HTTP Host Header | `history.example.com`（与 public origin 一致） |

```bash
docker compose --env-file server/deploy/.env -f server/deploy/docker-compose.yml config --quiet
docker compose --env-file server/deploy/.env -f server/deploy/docker-compose.yml up -d --build --wait
docker compose --env-file server/deploy/.env -f server/deploy/docker-compose.yml exec -T viewer cat /data/initial-admin-password
```

通过 HTTPS 域名登录并立即修改密码。生产 Cookie 自动 `Secure` 并发送 HSTS。本地
控制台填写 `https://history.example.com`。

### Cloudflare 注意事项

- 对应用域名禁用缓存，尤其 `/auth/*`、`/api/*`、`/ingest/*` 和私有 HTML。
- 可对 `/auth/login` POST 做边缘限流（应用自身也限流）。
- 不要对 `/ingest/v1/*` 加 JS/CAPTCHA challenge 或交互式 Access：本地服务是机器客户端，
  只携带 API Key。如需 Access，只保护网页路径。
- 默认批次约 1 MiB，应用请求上限 32 MiB（`VIEWER_MAX_REQUEST_MIB`），需与套餐的
  body/timeout 限制一致；超限请求返回 413 且不会被截断。
- Tunnel 不替代应用的密码和 API Key。

## 三、备份

用 SQLite backup API，或先正常停止单实例再备份整个卷；不要在写入时只复制 `.db` 而漏掉
WAL。数据库、`session.key` 需一起加密备份到 VM 外。若从旧备份恢复，本地服务会从云端
恢复后的已确认位置继续上传；若云端位置反而超过本地（本地库被旧备份替换），上传会
明确报 `LOCAL_BEHIND_CLOUD` 并停止，不会覆盖任何数据。

## 四、测试与排查

```bash
curl http://localhost:41880/healthz
python -m unittest discover -s server/tests
```

跨端 Docker E2E（代码在根目录 `tests/e2e/`）仅用于自己创建的隔离合成实例：

```bash
# PowerShell: $env:VIEWER_LOCAL_PORT='41881'   Bash: export VIEWER_LOCAL_PORT=41881
docker compose -p claudeviewer-smoke -f server/deploy/docker-compose.local.yml up -d --build --wait
python -c "from pathlib import Path; Path('server/deploy/secrets').mkdir(parents=True, exist_ok=True)"
docker compose -p claudeviewer-smoke -f server/deploy/docker-compose.local.yml cp \
  viewer:/data/initial-admin-password server/deploy/secrets/smoke-admin.password
python tests/e2e/smoke.py --origin http://localhost:41881 \
  --password-file server/deploy/secrets/smoke-admin.password --restart-project claudeviewer-smoke \
  --report server/deploy/artifacts/smoke.json
node tests/e2e/browser_smoke.mjs http://localhost:41881 server/deploy/secrets/smoke-admin.password --analysis-data
# 只删除合成测试卷，绝不对真实卷执行：
docker compose -p claudeviewer-smoke -f server/deploy/docker-compose.local.yml down -v
```

smoke 会在临时目录启动一个真实的本地 Web Server 子进程（合成历史、无窗口），通过
本地控制台 API 保存连接并上传，验证内容口径、容器重启持久化和增量上传。

常见问题：权限错误检查卷 UID 10001；502 检查 `viewer:8080` 是否 healthy；403 检查
public origin / HTTP Host Header；本地控制台收到 HTML 检查 Access/challenge；
`DATASET_MISMATCH` 表示该 Key 已被另一台电脑使用。
