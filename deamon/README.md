# Deamon

本地无状态历史同步进程（目录名按项目约定为 `deamon`）。只主动连接中心服务，
不监听本机端口，不接收 shell 命令，不修改 Agent 历史文件。

## 本地安装与启动

克隆整个 [agent-session-viewer](https://github.com/idyllic84/agent-session-viewer)
仓库，在根目录执行：

```bash
python -m pip install -r deamon/requirements.txt
python -m deamon --cloud https://history.example.com \
  --token-file /private/laptop.token --roots-file /private/roots.json
```

无需安装 Server 的 Flask/Waitress 或 Local 的依赖。客户端仅导入
`server/contracts/` 的纯数据契约，不导入服务端运行时、数据库或本地旧服务。
当前是 monorepo 源码运行方式，不支持只复制 `deamon/` 后丢掉数据契约。

### 第一次授权

1. 在 Server 网页用 owner key 登录。
2. 创建设备，勾选远程同步控制。
3. 保存只显示一次的设备 token 到私有 `*.token` 文件。
4. 启动 Deamon，看到网页设备 online 后，**网页下达 Sync now 才开始扫描**。

设备 token、owner key、浏览器 session key、Cloudflare Tunnel token 互不替代。
设备凭据有效期 30 天，轮换后替换本地 token 文件；保留相同 device ID 可继续使用
既有云端进度。凭据失效/撤销时停止，不无限重试。

### 允许同步的本地目录

```json
{
  "claude": ["C:/Users/you/.claude/projects"],
  "pi": ["C:/Users/you/.pi/agent/sessions"]
}
```

提供 roots 文件时，未列出的来源禁用；不提供时使用五种工具的默认历史目录。
本地配置是授权边界，网页不能增添路径或读取任意文件。请只配置你明确允许上传的
历史；正文和工具参数**不做凭据/签名等脱敏**。

### 连接本地 Docker 测试中心服务

```bash
python -m deamon --cloud http://localhost:41880 --allow-http-loopback \
  --token-file /private/laptop.token --roots-file /private/roots.json
```

HTTP 例外仅 loopback。连接真实中心服务用 HTTPS，不关闭证书验证、不接受跨域
重定向。不要把 token 写在命令行 URL 中。Cloudflare 的交互式 Access/challenge
不是机器 API 的登录方式。

## 云端保存唯一进度

Deamon 不创建本地 DB、checkpoint、outbox 或持久 parser cache，也没有 `--state`。
本地仅保存凭据/配置；任务工作数据放在 RAM，文件/任务结束即丢弃。

流程：

1. 认证、心跳、等待云端任务；空闲时不发现历史文件。
2. 按小窗口发现文件描述信息，批量查询云端进度（默认 64，上限 128）。
3. 对当前文件验证云端前缀/版本；默认一小时内改动的文件和 sidecar 暂缓，不读正文。
4. 生成四类分析消息，按实际 JSON 大小动态组批，默认目标 1 MiB。
5. 每批成功回执后继续同文件，再进入下一文件；不跨文件预读历史正文。
6. 断线/重启后重扫文件名并重新查云端，已提交的跳过。重试原请求用同 key，
   revision CAS 防止上一条延迟请求和新进程同时写出重复数据。

Claude/pi 可直接解析新尾部。Codex/Copilot/VS Code JSONL 某些更新需要在本机
内存重放已提交前缀，再处理 dirty turn/request；不会把该前缀上传或写到磁盘。
JSON 快照/无效检查点的 reconciliation 仍可能是不可拆分的单元，超过云端上限
明确报错，不截断参数或消息。

## 参数与后台运行

- `--batch-bytes`：分析 JSON 的目标大小，不是固定消息条数。
- `--progress-query-files`：描述信息进度查询窗口，默认 64。
- `--quiet-seconds`：默认 3600；仅本地可改，网页任务不替你关闭。
- `--daemon`：兼容参数，现在默认就是等指令的 daemon，无自动 one-shot 扫描。

Windows 需要无窗口常驻时，用任务计划程序的隐藏任务或 `pythonw.exe -m deamon`
加相同参数，工作目录设为仓库根目录；不要配置启动一个新的可见 console。
Linux 可用 systemd 用户服务，`WorkingDirectory` 指向仓库根目录，凭据文件仅当前
用户可读。不要把 token 或历史正文写进服务日志；stdout 只输出文件/阶段/错误码。

暂停/取消在原子批次边界生效，不回滚已上传内容。云端 worker lease 防止另一个
进程接管后旧进程继续提交。任务离线排队十分钟过期，运行中任务可在租约恢复后
继续，进度仍以 Server 为准。

## 自测

```bash
python -m unittest discover -s deamon/tests
```

这些测试使用纯内存假云端，不需要 Flask/SQLite/Docker 或真实网络。真实 HTTP、
五种来源、故障恢复和浏览器/Docker E2E 在根 `tests/`，部署文档在
[server/deploy/README.md](../server/deploy/README.md)。
