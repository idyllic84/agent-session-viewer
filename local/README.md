# 本地 Web Server

在本机增量导入 Agent 历史到**本地分析库**，提供无需登录的本地查看页和云同步
控制台。设计细节见 [docs/two-web-servers-design.md](../docs/two-web-servers-design.md)。

## 安装与启动（不需要 Docker）

```bash
python -m pip install -r local/requirements.txt
python -m local
```

启动顺序固定为：

1. 取得数据目录的单实例锁（同一库的第二个进程会拒绝启动）。
2. 打开/创建 `local/state/local.db`。
3. **按已保存的导入游标增量导入**五种来源（逐文件、只读）。
4. 导入完成后才监听 **http://localhost:8000**（仅 `127.0.0.1`；端口被占用时用 `--port`）。

首次导入全部历史可能需要十几到几十分钟（大文件按约 1 MiB 分批提交，可随时中断，
下次从游标继续）；之后每次启动只处理变化的文件，通常十几秒。

不需要云端即可导入和查看。启动不会上传，也不会启动任何监控或定时任务。

常用参数：

| 参数 | 说明 |
|---|---|
| `--port 8000` | 本地端口 |
| `--database PATH` | 指定本地分析库（不能是旧的 `viewer.db` / `claude.db`） |
| `--roots-file roots.json` | 只导入这些目录：`{"claude": ["..."], "pi": ["..."]}` |
| `--include-recent` | 不推迟一小时内修改过的文件 |
| `--quiet-seconds N` | 调整推迟窗口（默认 3600） |
| `--no-import` | 仅浏览，跳过启动导入 |
| `--credential-store auto\|keyring\|file` | API Key 存储位置 |

默认来源目录：`~/.claude/projects`、`$CODEX_HOME/sessions`、`~/.copilot/session-state`、
`~/.pi/agent/sessions`、VS Code（含 Insiders）的 `User` 目录；也可用环境变量
`VIEWER_SOURCE_ROOTS` 指定。

Windows 后台无窗口启动示例（PowerShell，日志写入已忽略的 `local/state/`）：

```powershell
Start-Process -WindowStyle Hidden -FilePath python -ArgumentList '-u','-m','local' `
  -WorkingDirectory 'D:\path\to\agent-session-viewer' `
  -RedirectStandardOutput local\state\local-server.log -RedirectStandardError local\state\local-server.err.log
```

## 页面

- **历史查看**：会话列表、全文检索（用户/助手正文）、消息详情。分析库不保存原始
  记录与分支，因此没有“原始记录”和分支选择。
- **Reload**：显式重新增量导入一次（不会常驻）。
- **Cloud sync console**：
  - 使用云端控制台生成的 **Upload** 类型 Key（Query 类型 Key 只能读，不能上传）。Key 的名称
    就是这台电脑在云端页面上显示的机器名。
  - 填写云端地址（`https://…`；仅 `http://localhost` / `127.0.0.1` 可用 HTTP）和云端
    控制台生成的 API Key。
  - **Test connection** 只检查可达性/鉴权/绑定状态，不上传。
  - **Save** 先验证再保存；Key 之后不再显示，只显示 Key ID。
  - **Sync to cloud** 上传自上次云端确认后的变更；**Pause / Cancel** 在批次边界生效，
    **Resume** 继续同一高水位。
  - 页面只在上传运行时轮询本机状态；本地服务不会轮询云端。

## 安全

- 无密码，但只绑定 loopback；校验 Host（防 DNS rebinding）和 Origin；写操作必须携带
  页面内的随机 CSRF token（每次进程启动不同）；无 CORS。
- API Key 优先存入系统凭据库（Windows 凭据管理器 / macOS 钥匙串 / Secret Service）；
  不可用时存为 `local/state/credentials/*.key` 仅属主可读文件（Windows 上依赖用户目录
  ACL，**不是加密存储**）。SQLite 只保存引用；Key 不写日志、不返回浏览器。
- 上传只发往已保存的云端 origin，验证 TLS，不跟随重定向，不使用代理环境凭据。

## 数据

| 位置 | 内容 |
|---|---|
| `local/state/local.db` | 分析消息、导入游标、最小解析状态、变更日志、云连接与上传进度 |
| `local/state/credentials/` | 文件形式的 API Key（仅在系统凭据库不可用时） |
| `local/viewer.db`、`local/claude.db` 及备份 | 旧完整历史库，**保持原样、只读** |

全部被 Git 忽略。两套进度：

- **导入游标**（`source_files` / `sync_checkpoints`）：哪些来源记录已安全写入本地库。
- **上传序列**（`change_log` + `cloud_connection`）：哪些本地变更已被云端确认。

本地库首次创建时会生成随机的数据集 ID（控制台 Local store 中可见）。首次同步时云端把它与
所用 API Key 绑定，用来区分不同电脑；因此每台电脑要用自己的 Key。删除或重建本地库会得到
新的数据集 ID，需要新 Key，或让云端 Owner 对旧 Key 执行“Allow a new computer”。

本地每次提交在一个事务里写入消息、游标和不可变变更日志。上传按字节动态组批，
批次范围/哈希/幂等键在发送前持久化，重试时从变更日志逐字节重建。

### 导入旧的完整历史库

```bash
python -m local.migrate --source local/viewer.db
```

源库以 SQLite 只读模式打开（主库与 WAL 内容不变；SQLite 读者可能刷新 `-shm` 共享内存文件的时间戳）；只插入本地库中不存在的会话（不会覆盖已从 Agent 文件导入的会话）；
内容按同一分析口径过滤；Agent 文件已消失的会话保留并标记为不可用。可重复执行。
建议先运行一次 `python -m local` 导入现有文件，再执行迁移。

## 内容口径

只保存：用户正文、主 Agent 回答正文、工具/subagent 调用（名称、ID、完整参数）、
工具结果成功/失败（不可判断时 `unknown`），每条带时间戳和来源。不保存 thinking、
系统事件、附件、原始记录、结果正文、过程日志、subagent 内部对话；不做敏感信息脱敏。

## 测试

```bash
python -m unittest discover -s local/tests
```

跨端（本地 ↔ 云端）测试在根目录 `tests/`。
