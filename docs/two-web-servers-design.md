# Agent Session Viewer：本地与云端双 Web Server 设计

**状态：已实施（云端 ingest 协议 6）。** 本文取代此前“无状态 daemon + 云端任务队列”
的运行方式。实施时保留了原有数据库/历史，不自动清洗旧数据。

实施要点与本文的细化：

- 变更操作为 `session`、`message`、`delete_message`。删除只来自本地投影的合法替换
  （例如 VS Code 请求重新投影），不会因为来源文件消失而产生。
- 会话在云端命名为 `<datasetId>/<source>:<id>`；云端不保存本地路径和导入游标。
- 第 6 节的 `pending_batches` / `upload_runs` 合并为 `cloud_connection.pending` 与
  `last_result`；`upload_receipts` 按 throughSeq 记录。
- 旧协议 5 云库不被打开；新部署默认 `/data/cloud-v6.db`。
- V1 不压缩变更日志（第 6.1 节的压缩/bootstrap 留待后续）。

---

## 1. 已确定的目标

最终只有两个 Web 应用：

| 应用 | 位置与用途 | 登录 | Docker |
|---|---|---|---|
| 本地 Web Server | 用户电脑；采集本地历史、入本地库、查看历史、控制上传 | **不需要密码**，仅 loopback | **不需要** |
| 云端 Web Server | 中心归档与内容分析；查看云端数据、管理上传 Key | 控制台必须**密码登录** | 提供本机直连和 VM + Cloudflare 两种配置 |

核心流程：

```text
Agent 原始历史（本地只读）
          ↓ 本地游标增量采集
     本地业务数据库
       ├─→ 本地历史查看网页，无密码登录
       └─→ 本地控制台：填写云端地址 + API Key
                      ↓ 用户手动点击同步
                  HTTPS 批量 API
                      ↓
                  云端数据库
                      ↓
             云端历史页 / 控制台，密码登录
```

**明确不做：**

- 没有独立 daemon，没有云端派发任务/任务领取/收到确认/远程执行权流程。
- 没有本地历史监控、文件 watcher、定时扫描、定时上传、后台任务轮询或 heartbeat。
- 云端不能让本地执行命令、修改扫描目录或读取任意文件。
- 本地数据不会因云端不可达、Key 错误或云端重启而无法查看。
- 不把原始 SQLite 文件或完整 Agent 原始记录作为上传协议。

本地控制台的“开始同步”可以启动一个按需工作线程，供页面查看进度及暂停/取消；
这是一次用户操作，不是常驻监控任务或云端任务队列。工作完成后线程退出。

## 2. 目录与职责

建议保留现有 `server/` 代表云端、`local/` 代表本地，移除 `deamon/` 的运行入口：

```text
repo/
├── README.md                         # 双端总览及快速开始
├── docs/
│   └── two-web-servers-design.md
├── server/                           # 云端 Web Server
│   ├── app.py                        # 云端网页、控制台、接收 API
│   ├── auth.py                       # 密码登录、session、CSRF
│   ├── keys.py                       # 上传 Key 管理
│   ├── ingest.py                     # 分析消息批量接收、幂等、上传游标
│   ├── storage/                      # 云端数据库访问
│   ├── contracts/                    # 纯数据协议与分析内容约束
│   ├── static/                       # 云端历史页/控制台
│   ├── deploy/                       # 两套 Docker 配置及部署文档
│   ├── requirements.txt
│   └── tests/
├── local/                            # 本地 Web Server
│   ├── app.py                        # 本地历史页、控制台及本地 API
│   ├── importer/                     # 五种来源、增量游标、分析投影
│   ├── storage/                      # 本地消息、采集进度、上传进度
│   ├── cloud_client.py               # HTTPS、API Key、连接复用、重试
│   ├── uploader.py                   # 从本地 DB 上传，不扫描 Agent 文件
│   ├── static/                       # 本地历史页与云同步控制台
│   ├── requirements.txt
│   ├── README.md                     # 本机安装/启动说明，无 Docker 要求
│   └── tests/
└── tests/                            # 跨端集成和 HTTP/Docker/浏览器 E2E
```

上述文件拆分是职责示意，实施时可按模块大小合并，不为目录而增加抽象。
`server/contracts/` 可继续作为双方引用的纯 Python 数据契约，不导入 Flask/SQLite。
两端均从 repo 根目录启动；若将来分别打包，再把契约提取成独立包。

复用方向：

- 复用原本地版的来源识别、增量验证和启动前导入顺序。
- 复用现有分析内容投影、HTTP 连接复用、批量大小控制和幂等原则。
- Deamon 内有用的 HTTP/解析组件归入本地应用；领任务、worker lease、队列与控制轮询移除。
- 云端只管理 Key/数据/上传进度，移除任务控制页面与远程任务 API。

## 3. 两端的数据口径

### 3.1 业务消息均使用分析口径

新的本地业务库与云端业务库只记录四类：

| 类型 | 内容 |
|---|---|
| 用户消息 | 用户正文 |
| 主 Agent 回答 | 回答正文，不含 thinking/reasoning |
| 工具 / subagent 调用 | 工具名、调用 ID、命令、完整参数/任务描述 |
| 工具结果 | 关联调用 ID、成功/失败，不含结果正文、详细错误和日志 |

其余规则延续已确认的要求：

- 不记录系统事件、thinking/reasoning、附件、工具过程和 subagent 内部对话。
- Session 级标题、来源、工作目录、模型/provider、用量等 metadata 可以保留。
- 每条消息包含时间戳及来源；优先原时间，只有请求/会话时间则明确标注，完全缺失时
  使用文件修改时间并标记 `file_mtime`，不伪装为真实事件时间。
- 工具来源没有可靠成功/失败信息时，标记 `unknown` / null，不从结果文字猜测。
- 不做敏感词脱敏：正文/调用参数中的凭据、签名、代码、路径不会自动遮罩。
  因而本地用户必须明确批准采集目录和云端上传。
- 过滤是按消息/附件类型选择数据，不是按“看起来敏感”的字段名删除参数。
- 原始历史仅作为本机只读输入；新业务库不另存完整原始事件或附件。

### 3.2 最小解析状态不是历史消息

持久化游标/必要解析状态用于增量采集，不作为系统事件展示，也不能成为隐藏的内容上传通道。
允许保存：偏移、行号、前缀摘要、格式/解析器版本、活跃 turn/request/call ID、
消息关联/顺序、保留正文/调用参数的必要投影、request 摘要。

禁止在这些状态里另外保存被排除的 thinking、完整结果、系统提示、附件或过程日志。
例如 VS Code mutation log：

- 对允许的数据维护必要请求投影；响应数组的被排除 slot 可保留类型/位置占位，
  不能因为删除数组元素导致后续 mutation 索引错位。
- 工具结果等被排除子树可作为 opaque 标记，忽略只修改该子树的操作。
- 若新操作使旧的被排除数据成为投影所需内容，或状态结构不兼容，允许在**内存**
  重放该单文件的安全前缀，重新生成分析投影；不得把重放内容写入业务库/上传。
- 不能正确重放时保留旧游标并报错，不跳过记录“继续成功”。

这部分是实现验收重点：不能只过滤消息表，却把原始内容放进 parser state、
change log 或待上传记录中。

## 4. 本地服务启动顺序

```text
加载本地配置 / 打开本地 DB / schema 检查
    ↓
读取已保存的来源游标
    ↓
逐文件发现 → quiet-period 检查 → 增量读取 → 分析投影
    ↓
提交消息 + 最小解析状态 + 来源游标 + 本地变更日志
    ↓
本轮扫描完成（可带逐文件警告）
    ↓
启动本地 HTTP server
    ↓
提供历史查看页和云同步控制台
```

具体约束：

1. 启动前不绑定 HTTP 端口；不能先展示空页面再悄悄全量导入。
2. 默认仍暂缓最近一小时改动的文件/sidecar，不读正文、不推进它们的游标。
3. 逐文件处理，没有全目录正文预读屏障；一个文件的安全单位提交后才前进。
4. JSONL 仅推进到格式允许的完整安全边界。截断/改写时进入该文件的核对流程，
   不把更短源文件当成删除云端历史的命令。
5. JSON 快照按请求/投影摘要对比，正文变化更新对应消息，不持久化完整原始快照。
6. 单文件失败保留旧数据/游标，记录错误，继续其他文件；枚举不完整不能把整片来源标缺失。
7. 本轮尝试完成后可带警告启动网页，显示本地导入摘要及失败项。
8. 数据库损坏、schema 不兼容、存储无法安全提交属于致命错误：启动失败，不能偷偷
   创建新空库覆盖或改连另一数据库。
9. 启动流程不要求访问云端。没有网络/未配置云连接仍可本地采集和查看。
10. 启动不自动上传。云同步由本地控制台明确发起；上次中断仅显示“可继续”，不自动重试常驻。

可保留“手动重新导入本地历史”按钮作为次要功能；不启用 watcher 或定时任务。
若需要仅浏览，提供显式 `--no-import`，默认行为仍为先导入再监听。

## 5. 必须分开的两套进度

### A. 来源 → 本地库：采集游标

按来源文件保存，例如：

```text
source / file identity / path
file generation / dependency fingerprints
safe byte offset / next line / verified prefix hash
parser version / minimal scope state / last error
```

它表示哪些来源记录已经**可靠转换并存进本地库**。由本地采集器维护。

### B. 本地库 → 云端：上传游标

按连接及 dataset 保存，例如：

```text
connection identity = cloud origin + cloud instance + client slot + dataset UUID
cloud-confirmed change sequence
pending batch range + immutable payload hash + idempotency key
last receipt / last sync result
```

它表示哪些本地分析变更已在云端提交。由上传器在确认云端回执后维护。

**导入成功不等于上传成功；原始文件 offset 不能作为云上传 offset。**
用户可以长时间离线导入，再完全从本地库上传；上传过程不重新扫描 Agent 文件。
云端的已提交游标用于上传对账，不参与本地来源文件扫描。

## 6. 本地数据模型（建议）

| 表/实体 | 用途 |
|---|---|
| `sessions` | 分析 session 与 metadata，来源不可用也不删历史 |
| `messages` | 四类当前消息、稳定 ID、时间戳、内容/状态、实体 revision、顺序 |
| `source_files` | 文件身份、依赖、指纹、代际、来源游标、版本和错误 |
| `projection_state` | 不含排除内容的最小 turn/request 解析状态 |
| `change_log` | 不可变的本地分析变更序列，用于可靠增量上传 |
| `cloud_connection` | 云地址、实例、client slot、dataset ID、Key 引用、确认位置 |
| `pending_batches` | 当前连接的固定批次范围、hash、key，不需复制整文件 |
| `upload_receipts` | 已确认回执/范围，供审计和恢复 |
| `upload_runs` | 用户这次同步的本地执行状态、计数、错误、暂停/中断标志 |

本地采集的一次事务同时：

```text
更新消息/metadata → 写不可变 change_log → 更新解析状态/来源游标 → commit
```

不能出现“游标已前进但消息/上传变更没有保存”。

### 6.1 不可变变更序列

采用每个本地 dataset 单调增加的逻辑 `seq`，消息更新也产生新变更，不只记录新增。
发布序号应由同一事务内的 dataset counter 连续分配，不能把会产生跳号的
`INSERT OR IGNORE` / 内部 rowid 副作用当成协议序号；已确认的旧日志压缩不改变后续序号。
变更包括：

```text
upsert_session
upsert_message（含工具调用）
upsert_message（工具结果由 unknown 更新为 success/failure）
set_source_available
```

`change_log` 保存提交时的**分析内容快照**，不是只引用会继续变动的当前 message 行。
这样原始文件或当前消息后续变化，也不会改变已经准备重试的 HTTP 内容。
日志不允许夹带原始记录/被排除内容。

跨多条消息的必要原子变更使用一个 `group_id`（例如一条来源事件或一个请求的投影），
上传不在 group 中间推进确认位置。不是整个文件一个 group，更不是上传包/分块协议。
首次大量导入应按安全投影单位分段形成本地 commit groups；JSON 快照按请求处理，
只有完整完成当前快照后才推进其整文件完成指纹。重启可依靠已提交 request 摘要继续。

历史保留/清理规则：

- 原始来源删除/不可读只更新 availability，不自动产生消息删除或云端删除。
- 合法消息编辑可更新同一稳定 ID 的 revision；原始 ID 缺失且文件改写时使用新代际，
  不把不同内容错误覆盖到旧消息上。
- V1 不提供自动清除云端历史的动作。
- change log 只能在所有依赖它的连接已确认、且没有 pending batch 引用后压缩/清理。
  如有新云端从零同步需求，使用受控的分析快照 bootstrap，不要求永远保留所有已确认日志。

## 7. 本地控制台

本地无密码登录，打开本机页面即可使用。

### 7.1 连接配置

用户填写：

- 云端地址，例如 `https://history.example.com`。
- 云端控制台生成的 API Key。

提供：测试连接、保存、替换 Key、断开/删除连接配置。UI 回显地址、Key ID/已配置
状态，不回显完整 Key。

测试连接先验证 TLS、API 版本/权限、cloud instance 和 client slot，不上传历史。
保存连接不会触发自动上传；用户还要点击“同步到云端”。

V1 先提供一个活动云端连接。存储按连接身份隔离进度，不能因改地址或换一把 Key
就盲用旧游标；Key 轮换若仍属于同一 client slot，应在对账后保留进度。
云实例变化或 Key 绑定不同 dataset 时阻止上传，要求用户明确处理。

Key 保存优先使用操作系统凭据存储；不可用时才使用 owner-only 的本地敏感文件，
DB 保存引用。这个 fallback 必须在文档说明，不能宣称普通 SQLite 是加密存储。
Key 不入日志、URL、浏览器 localStorage、云同步内容或默认备份包。

### 7.2 操作及状态

主要展示：

- 本地入库时间、会话/消息数、来源导入错误。
- 云端连接/授权状态、上次同步结果。
- 本地最大变更 seq、云端已确认 seq、待上传量。
- 当前上传批次、字节数、重试/错误、已完成数量。

操作：开始同步、继续中断同步、暂停、取消本次操作。暂停/取消在批次边界生效，
遇到提交结果不明确时先查云端/重试同批，不直接把未知结果当成失败未提交。

同一活动连接只允许一个 uploader。重复点开始返回当前本地 `upload_run`，不建立
云端队列，也不同时开两个上传线程。完成后退出，不因页面打开而常驻重试。
页面可以轮询**本机**状态，这不是本地进程对云端的 heartbeat。

## 8. 云端控制台与 Key

### 8.1 密码登录

云端查看页、控制台、搜索/读取 API 均需 Owner 密码会话。上传 API Key 不能用于
登录控制台，也不能获取其他客户端的历史。

- 首次初始化从私有密码文件（优先）或显式 bootstrap secret 设置管理员密码。
- DB 只保存加盐 scrypt/Argon2id 密码摘要，不保存可逆/明文密码。
- 后续重启不自动用环境变量覆盖已设置密码；更改/重置是明确的管理操作。
- 独立 session signing secret 持久化，HTTPS 下 Cookie `Secure`、`HttpOnly`、
  `SameSite`；写操作有 CSRF，登录有限流。
- 保留同源 Referrer/Origin 的正确配置，避免表单 POST 的 opaque Origin 问题。
- 密码、API Key 和 Cloudflare Tunnel token 不复用。

### 8.2 生成上传 API Key

Owner 在云端控制台创建 Key，填写名称（例如“工作电脑”），可设置过期时间。
Key 仅显示一次；支持撤销/轮换，轮换保持 client slot 方便续传。

建议 Key 形式为不可预测高熵 secret + 可查询的非敏感 Key ID。服务器只保存 hash，
验证用常量时间比较。权限限定为该 client slot/dataset 的上传和进度查询，不包含
用户/Key 管理、任意文件读取或通用 SQL/命令执行。

client slot 是云端授权命名空间，dataset UUID 是本地数据库数据集身份。首次连接
显式绑定；同一 Key 碰到另一个新 dataset 不能自动覆盖原数据或沿用旧 seq。
不同机器上的同 UUID 来源会话也不能互相覆盖。

云端控制台只展示数据、客户端标识、最后上传、Key、进度/回执。**不再有远程任务
队列、Enqueue/Pause/Run 指令、worker lease、heartbeat 或在线监控页面。**

## 9. 本地 → 云端完整同步流程

### 9.1 开始与对账

1. 用户在本地控制台点击同步；本地建立/恢复一个 `upload_run`。
2. 读取已保存连接、dataset 和本地上传位置。
3. 使用指定地址和 Key 读取云端 capabilities / 已提交位置。
4. 校验版本、授权 client slot、dataset、cloud instance，处理本地回执与云端
   head 的差异：本地少记了已确认回执可前进；不能跳过云端尚未提交的数据。
5. 固定本轮高水位 `H = local max_seq`，本轮仅上传到 H；期间新导入的变更留给
   下一次同步，避免用户的一次操作永远追着持续变化的数据跑。

### 9.2 动态组批

- 以过滤后实际分析 JSON 大小为目标，默认约 1 MiB，不固定消息条数。
- 从本地 change log 的已确认位置后取**连续** groups，不能越过未确认 group。
- 同一 POST 可以包含多个 session/来源的变更，因为来源采集早已独立完成入库；
  上传阶段不再受“正在扫描哪个 Agent 文件”的限制。
- 保持必要 group 原子性；单个 group 超目标可单独一批，不截断命令或参数。
- 服务端广告单请求硬上限（初版可沿用 32 MiB）。超过时明确报错；优先在本地
  采集/快照 bootstrap 按安全单位拆小，不恢复 chunk/seal 上传包协议。
- 固定批次 `fromSeq/toSeq/group IDs/canonical payload hash/Idempotency-Key` 到本地
  pending manifest。变更本身不可变，因此可精确重建，无需再复制整个文件或数据库。
- 持久化 pending manifest 后才发送 HTTP；后续源变化不能改用同 key 发送新内容。

V1 先做“全库分析增量上传”，不能把页面的项目筛选当成可跳过 seq 的同步过滤。
将来按项目/会话选择上传，应为独立 stream 定义独立游标，不能复用一个全局水位。

### 9.3 一个普通批量 POST

示意，不是最终字段逐字定义：

```http
POST /ingest/v1/batches
Authorization: Bearer <API Key>
Idempotency-Key: <固定批次 key>
Content-Type: application/json
```

```json
{
  "protocolVersion": 6,
  "datasetId": "local-dataset-uuid",
  "expectedSeq": 1200,
  "throughSeq": 1204,
  "groups": [
    {
      "id": "local-commit-group-id",
      "firstSeq": 1201,
      "lastSeq": 1204,
      "changes": [
        {"seq": 1201, "op": "upsert_session", "session": {}},
        {"seq": 1202, "op": "upsert_message", "message": {}},
        {"seq": 1203, "op": "upsert_message", "message": {}},
        {"seq": 1204, "op": "upsert_message", "message": {}}
      ]
    }
  ]
}
```

消息字段严格遵守四类内容/时间戳约束，示意中的空对象不可直接用于真实请求。
client slot 来自鉴权身份，不能靠 body 冒充其他上传者。

### 9.4 云端原子写入

云端在同一事务内：

1. 验证 Key、dataset、版本、body/hash/引用和大小。
2. 查 idempotency receipt：同 key + 同内容返回原回执；同 key 不同内容拒绝。
3. 比较 expectedSeq 与云端连续已提交位置。
4. 验证连续 seq / group 完整性及稳定消息 ID / revision。
5. 应用 session/message upserts，更新计数、索引和状态摘要。
6. 更新 dataset 已提交 seq，保存成功回执。
7. commit 后才返回成功。

返回 `ackSeq`、batch key/hash、cloud instance 和必要计数；没有下一条远程任务。
本地收到回执后，在本地事务更新 confirmed seq/receipt、清除 pending manifest。
随后继续下一批，至本轮 H 完成。正常不每批查询一次 progress，不发送 heartbeat。

### 9.5 超时或重启

- 云端提交成功、响应丢失：重发**同 key 同内容**，取回原回执。
- 本地成功接收后未保存回执就崩溃：pending manifest 和 immutable change log
  能重建原请求；也可对账云端 ackSeq，不能直接再做一次追加。
- 原始 Agent 文件在此期间变化：不影响这次 pending body，因为上传来自本地库的
  已提交不可变变更，而不是重新读 Agent 文件。
- 本地服务重启默认先导入，再开放网页；将中断的上传标记为可继续，等待用户点击。

## 10. 并发和浏览一致性

- 本地数据目录启动时取得 OS 单实例锁；同一 DB 的第二个本地服务拒绝启动，
  避免在 HTTP 端口绑定之前就并行导入/上传。锁随进程退出释放，不靠 heartbeat。
- 本地来源导入和上传分别有自己的互斥状态；上传单 worker，不启动常驻监控。
- DB 使用 WAL 与短事务。上传读取已提交 change log 并固定 H，本地继续导入只
  生成更大 seq，不修改待重试 body，不需要为网络请求持有本地写事务。
- 云端按 dataset 比较连续进度，重复/并发上传同一 dataset 不允许 last-writer-wins。
- 消息更新用稳定 ID + 实体 revision，旧 revision 不能覆盖新内容。同 revision
  不同 hash 是完整性错误，不猜测合并。
- 两端消息分页可带 session revision；第二页遇版本变化提示重新载入，避免混拼。
- 本地或云端备份恢复后可能出现 cloud ackSeq 大于 local maxSeq。必须阻止普通上传，
  不能回退云端游标或复用旧 seq 写新内容。V1 通过恢复完整本地备份，或显式创建新
  dataset/云端命名空间全量同步处理；不做隐式覆盖旧云数据的“自动恢复”。

## 11. 错误处理矩阵

| 场景 | 行为 |
|---|---|
| 单个来源 JSON/权限错误 | 保留旧数据和来源游标；记录该文件错误，继续其他文件 |
| 来源目录枚举失败 | 本轮标 partial，不批量标记所有会话消失 |
| 活跃文件/未完成末行 | 暂缓或只提交格式允许的完整安全前缀，不越过坏行 |
| 文件截断/前缀改写 | 单文件核对/新代际；不自动删除既有分析历史 |
| 本地 DB 损坏/无法安全写入 | 启动失败或停止导入；不覆盖/改连空库 |
| 云端离线/连接超时 | 本地查看不受影响；pending/确认位置保留，有限重试后让用户重试 |
| TLS/错误域名/跨域重定向 | 拒绝连接，不关闭证书校验、不转发 Key |
| 401/403 Key 撤销/过期/无权限 | 停止上传，提示替换 Key，不重新发送全部历史 |
| 409 进度或 dataset 冲突 | 重新读取云端已提交位置核对；禁止盲改 key 绕过 |
| 同 key 不同 payload / 同 revision 不同内容 | 完整性错误；保留诊断，不猜测覆盖 |
| 413 超上限 | 不截断消息；按安全单位调整，单个不可拆分项明确失败 |
| 422 数据/类型/附件约束不符合 | 整批拒绝，不推进任何上传游标；本地历史保留 |
| 426 协议不兼容 | 提示升级，不猜旧字段或回退到原始上传 |
| 429 | 遵守 Retry-After；超预算后暂停本次运行，不无限后台重试 |
| 5xx / 云 DB busy | 先考虑结果不明确，重试原 key/body；不标整批未提交后换 key |
| 磁盘满/配额满 | 事务回滚，不推进游标；明确提示，不自动删历史 |
| 用户暂停/取消 | 批次边界生效；提交结果不明确时先恢复回执 |
| 修改云地址/更换授权命名空间 | 测试/确认新连接，不能沿用旧游标 |
| 本地恢复到旧备份 | 阻止复用旧 seq，新数据集或显式恢复流程，云端旧数据不删 |

网络重试使用指数退避 + jitter 和明确次数/总时间预算。必须遵守服务端
Retry-After；不因预算到期就认定一次超时提交肯定没发生。

## 12. 安全边界

### 本地无需登录，但不是开放网络服务

- 默认只监听 localhost/127.0.0.1/::1；V1 不提供无密码 LAN/公网绑定。
- 校验 Host、精确 Origin（含端口），不信任任意 forwarded headers。
- 不开放宽泛 CORS；本地写 API 校验页面拿到的随机 CSRF token/自定义 header，
  防恶意网页调用本地 API、DNS rebinding 或偷偷改云端地址/启动上传。
- 上传 Key 永远只发给用户批准且验证证书的云端 origin；不跟随跨域重定向。
- 云上传 HTTP client 使用自己的 Bearer 凭据，不转发浏览器 Cookie；本地应用不把
  可能收到的其他 localhost 服务 Cookie 当成本地登录凭据，也不将其记录进日志。
- 本地查看历史允许无密码；若用户需要远程访问本地端，应另行设计鉴权，而不是
  简单改 `--host=0.0.0.0`。

### 云端

- 历史页/控制台/读取 API 要密码 session；机器 ingest 要 API Key，二者分离。
- 每个 Key/dataset 的 ID/进度/实体引用均有授权边界；body 不可覆盖身份。
- 内容都是不可信数据：不执行 tool 参数，不抓取上传 URL，不反序列化 pickle。
- 页面转义、CSP、CSRF、no-store、登录限流保留。日志不含正文、密码、Key 或 Cookie。
- 传输、卷、备份做好访问控制/加密；数据不脱敏不意味着可公开访问。

## 13. Docker 与 Cloudflare

只有云端提供 Docker：

1. **云端服务的本机直连模拟**：发布 `127.0.0.1:41880`，明确允许 loopback HTTP，
   用于和本机 Local Web Server 联调。这不是把本地采集器装进 Docker。
2. **VM + Cloudflare Tunnel**：云端服务与 cloudflared 两容器，应用无宿主机发布
   端口，HTTPS public origin，经 Docker 内网 HTTP 连接。Cookie Secure，Host /
   Origin 与 Tunnel Host Header 精确匹配。

本地 Web Server 在用户机器原生启动，默认端口可继续 8000。云端和本地使用不同
配置/数据库/凭据；Compose volume 与已有 Local 数据没有绑定关系。

Cloudflare 不替代应用 Key/密码。不要缓存私有页面/API，不要给机器 ingest 加
JS/CAPTCHA challenge；当前未实现 Access service-token 就不能直接保护整站后
假定机器能穿过交互登录。边缘 body/timeout 需与批量 API 的限制匹配。

Server-only 镜像使用显式代码白名单，禁止把 Local 数据库、Agent 历史、
change log、配置/Key、`.env` 或 Git 打进镜像。健康检查不依赖用户登录，且不暴露数据。

## 14. 旧结构和数据迁移

当前仓库有 Server、Deamon、原 Local 三项目。实施顺序：

1. 先完成新的本地业务库/变更日志和两套游标，复用来源解析与分析投影。
2. 将 Deamon 的 HTTP 传输能力移为 Local uploader，删除任务 GET/ACK/轮询入口。
3. 增加本地无密码查看页和云同步控制台，保证启动先导入后监听。
4. 云端将设备令牌管理收敛为 API Key 管理，增加密码登录与 dataset change-stream API。
5. 删除云端任务页面/路由；旧任务记录仅在显式迁移时归档/取消，不偷偷删除历史。
6. 更新两套云端 Compose、两端 README 和根目录测试，再切换真实部署。

### 数据保留

- 现有 `local/viewer.db`、`local/claude.db`、备份和 sidecar **保留原文件**，不直接
  在它们上面执行“删除 thinking/raw/附件”的破坏性升级。
- 建议创建新的 `local/state/local.db` 作为分析业务库，从旧库只读提取四类数据，
  包括只存在于旧库而 Agent 文件已消失的历史。不能只重扫当前目录丢掉这些会话。
- 转换后核对保留内容、消息身份/时间/结果和来源进度，再明确选择新库。
- 旧云端 analysis-v1 内容可以迁入新 dataset 命名空间；旧 byte cursor 不直接当
  新 change seq。已有 Key 是否保留、队列记录如何归档均需明确迁移，而非自动猜测。
- Git 中只保留代码/文档/合成测试，DB/sidecar/日志/Key/真实历史持续忽略。

## 15. 验收与测试

两应用各自自测，跨端与 E2E 在根目录，不依赖真实历史或公网云账户。

必须覆盖：

- 本地启动阻塞在导入时，HTTP 尚未监听；导入完成再能看到入库消息。
- 云端完全不可达/未配置时，本地启动、导入、无密码查看仍正常。
- 本地空闲不扫描、不监控、不轮询云端、不发送 heartbeat、不自动上传。
- 本地控制台设置 URL/Key、测试连接、手动同步/暂停/继续及状态查看。
- 本地无登录但 Host/Origin/CSRF/CORS 防护有效；Key 不回显、不进日志。
- 云端控制台密码保护，API Key 只能 ingest/查自己的进度，不能登录/管理。
- 五种来源增量、坏行、前缀改写、快照变化、工具状态更新、subagent 排除。
- 消息/状态/change log/pending/云端实际落库均无 thinking/系统/附件/结果正文。
- 原正文与参数中的凭据/签名不被脱敏，时间缺失来源与 unknown 结果准确。
- 本地导入事务任意阶段崩溃，消息/变更日志/来源游标不出现半提交。
- 远端 POST 响应丢失、本地回执保存前崩溃、并发更新、同 key 不同 body、不同 dataset
  和本地旧备份恢复都不会重复或静默丢数据。
- 上传期间本地新增消息只增加 seq，本轮 H 不变化，pending body 不被篡改。
- 连接改地址/换 Key、云实例恢复、Key 撤销、413/429/5xx/磁盘满可恢复且不误推进。
- 普通批次按大小动态组成；必要 group 不被硬切，超大单元不截断。
- 云端两套 Docker、卷/密码/session 持久化、Cloudflare Host/Cookie 约束。
- Windows 测试/后台子进程无窗口；只清理测试自己创建的进程和卷。

## 16. 本设计的默认取舍

为避免继续扩大复杂度，建议首版明确限定：

- 一个本地应用、一个活动云连接、一个手动上传 worker。
- 新业务库两端只存四类分析消息；旧详细库保留作只读迁移源。
- 全库连续变更同步，不支持在一个全局 seq 中跳着上传某些项目。
- 普通批量 POST + Key + 幂等回执，没有 chunk/seal、云任务、heartbeat 或 watcher。
- 本地和云端均使用 SQLite WAL，云端单写服务；不做多实例分布式数据库。
- 无 SSO/多租户/自动调度，密码会话与上传 Key 管理先做好。
- 原始时间/结果不明确就标记来源/unknown，不伪造精度。
- 先验证迁移与故障恢复，再显式更新真实本地库和云端部署。

**待你确认本文后，再开始修改实现。**
