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
4. 启动 Deamon，网页显示最近联系时间；**网页下达 Enqueue sync 才开始扫描**。
   页面任务进入每设备 FIFO，Deamon GET 后先 POST 收到确认，确认成功才执行。

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

1. 启动时认证/注册一次，之后每分钟 GET 一个任务；**没有 heartbeat 请求**，空闲不扫描。
   GET 不使任务出队；POST 收到确认成功后，服务端出队并保留执行记录。当前任务
   完成且报告结果后才再次 GET，不预取或并行执行后续任务。
2. 按小窗口发现文件描述信息，批量查询云端进度（默认 64，上限 128），同时登记存在性，
   不给未变化/近期文件单独发 presence。
3. 对当前文件验证云端前缀/版本；默认一小时内改动的文件和 sidecar 暂缓，不读正文。
4. 生成四类分析消息，按实际 JSON 大小动态组批，默认目标 1 MiB。
5. 每批回执携带服务端已提交的下批上下文和控制指令，直接继续，不每批重新查 context。
   HTTP Session 按线程复用以复用连接；没有新增本地持久进度。不跨文件预读正文。
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

暂停/取消在原子批次边界生效，不回滚已上传内容。业务响应附带当前控制指令；
没有业务请求或已暂停时，在安全边界最多每分钟 GET 当前任务的 commands，
这不是 heartbeat，也不会领取下一任务。长解析期间不会发后台网络保活；本地
慢步骤日志仍可每五秒输出一次。

执行权由收到确认后的 worker assignment 决定，不会仅因过了某个 heartbeat
时间就拒绝原 worker 的长解析结果。若另一个进程启动，同一未完成任务在业务
联系已停止五分钟后可重新投递并确认恢复；旧 worker 被 fencing，不能继续写。
无 heartbeat 不能可靠判断崩溃还是长计算，网页只显示最近联系，不承诺在线状态。

未确认任务仍在 FIFO 中，默认不过期；取消待领任务仅删队列项，任务记录保留。
收到确认丢回包可重试，执行完的结果回包也可重试。重启恢复当前未完成任务，
不因队列中有更多任务就跳过去。进度始终以 Server 为准。

## 自测

```bash
python -m unittest discover -s deamon/tests
```

这些测试使用纯内存假云端，不需要 Flask/SQLite/Docker 或真实网络。真实 HTTP、
五种来源、故障恢复和浏览器/Docker E2E 在根 `tests/`，部署文档在
[server/deploy/README.md](../server/deploy/README.md)。
