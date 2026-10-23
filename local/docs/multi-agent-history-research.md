# 多 Agent 对话历史接入调研

调研日期：2026-10-05。对象：当前 ClaudeViewer，以及本机 Claude Code、Codex、GitHub Copilot CLI、VS Code Chat、pi。

## 1. 结论

**可以在现有 Flask + SQLite + 原生 JavaScript 架构上接入这些历史，不需要启动各 Agent、不需要模型 API，也不需要先改造成 React/Electron 应用。**

推荐路线：

1. 把 Claude 专用文件读取层改成独立的 source adapters。
2. 保存原始记录，同时生成统一的会话、消息、内容块和关系模型。
3. 第一版全部只读导入；“清除 viewer 索引”和“删除 Agent 原始历史”必须是不同功能。
4. 先接入 pi、Codex 和 Copilot CLI，再接入复杂度更高的 VS Code Chat。
5. 顺便解决现有同步一致性、搜索全表扫描、消息一次性加载的问题，但暂不引入云同步、语义搜索或 Agent 执行能力。

最值得参考：

- **agentsview**：覆盖最完整，适合参考多源架构、发现机制、同步和测试。
- **AI Control Plane**：Python + Flask + SQLite，与现项目技术路线最接近，特别适合参考 Copilot/VS Code 接入。
- **ctx**：原始记录溯源、稳定身份、增量导入、子代理与 fork 关系的参考。
- **Agent Sessions**：pi/Copilot 解析和跨版本测试样本的参考；其 macOS/Swift UI 不适合直接作为本项目基础。
- **OpenAI Euphony**：官方 Codex 可视化与可复用 Web Components 的参考，不是完整的本地多源索引器。

以上是基于 README、官方类型定义、部分解析器和测试的判断；**没有安装或运行这些第三方产品，也没有做性能对比测试**。

## 2. 调研方式与边界

- 使用 LangSearch 进行 13 组公开搜索，覆盖多源查看器、Codex、Copilot CLI、VS Code Chat、pi 和历史搜索工具。
- 搜索结果有明显噪声，关键结论进一步用已识别项目的 GitHub README、源码、测试和官方实现核对。
- GitHub 未认证 API 在调研中达到限额；后续通过已识别仓库的 raw 文件继续核对，不影响下述核心结论。
- 本机仅只读扫描文件、统计字段/事件类型、只读查询 SQLite，未读取认证文件、未同步、未删除、未启动 Agent。
- 本机私人对话、目录清单和数据库内容未发送到搜索服务或 GitHub。
- 本机 VS Code 补丁做了独立的只读内存重放检查，未执行下载的第三方源码。
- 引用针对调研时的 upstream `main`；真正移植代码时应固定 commit、保留许可证，并用本项目测试验证。

## 3. 本机实际数据

以下为调研时快照，日志数量不一定等于有效会话数量。

| 来源 | 本机位置 | 已确认的数据 |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/`；当前 viewer 的 `claude.db` | viewer 数据库已有 321 会话、35 项目、211,920 记录，约 1.14 GiB；未重新扫描 Claude 原始文件 |
| Codex | `~/.codex/sessions/**/*.jsonl` | 46 文件、46 个唯一 thread ID；42 个声明 paginated，4 个为旧格式/未声明；本机 `state_5.sqlite` 有 42 条 threads |
| Copilot CLI | `~/.copilot/session-state/<id>/events.jsonl` | 8 个 events.jsonl；`session-store.db` 有 8 条 sessions；还有 workspace YAML、计划、checkpoint 等关联资料 |
| pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | 9 文件，全部 v3；存在 user、assistant、toolResult、thinking、toolCall、model_change、compaction |
| VS Code Chat | `%APPDATA%/Code/User/workspaceStorage/*/chatSessions/` | 30 JSON、56 JSONL，共 86 个工作区存储文件 |
| VS Code 无工作区聊天 | `%APPDATA%/Code/User/globalStorage/emptyWindowChatSessions/` | 另有 10 个文件；合并上述存储共 96 文件、95 唯一 session ID，37 个文件的重建状态包含 requests |

VS Code 重放的物理 request 总数为 4,299，包含重复存储副本，**不是去重后的会话/消息数量**。检查中未遇到操作重放错误，但没有逐条人工验证内容。

本机 VS Code JSONL 操作包含 Initial、Set、Push；路径深度最多 4 层，存在带 `i` 的数组操作。重建后的响应块包含 markdown、thinking、toolInvocationSerialized、inlineReference、textEditGroup、workspaceEdit、questionCarousel、confirmation 等，说明只有文本问答解析远远不够。

本机 Codex 有 13 个日志头包含 `subagent_history_start_ordinal`。这意味着子代理原始文件可能带有父级模型上下文，不能把全部 response_item 都当成子代理自己说过的话。

目前 `CODEX_HOME`、`PI_CODING_AGENT_DIR`、`PI_CODING_AGENT_SESSION_DIR` 未设置。发现模块仍应支持环境变量和用户手动添加 root，不应把本机默认路径写死。

## 4. 各来源解析策略

### 4.1 Codex：rollout 是正文，SQLite 是可选元数据来源

主要来源：

```text
$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl
默认 CODEX_HOME = ~/.codex
可选：archived_sessions/，本机当前没有该目录
可选：state_*.sqlite 的 threads / thread_spawn_edges
```

`history.jsonl` 可以提供输入历史，但不能替代完整 rollout 对话。

日志有两层类型：外层 `type` 与内层 `payload.type`，不能沿用当前 Claude 的单层消息类型识别。

| 记录 | 用途 |
| --- | --- |
| session_meta | thread 身份、cwd、版本、来源、history_mode、父子/fork 元数据 |
| turn_context | 每轮模型、推理配置、权限等上下文 |
| response_item / message | user/assistant 文本、内容块；也可能是模型上下文或重放内容 |
| response_item / function_call、custom_tool_call | 工具调用，保留 call_id、名称、参数/原始 input |
| response_item / function_call_output、custom_tool_call_output | 工具结果，与 call_id 关联 |
| response_item / reasoning | 仅展示明文 summary/content；不解密 opaque 内容 |
| event_msg / item_completed | 完成后的 UserMessage、AgentMessage、CommandExecution、FileChange、Reasoning、ContextCompaction、McpToolCall 等 |
| event_msg / token_count | 旧式 token 使用信息，不可把累计计数逐条相加 |
| compacted | 压缩事件；replacement_history 是上下文替换，不应当新对话全文再展示一次 |
| token_usage_record | 新式 usage；需区分单 response、turn 和 thread 累计口径 |
| world_state | 系统状态/上下文信息，不是普通用户发言 |

#### 必须处理的身份与重复问题

- **thread 主键使用 session_meta.payload.id**。新格式的 `session_id` 是 root thread ID，不能用它合并所有子代理。
- 文件名是存储定位，不应成为唯一会话身份。官方类型说明 revert 后物理 rollout ID 可变化，而逻辑 thread ID 保持稳定。
- paginated 模式下需要结合 `ordinal`、item/turn ID 和官方边界规则选择正文来源。保留所有原始记录，但统一对话不能同时把 response_item、item_completed、压缩后的 replacement_history 机械展开成三份。
- 可优先以完整的 item_completed 构建用户可见的完成项，利用 response_item 补充函数参数和其他缺失细节；旧格式以 response_item 为基础。**这只是设计方向，具体权威关系必须通过 fixtures 验证，不是“同文本就去重”。**
- `subagent_history_start_ordinal` 用于区分继承上下文与子代理自己的记录；`history_base`、fork 边界需单独处理。
- 对真实重复提问必须保留。禁止对全库用文本 hash 去重。
- 官方 Codex protocol/models/items、ctx 的 paginated fixture，比泛化“支持 Codex”的 README 更有参考价值。

### 4.2 Copilot CLI：语义事件流，不等于模型内部快照

主要来源：

```text
~/.copilot/session-state/<session_id>/events.jsonl
同目录 workspace.yaml / plan.md / checkpoints / files
~/.copilot/session-store.db
```

| 原生事件 | 建议映射 |
| --- | --- |
| session.start / session.resume | 元数据/生命周期，保持同一会话 |
| user.message | 用户内容，保留 attachments、transformedContent 等原字段 |
| assistant.message | 助手内容，解析 reasoningText 和 toolRequests |
| tool.execution_start | 工具执行开始，按 toolCallId 与调用关联 |
| tool.execution_complete | 执行状态和结果，支持字符串/对象/内容块 |
| session.model_change | 模型变化事件 |
| subagent.started / completed / deselected | 子代理生命周期，能确认关系才关联 |
| abort / session.shutdown | 中止、结束、usage 等 |
| hook.* / permission.* / system.* | 可过滤的系统/调试事件 |
| model.messages_snapshot / model.message / model.response | 默认归为底层调试记录，不再复制成正常聊天 |

`assistant.message.toolRequests` 与 `tool.execution_start` 往往描述同一调用，应合成同一工具调用的不同阶段，不是两次调用。

`session-store.db` 本机有 sessions、turns、checkpoints、assistant_usage_events、search_index 等。适合补充标题、cwd、usage，以及 events 缺失时的有限降级；不能把 turns 摘要当成具有完整工具细节的无损正文。

JSONL 与 SQLite 同时存在时，以同一原生 ID 归并。数据库是可选辅助来源，字段探测优于硬编码某个 schema_version。

SDK 文档证实持久化/列举/恢复会话能力，但其展示的 checkpoints 布局与本机实际 events 布局并不完全相同。历史 viewer 不必为了离线查看启动 SDK server。

### 4.3 pi：事件树，不是单条线性聊天

默认路径：

```text
~/.pi/agent/sessions/--<encoded-cwd>--/<timestamp>_<uuid>.jsonl
```

以 header 中的 `cwd` 和 `id` 为准，不从目录名反推路径。支持 root 配置、环境变量和自定义 session dir。

- header：`type=session`、version、id、timestamp、cwd、可选 parentSession。
- 普通消息 entry：`type=message`，真正角色在 `message.role`。
- 内容块为 text、thinking、image、toolCall；toolResult 是 message role，不是 Claude 风格的 tool_result 块。
- 顶层时间常为 ISO，内层 `message.timestamp` 为 Unix 毫秒；现有前端把数字时间乘以 1000，必须修正。
- 保存所有 entry 的 id/parentId，包括 model_change、thinking_level_change、compaction、branch_summary、custom、custom_message、label、session_info。
- 读取 session_info 的 name 作为标题；没有则回退到第一条真实用户内容。
- 保留 compaction 前历史，不因为上下文压缩而删掉旧对话；retainedTail 不应重复计成新对话。
- 兼容 legacy v1/v2/v3，但 importer 不调用会自动迁移并写回原文件的 API。

本机暂未观察到多子节点分叉，但 pi 官方格式明确支持 `/tree` 原地分支，所以模型必须从第一版保存关系。

UI 可以先有“全部记录（物理顺序）”和“所选分支”两个模式。离线恢复的默认 leaf 可按官方 SessionManager 的文件索引行为，从最后一个非 header entry 开始向 parent 回溯；这不代表可以知道仍在运行进程中尚未落盘的临时选择。

若隐藏元数据，不能断开消息树关系：保留原始关系，展示时再寻找最近可见祖先。

pi 自带 `/export` HTML/JSONL，可用于人工对照，但不适合让每次同步都调用 CLI 导出。

### 4.4 VS Code Chat：必须重建对象状态

Windows 默认位置：

```text
%APPDATA%/Code/User/workspaceStorage/<workspace-id>/chatSessions/*.json
%APPDATA%/Code/User/workspaceStorage/<workspace-id>/chatSessions/*.jsonl
%APPDATA%/Code/User/globalStorage/emptyWindowChatSessions/*
```

还应支持 Insiders、profiles、自定义 user-data-dir；旧版 `workspaceStorage/no-workspace/chatSessions` 可作兼容来源。本机 `Code/User/emptyWindowChatSessions` 目录为空，不应把它误当成官方 globalStorage 位置。

`workspace.json` 帮助获得 cwd；需支持 file URI、Windows drive、multi-root workspace 和无法访问的 remote URI。不同来源的相同项目应按明确路径规则归并，不靠名称猜测。

#### JSON 与 JSONL 的差别

JSON 是会话状态；JSONL 是 object mutation log：

| kind | 官方语义 |
| --- | --- |
| 0 | Initial，全对象初始状态 |
| 1 | Set，在 `k` 路径替换属性 |
| 2 | Push，在数组追加；如存在 `i`，先把数组长度改为 i，再追加 v；v 可省略，表示只截断 |
| 3 | Delete，删除/清空属性 |

路径可混合字符串和数字索引，并可超过三层。必须按顺序重放后解析 `requests[]`，而非逐行制造消息。

对照官方 `objectMutationLog.ts` 后发现：

- AI Control Plane 当前简化解析把 kind 1/2 近似处理成赋值，只覆盖浅层路径，不能直接当作完整重放器。
- 调研时 agentsview 的 jsonlPush 带 i 路径采用“替换 len(items) 个元素，再保留后缀”的方式，与当前官方“截断后追加”不同。
- agentsview 会投影掉部分 `resultDetails.output`；如果目标是保留原始详情，不能直接照搬这种有损裁剪。

因此第三方代码可参考架构、发现和响应块映射，但补丁重放必须以官方实现及本项目 fixture 为准。

#### 响应解析

每个 request 包含用户 message 和 response chunk 数组；统一模型应保留顺序，支持 markdown、thinking、工具调用、引用、编辑、确认/问题以及错误状态。不存在的工具输出、时间或 token 数据应标记 unknown，不推算成事实。

VS Code 迁移可能保留原副本。本机已有重复 session ID，必须以原生 ID + source instance 归并，并保留多个 source file 的来源信息。CLI/IDE 共享或嵌入的会话通过明确 ID 关联，不能按文本相似度硬合并。

`state.vscdb` 可以补充索引/工作区信息；不应该第一步就从整个 state DB 里搜所有字符串。另有 `GitHub.copilot-chat/transcripts` 等辅助记录，需要与主存储去重并单独评估，不应全部无差别导入。

## 5. GitHub 同类项目对比

许可证以实际文件为准；`null` 表示 GitHub 未识别，不等于已确认无许可证。星数/版本变化快，本报告不以此排名。

| 项目 | 已核对的用途/覆盖 | 借鉴重点 | 限制/许可证 |
| --- | --- | --- | --- |
| [kenn-io/agentsview](https://github.com/kenn-io/agentsview) | 多源本地索引、浏览、搜索、统计；明确包含 Claude、Codex、Copilot CLI、pi、VS Code Copilot | provider/解析器分离，稳定来源前缀，纳秒文件状态，工具配对，FTS5、SSE，测试 fixtures | Go + Svelte，不直接移植整个应用；MIT；部分新格式和保真规则仍需核对 |
| [l-teles/ai-control-plane](https://github.com/l-teles/ai-control-plane) | 本地 Python/Flask/SQLite Web UI；Claude、Copilot CLI、VS Code Chat | Python parser、统一 conversation、DAG、SQL migrations、内容搜索、Windows/Insiders 发现 | 未列 Codex/pi；VS Code 补丁重放不完整；MIT |
| [ctxrs/ctx](https://github.com/ctxrs/ctx) | 多源历史索引/检索 CLI；Claude、Codex、Copilot CLI、pi 等 | 原始记录 citation、稳定事件身份、只读导入、原子发布、边界/lineage、Codex item_completed fixtures | Rust/Tantivy，不是三栏 Web viewer；功能宣称/性能数字未独立实测；Apache-2.0 |
| [jazzyalex/agent-sessions](https://github.com/jazzyalex/agent-sessions) | macOS 本地浏览/搜索/恢复；包含目标四类 CLI agent | PiSessionParser、CopilotSessionParser、schema-drift/golden fixtures、恢复命令、图片与 usage UI | Swift/macOS；不能直接作为 Windows viewer；MIT |
| [dhruv-anand-aintech/agent-session-viewer](https://github.com/dhruv-anand-aintech/agent-session-viewer) | Node 本地多平台 Web viewer；README 包含 Claude、Codex、pi 和其他来源 | source filter、按项目分组、SSE、工具卡、归一化导入格式 | 未确认目标 Copilot 两路原生解析覆盖；云/远程/Agent Console 功能超出需求；许可证未由 GitHub 识别，复制前核验 |
| [openai/euphony](https://github.com/openai/euphony) | OpenAI 官方 Harmony/Codex 浏览器查看器 | Codex 检测、内容渲染、metadata、可嵌入 Web Components | 不是自动扫描的多源数据库；main 的样本不能证明覆盖本机新 paginated 格式；Apache-2.0 |
| [axsaucedo/copilot-chronicle](https://github.com/axsaucedo/copilot-chronicle) | 纯浏览器 Copilot CLI JSONL timeline | 原生事件类型、过滤、详情、UTC/本地时区、复制 | 手动加载文件，不是跨源索引；README 声明 MIT，GitHub API 未识别，复制前核验 |
| [simonw/claude-code-transcripts](https://github.com/simonw/claude-code-transcripts) | Python 工具，把 Claude 历史生成分页 HTML | 本地导出、分页、工具细节、项目归档、可分享但不强制上传 | Claude 专用；README 明示部分 web API 命令已失效；Apache-2.0 |
| [beaugunderson/obliscence](https://github.com/beaugunderson/obliscence) | Claude Code/pi/claude.ai 历史索引；SQLite、FTS5/BM25、sqlite-vec | provenance、来源过滤、本地语义检索、长消息分块、幂等导入 | CLI/检索导向；README 功能核对，未审阅核心 parser/许可证；不建议第一版引入其语义依赖 |
| [andyfischer/ai-coding-tools: claude-history-tool](https://github.com/andyfischer/ai-coding-tools/tree/main/claude-history-tool) | Claude 桌面历史浏览器 | 项目分组、原 JSON/工具详情、token/analytics 体验 | Electron/Claude 专用；README 声明 MIT |
| [Dicklesworthstone/coding_agent_session_search](https://github.com/Dicklesworthstone/coding_agent_session_search) | 搜索时识别的多源历史检索候选 | 仅记录为候选，不建议直接引入 | LICENSE 是带 OpenAI/Anthropic 限制条款的 MIT 变体，**不是普通 MIT**；本次未进一步审阅/移植其实现 |

另有同名 PyPI `agent-session-viewer` 包，描述是 Claude/Codex 的 Python viewer。其 PyPI 元数据未提供明确仓库链接，不能与上述 Node 同名项目混为一谈，所以不作为主要代码参考。

## 6. 适合当前项目的接入设计

### 6.1 分层，不让前端理解五套原生格式

```text
原生文件 / 只读原生 SQLite
            ↓
source adapters（发现、解析、checkpoint、能力声明）
            ↓
统一 events/messages + 原始 records + 来源定位
            ↓
viewer 自己的 SQLite / 搜索索引
            ↓
Flask API
            ↓
现有三栏 UI + 来源/项目/日期/分支筛选
```

建议目录：

```text
sources/
  base.py
  claude.py
  codex.py
  copilot_cli.py
  pi.py
  vscode_chat.py
models.py
sync.py
migrations/
tests/fixtures/{claude,codex,copilot_cli,pi,vscode_chat}/
```

adapter 至少提供 `discover_sessions()`、`read_metadata()`、`parse_records()`、`capabilities()`。append checkpoint 为可选能力，不能强迫 VS Code 的状态重放遵循 Claude 的追加消息策略。

需要区分运行工具和模型供应商：**pi 使用 OpenAI/Copilot 模型仍是 pi 会话**；VS Code 使用 Claude 模型也不应标成 Claude Code。

### 6.2 统一模型

会话建议字段：

```text
id                     viewer 稳定 ID，例如 pi:<native_uuid>
source                 claude / codex / copilot_cli / pi / vscode_chat
source_instance        可选：多个 home/环境/编辑器 profile 的命名空间
external_id            原生 thread/session ID
project_path           真正 cwd/工作区路径，可为空
project_display_name   UI 展示名，不作为唯一身份
name / started_at / updated_at
parent_session_id / relationship_type
metadata / capabilities
```

事件/消息建议字段：

```text
id / session_id / source_record_id / source_subindex
native_event_id / parent_event_id / order_index
kind                   user / assistant / tool_call / tool_result /
                       thinking / system / compaction / lifecycle / unknown
role                   与 kind 分开，混合内容不强制只有一种类型
timestamp_ms           统一 UTC Unix 毫秒，可为空
content_blocks         text / thinking / image / tool_call / tool_result / ...
text_for_search
model / provider / usage
turn_id / tool_call_id / status
raw_record_ref / source_locator
```

- 一个原生记录可生成多个块/事件，使用确定性的 subindex，不丢掉正文+工具混合内容。
- 原生 ID 优先；无 ID 时使用可追溯记录位置/格式特定序号，不用内容去重。
- pi 短 entry ID 需要 session 命名空间，不能当全库唯一 ID。
- `raw_records` 保存原 JSON，normalized rows 引用它，避免原 JSON 在每个拆分内容块重复一份。
- `source_files` 保存一对多路径/文件指纹，处理迁移副本、revert、sidecars、主/子代理。
- 保存原生 DAG 边，不因为隐藏 metadata 而丢失 parent；UI 关系为投影。
- 为缺失工具输出、时间、模型、usage 明确记录 unknown/truncated，不伪装成完整数据。

### 6.3 同步与存储

当前同步以秒级 mtime + DB 消息 COUNT 跳过物理行，并假设只追加；新来源不能复用这个假设。

建议：

1. 第一版对发生变化的**单会话完整重解析**，在一个事务中替换其归一化投影。先保证正确，再优化尾部读取。
2. 记录 `mtime_ns`、文件大小、格式版本、parser version、内容/前缀指纹；只用 mtime 不足以发现同长度改写。
3. JSONL 稳定追加可记 byte offset，**只提交完整记录**。半行不推进 checkpoint，下一轮重试。
4. 文件截断、替换、格式升级或校验不匹配时全量重建该 source/session。
5. VS Code 在 MVP 阶段始终完整重放已变化文件；后续才能持久化对象状态与 offset。
6. metadata、raw records、归一化消息、搜索索引和 checkpoint 同事务提交，失败回滚并显示错误，不更新成功水位。
7. 单写入同步任务 + 进度/错误报告；只读查看继续服务。根据规模采用 WAL、busy_timeout、分页。
8. 对原生 SQLite 使用只读连接且尊重 WAL。不要复制正在写入的 `.db` 主文件就当作完整快照，不设置会写回源库的 PRAGMA，不自动做 migration/checkpoint。
9. 源文件消失时标记 unavailable/deleted；是否保留 viewer 归档由明确策略决定，不能靠下次扫描自动物理删档。

当前 `claude.db` 较大。建议先用 SQLite backup API 备份，再在新 schema/新数据库中并行验证新 importer；验收后切换。业务代码尚未实施迁移，不应先覆盖现有数据库。

### 6.4 搜索与前端

- 搜索只索引提取出的文本/工具参数/结果，不再对整份 raw_json 做无索引 LIKE 全表扫描。
- FTS5 可以用于英文 lexical/ranking，但默认 unicode61 对中文子串并不等价于当前 LIKE。必须专门测试中文：可评估支持版本下的 trigram 索引，或分词/字符方案，并保留明确的 substring fallback。
- 不把“全文搜索”直接等同于默认 FTS5 可无损支持所有语言。
- 搜索返回匹配消息、摘要和定位，不只返回会话 ID。
- 会话和消息 API 分页；前端至少分段渲染，后续再评估虚拟列表。
- 左栏来源标签、来源过滤、项目归并、名称/首问预览；中栏事件过滤、分支/子代理关系；右栏 normalized 详情 + 原始记录。
- 统一时间为 ms 后删除目前“所有数字都是 Unix 秒”的隐式假设。
- 所有外部内容视为不可信：属性值和文本分别转义，Markdown/HTML 严格清洗；图片和文件访问限制在已授权源目录，不支持任意本地路径读取。
- 活跃 Agent 持续追加时可后续增加文件 watcher + SSE；初版保留手动 Reload 也能满足浏览需求。

建议 v2 API：

```text
GET  /api/sources
GET  /api/sessions?source=&project=&q=&cursor=
GET  /api/sessions/<id>/messages?cursor=&limit=&branch=
GET  /api/messages/<id>/raw
POST /api/sync
GET  /api/sync/status
```

同步结果明确返回 `{added, updated, unchanged, failed, errors}`，修复现有前端期待统计、后端却返回数组的问题。旧 API 可短期保留兼容。

### 6.5 删除安全

**不要给新来源复用现在的 Claude 删除逻辑。**

- source capabilities 明确 `supports_source_delete=false`，新来源第一版只读。
- “隐藏/移除 viewer 索引”必须有 tombstone，否则同步后又出现。
- 原生 SQLite 有自己的索引和外键，直接删 JSONL 不一定能正确删除 Codex/Copilot 会话。
- 原始删除作为后续独立功能，优先官方 API/CLI、确认 active 状态、预览影响、可恢复备份/回收站。
- 路径授权使用解析后的目录包含关系，不用字符串 startswith；检查 agent sidecar、symlink/junction 和 root 本身。
- 即便仅监听 localhost，也考虑 Origin/Host 校验和 mutation token，尤其当前已有真实删除接口。

## 7. 实施顺序与验收

### 阶段 A：先建立共同基础

- 备份/版本化 schema，增加 source identity、source_files、raw_records、events/blocks。
- 抽出 Claude adapter，保持现有浏览结果；新来源默认只读。
- 合成 fixtures + pytest，所有测试用临时目录/临时数据库。
- 同步事务和失败报告、毫秒时间、基本分页。

### 阶段 B：接入 CLI 历史

1. **pi**：格式清晰且已有本地正式文档，先实现树关系/内容块。
2. **Codex**：包含 legacy/paginated、item_completed、工具 pairing、子代理上下文边界，不只实现旧 response_item。
3. **Copilot CLI**：事件流、工具生命周期、model snapshot 排除、可选 metadata store。

阶段结束目标：四类 CLI 来源同页浏览、来源筛选、项目筛选、正文/工具/思考详情、跨源搜索；同步幂等且不改任何源文件。

### 阶段 C：接入 VS Code Chat

- 官方 mutation replay，任意深度路径、截断与空 v、Delete、半行恢复。
- JSON/JSONL、工作区/global、Stable/Insiders/custom roots、重复副本。
- response chunks 和不同 agent/model，关联但不误合并 Copilot CLI。

### 阶段 D：按使用需求增强

- FTS/CJK 索引和搜索定位优化。
- 文件 watcher/SSE、虚拟列表。
- HTML/Markdown 导出、复制恢复命令、使用量统计。
- 语义检索、云同步、原始删除不列入 MVP。

### 必测清单

- 同一原生 session/event ID 在不同 source 不冲突；同一 source 重复导入无新增。
- 同一个问题连续问两遍不被删；snapshot/replay 不制造假重复。
- 一条 assistant 同时有文本、thinking、多个工具调用时全保留。
- 工具 complete 先/后到达、失败/中止、结果是对象/数组，均可追溯。
- JSONL 最后半行、坏行、截断、同大小重写、同一秒更新、格式升级。
- 单会话事务失败后数据库/checkpoint 不出现半成品。
- Codex root session_id 与 thread id 不混淆；subagent inherited prefix 不当作新发言；history_base/revert 关系能保留或明确标记未支持。
- pi 分叉经隐藏 metadata 仍能回到正确父消息；compaction/retainedTail 不重复生成对话。
- VS Code Push 带 i 截断、v 缺省只截断、超过三层路径、Delete、多文件同 session。
- 英文/中文子串、标点、SQL/FTS 特殊字符、工具参数搜索。
- HTML/属性注入、恶意 Markdown、路径越界、symlink/junction。
- 检查 importer 前后源文件 hash 不变；测试不能调用真实删除方法。
- 用脱敏合成 golden fixtures 做可读输出对照，不把本机真实历史提交到 Git。

## 8. 决策建议

**继续扩展当前 viewer，比替换整套技术栈更符合“统一浏览本机历史”的目标。** Flask/SQLite 足够，真正的难点是 source semantics、幂等和保真，不是选择前端框架。

如果希望完全停止维护自己的项目，agentsview 是应首先做本地只读试用的候选；但本次没有运行验证，不能保证它在本机当前 Codex/VS Code 格式上无缺失。

如继续自建，建议重点阅读 AI Control Plane 的 Python 分层和 migrations、agentsview 的 source/provider 接口与测试、ctx 的原始定位/lineage，然后独立实现小而正确的 adapters。不建议直接把这些大型项目的全部功能搬进来。

## 9. 关键源码与文档链接

### 官方/第一方格式

- Codex session identity/history boundaries：[protocol.rs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs)
- Codex response items：[models.rs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/models.rs)
- Codex completed turn items：[items.rs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/items.rs)
- Copilot SDK persistence：[session-persistence.md](https://github.com/github/copilot-sdk/blob/main/docs/features/session-persistence.md)
- Copilot event schema：[session-events.ts](https://github.com/github/copilot-sdk/blob/main/nodejs/src/generated/session-events.ts)
- pi session specification：[session-format.md](https://github.com/earendil-works/pi-mono/blob/main/packages/coding-agent/docs/session-format.md)
- 本机实际 pi 文档：`D:/Repos/AI/pi/packages/coding-agent/docs/session-format.md`；已完整阅读。
- pi leaf/index 行为：[session-manager.ts](https://github.com/earendil-works/pi-mono/blob/main/packages/coding-agent/src/core/session-manager.ts)
- VS Code 存储位置/迁移：[chatSessionStore.ts](https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/chat/common/model/chatSessionStore.ts)
- VS Code 补丁权威语义：[objectMutationLog.ts](https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/chat/common/model/objectMutationLog.ts)
- VS Code 会话投影：[chatSessionOperationLog.ts](https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/chat/common/model/chatSessionOperationLog.ts)

### 最有价值的第三方参考入口

- agentsview：[parser 目录](https://github.com/kenn-io/agentsview/tree/main/internal/parser)、[pi.go](https://github.com/kenn-io/agentsview/blob/main/internal/parser/pi.go)、[copilot.go](https://github.com/kenn-io/agentsview/blob/main/internal/parser/copilot.go)、[vscode_copilot.go](https://github.com/kenn-io/agentsview/blob/main/internal/parser/vscode_copilot.go)
- AI Control Plane：[parser.py](https://github.com/l-teles/ai-control-plane/blob/main/src/ai_ctrl_plane/parser.py)、[vscode_parser.py](https://github.com/l-teles/ai-control-plane/blob/main/src/ai_ctrl_plane/vscode_parser.py)、[migrations](https://github.com/l-teles/ai-control-plane/tree/main/src/ai_ctrl_plane/migrations)
- ctx：[provider-support](https://github.com/ctxrs/ctx/blob/main/docs/provider-support.md)、[Codex paginated fixture](https://github.com/ctxrs/ctx/blob/main/tests/fixtures/provider-history/codex-paginated-item-completed.jsonl)、[Codex projection](https://github.com/ctxrs/ctx/blob/main/crates/ctx-history-provider-codex/src/codex/nativepath/reader/project.rs)
- Agent Sessions：[PiSessionParser.swift](https://github.com/jazzyalex/agent-sessions/blob/main/AgentSessions/Services/PiSessionParser.swift)、[CopilotSessionParser.swift](https://github.com/jazzyalex/agent-sessions/blob/main/AgentSessions/Services/CopilotSessionParser.swift)、[fixtures](https://github.com/jazzyalex/agent-sessions/tree/main/Resources/Fixtures)
- CASS 特殊许可证：[LICENSE](https://github.com/Dicklesworthstone/coding_agent_session_search/blob/main/LICENSE)
