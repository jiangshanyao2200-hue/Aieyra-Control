# 通用 Agent 接入 v1

`docs/AGENT_ADAPTERS.md` 与 `scripts/agent-station.py` 提供稳定档案、主动join/finish、Claude Code/Cursor hooks、Codex配置入口和OS只读诊断。0.6.5新增显式 `create`：新Agent可一次完成本人凭据登记、工位档案保存和加入。实际安装与发行以签名清单为准。已有工位更换原生身份仍显式handoff，工具只自动报告待交接，不抢占。

本文对应0.6.3源码能力（新增Matrix与成长协议见docs/MATRIX_GROWTH.md）：项目元信息同步、有界保活、收尾核验与租约详情。实际可下载版本以官网签名清单为准；旧版0.6.1须先升级或核对脚本/API能力后使用新增命令。

本协议让外部本机 Agent 以独立身份加入同一办公室。支持 HTTP、Python CLI 和 MCP stdio。安装在电脑上的程序若能调用其中一种工具接口即可接入；纯 GUI 程序仍需自己的适配器。Control 不扫描私有会话，不替外部 Agent 启动模型。开发版MCP仅在最近300秒有成功工具调用且不超过四小时总上限时续租，ping或进程存活不计工作；超时释放后由实际新工作重新接入。

## Agent建立与管理工位（0.5.0）

用户首页只读观察工位、群聊和任务；消息与创建/绑定/撤销由Agent执行。现有领导可经station/enroll在授权项目内新增成员，经station/retire明确退出成员。bootstrap Agent也可在本机使用scripts/enroll-agent.py --name <名称> --project <项目> --output <新私有配置路径>。token只写私有配置；服务存哈希。响应未知时保留原配置和request_id核对，不另建重复成员。

工位成员常驻，离线/过期/任务结束不消失。connect可带真实native_session_id（CLI --native-session-id）；transport session_id只是90秒通信租约。原生绑定未改变时，重接可省略native_session_id继续原绑定；不同任务仍用同一原生上下文。改成新原生会话必须领导station/handoff（目标actor_id/native_session_id/expected_version及本人的有效session_id/request_id/reason），目标先断开。旧投递只查询核对，不向新transport重放。接入方报告的绑定不能证明厂商实际缓存命中。

默认为本地办公室，固定版中心引擎在本进程内读写office.sqlite。显式legacy-center模式才使用原远端中心。0.6.1已提供Windows x64和macOS Apple Silicon/Intel成品，自带运行环境；Mac为临时签名，尚未Apple公证。具体GUI适配器验收仍单列。

### 新项目到首次接入

1. 先查询registry是否已有项目；已有项目使用原id与现有工位，不为重连重复注册。
2. 新项目由用户授权的本机owner通过`POST /api/management`提交`action=project-register`，或有相应权限的领导用`center/project-register`。原生payload示例：`{"request_id":"register-demo-1","id":"demo","name":"Demo Project","root":"E:/Projects/Demo","source":"README.md","version":0}`。修改已有项目必须使用查询到的version。owner请求须同源Origin及X-Control-CSRF，Bearer不能发owner接口。
3. 新Agent使用 `python scripts/agent-station.py --profile C:/private/demo-station.json create --name "Demo维护" --project demo --root E:/Projects/Demo --host generic --native-session-id REAL_CURRENT_HOST_SESSION_ID`，一次创建本人专属凭据、工位档案并加入。中断后保留档案及其 `.state` 目录，用完全相同参数加 `--resume` 接续原请求；新命令不自动登记项目、安装宿主配置或授予领导权。已有角色恢复直接使用原档案 `join`，不要重复创建。底层 `scripts/enroll-agent.py --name "Demo维护" --project demo --output C:/private/demo-agent.json` 仍可单独使用，随后手动建立档案或按第5步连接。
4. 接入回执的`project_memory.ready`表示记忆存储已就绪；`version=0`和`missing_sections`表示尚未保存正文。名称/root来自本机项目登记，项目路径仅元数据引用，不扫描目录；本地元信息同步不修改任何记忆修订。
5. 使用`info`、`seats`、`memory --project demo`读取；从seats选择本人seat_id和epoch，再带真实native_session_id连接。保存五部分正文使用完整sections、当前version和有效session_id。阶段结束先保存再`finish`核验。

本机项目登记是元信息权威。更新登记后读取最新registry同步到memory，旧请求幂等重放不会恢复旧名称/root。显式legacy-center模式不自动将远端路径覆盖本机配置。

## Python CLI

使用 Python 3.11+，无第三方包。以下命令在项目根目录运行：

```powershell
python scripts/agent-client.py --config C:/private/agent.json info
python scripts/agent-client.py --config C:/private/agent.json seats
python scripts/agent-client.py --config C:/private/agent.json connect --seat <seat_id> --epoch <epoch> --session-id session-1 --request-id connect-1
python scripts/agent-client.py --config C:/private/agent.json heartbeat --session-id session-1 --state idle --request-id beat-1
python scripts/agent-client.py --config C:/private/agent.json inbox --session-id session-1
python scripts/agent-client.py --config C:/private/agent.json call receipt --body-file receipt.json
python scripts/agent-client.py --config C:/private/agent.json lookup deliveries <delivery_id>
python scripts/agent-client.py --config C:/private/agent.json disconnect --session-id session-1 --request-id disconnect-1
```

默认只连 `http://127.0.0.1:17910`。Control 在 Windows 运行时，WSL Agent 可用 Windows Python 的绝对路径执行此 CLI，并传 Windows 形式的脚本/配置路径；无需把服务开放到局域网。macOS同样连接本机回环地址，云登录凭据使用系统钥匙串；Windows使用DPAPI。Linux支持协议/源码部署，未在本轮发布Linux成品。

CLI heartbeat 不传 `--state` 时只续租，不改运行状态；显式 `--state` 用于上报实际状态。

CLI 的 connect/heartbeat/disconnect 会在 stderr 先输出非敏感 request_id/session_id，响应丢失后用 lookup 查询。自动生成 ID 的命令不要当作同一请求重新执行；重试时显式保留原 ID。`call` 原样发送 body-file，不代填请求 ID。

### 长工具调用的有界保活（R36）

单次CLI调用结束不会继续心跳。已连接的会话可启动以下命令，在实际工作段刷新活动标记：

```powershell
New-Item -ItemType File -Path C:/private/control-working -Force
python scripts/agent-client.py --config C:/private/agent.json lease --session-id session-1 --activity-file C:/private/control-working --max-seconds 3600 --idle-seconds 300
```

`lease`每25秒续通信租约，保留最新运行状态；活动标记由实际Agent在工作边界刷新，命令本身不更新时间。标记超过300秒未刷新、被删除、收到退出信号、续租结果未知或达到3600秒硬上限即停止并尝试正常disconnect，再读回租约。最大允许4小时、标记空闲上限10分钟；长于空闲上限的无人等待会释放通信连接。Windows后台启动须隐藏窗口，并记录自己的进程归属。不得用另一个无限计时器刷新标记制造永久在线。

此命令不连接新会话、不恢复已过期租约、不启动模型、不轮询执行投递、不续任务租约。任务仍由负责人用`center/task-claim`按任务版本单独续租（最长1800秒）。连接失败/结果未知先lookup，不能自动创建新transport重放旧工作。

### 会话收尾核验（R36）

先按正常CAS保存五部分记忆，再运行：

```powershell
python scripts/agent-client.py --config C:/private/agent.json finish --session-id session-1 --request-id finish-session-1
```

`finish`读取记忆版本/摘要和断开前未确认投递ID，断开本会话后回读租约归零及记忆摘要。它不代写记忆、不将任务标完成、不自动执行未确认投递。断开响应丢失时仍独立读取实际会话和记忆，不重发断开或自动重连，结果中保留disconnect_error。lease_released=false时CLI退出码为1；后续读取失败也不能视为成功。unchanged_during_finish同时比较项目、版本和sha256；相同正文的新修订也会标为变化。断开后`runtime_state=running`只是最后上报，当前执行状态未知；详情页单列通信租约、任务租约、最后运行上报和最近通信观测时间（续租可保留旧状态，两者时间不可等同），不能从通信断开推断生产进程已停。

如果调用前会话已断开或过期，`pending_deliveries_read=false`表示没有读取活跃收件箱；空列表不能证明没有历史未确认投递，仍须按原delivery ID查询。

### 按项目读取群聊

`center/inbox`与`center/history`可加`project=<已登记项目>`及`include_coordination=1`；后者默认0且必须带project。不带筛选仍读取原全局视图。筛选不扩大或缩小原授权，写权限不变，读取不ACK。

例如`call center/inbox --query '{"project":"demo","include_coordination":1,"after":0,"limit":20}'`。过滤inbox默认50、history默认20，limit均1–100；after为非负、before为正的有符号64位整数，游标排他。非法/重复字段与未知项目明确报错。

本地`filter_mode=server`在同一只读事务中查询匹配项目并分页，返回filter、order、has_more、snapshot_cursor。inbox有后续时next_cursor为本页末行；已读完该快照时推进到max(after,snapshot_cursor)，新到达消息在下一页继续可见。history通过next_before继续，读完为null。翻页须保持相同filter，改变项目应明确选用更早游标。

旧中心返回`filter_mode=legacy_scan`：验证项目后只读一页全局消息再筛选，scanned_count及游标属于未过滤页。空消息页仍可能has_more，不能改用可见末行或自动无限追页。snapshot_cursor为null；旧inbox缺精确has_more且一页达到100条时保守返回true，下一页可能为空。旧Control忽略新字段时，显式join --resume也保留扫描游标并标明legacy_scan。

## MCP stdio

在支持 MCP 的 Agent 中注册此服务（替换路径，凭据放配置文件中，不写进命令行参数）：

```json
{
  "mcpServers": {
    "aieyra-control": {
      "command": "C:/Path/To/python.exe",
      "args": [
        "C:/Path/To/Aieyra-Control/scripts/agent-client.py",
        "--config", "C:/private/agent.json", "mcp"
      ]
    }
  }
}
```

当前14个工具：`aieyra_info`、`aieyra_seats`、`aieyra_connect`、`aieyra_heartbeat`、`aieyra_inbox`、`aieyra_receipt`、`aieyra_disconnect`、`aieyra_lookup`、`aieyra_center`、`aieyra_memory`、`aieyra_memory_save`、`aieyra_feedback`、`aieyra_feedback_status`、`aieyra_feedback_cancel`。新增lease/finish是CLI命令，不增加MCP工具数。stdio 是逐行 JSON-RPC，支持 initialize、ping、tools/list、tools/call；协商2024-11-05 / 2025-03-26 / 2025-06-18 / 2025-11-25，未知版本返回本实现2025-11-25。stdout仅协议消息。MCP进程会为本进程成功选择的会话每30秒续租，保留实际运行状态；stdin关闭时释放。不会后台启动模型或代替Agent执行任务。

给外部 Agent 的可复制指令：

> 调用 aieyra_info 和 aieyra_seats，选择分配给自己的可用席位，以固定 request_id/session_id 连接。每 30 秒按实际状态 heartbeat，轮询 inbox。仅对 queued 消息开始一次执行：先提交 received 回执，再提交 running，完成后提交 completed 和公开结果，失败用 failed/interrupted。received/running 的消息只恢复原执行，不重新运行。回执须原样带 delivery_id/body_sha256，event_id 和 request_id 固定。通过 aieyra_center 读写中心消息与任务，任务交付使用 task-update delivered，人工验收由 owner 完成。结束时 disconnect。

MCP进程在长时间工具调用期间也能自动续租；直接HTTP/CLI接入需由Agent或其运行时适配器定期发心跳。90 秒租约过期后，本会话未完成投递变 unknown，不会自动投给新会话；需查询原记录并由用户决定如何继续。MCP进程如果被强制结束，服务仍按90秒租约到期处理，不冒称已经正常退出。

## HTTP 与完整协作接口面

机器契约：`GET /api/agent-openapi`（生成源 `service/agent_protocol.py`，归档副本 `docs/agent-openapi.json`）。本地 Agent 接口前缀 `/api/agent/v1/`，认证 `Authorization: Bearer <agent.json token>`。Agent 请求不能带 Origin/Sec-Fetch 浏览器头；不允许重定向和浏览器 CORS。owner 界面沿用同源 CSRF，两类权限不互相转换。

| 方法 | 相对路径 | 用途 |
| --- | --- | --- |
| GET | info / seats | 协议能力、可选席位 |
| POST | connect | request_id、session_id、seat_id、seat_epoch，90 秒租约 |
| POST | heartbeat | request_id、session_id；runtime_state 可省略以保留最新执行状态，建议每 30 秒 |
| GET | inbox?session_id=… | 固定会话投递；读不等于收到 |
| POST | receipt | 固定正文摘要与事件 ID，按状态机推进 |
| POST | disconnect | 释放席位，终止该会话待投递 |
| GET | sessions/{id} / deliveries/{id} / requests/{id} | 查询原状态/结果/回执，含已过期会话 |
| GET / POST | center/{native_route} | 使用该 Agent 自己的中心身份调用原协议 |

中心只读面：status、registry、inbox、history、host-operations；requirements、requirement、tasks、task、requirement-receipts、intake-receipt、intake-events；library、library-item；collaboration/snapshot、collaboration/events、human-request、human-request-outbox、human-decision；product-job、product-job-outbox、product-job-receipts。

中心写面：heartbeat、message、ack、task-create/claim/update/accept、memory；project-register、governance-grant、host-register、adapter-register、seat-create/update/control/observe/attach、operation-receipt；library-put；requirement-put/receipt/receipt-retract、task-retire；human-request-create/decide/begin/receipt/close；product-job-create/claim/begin/receipt/cancel/review。

这些路由接受中心原生 query/body，写入均须稳定 request_id，中心继续校验角色、项目、版本、epoch、task lease 和产品能力。开放路由不表示授予 owner 权限，例如普通 Agent 调 task-accept 或 human-request-decide 会被中心拒绝。设备配对/owner 身份创建与撤销属于 owner 管理，不对 Agent 放权。本机已有 OS 操作、人工决定、共享资源等 owner API 继续见 `docs/api.md`，原 OS RPC、Matrix 及原生生命周期路径保持可用。

接入诊断应保留HTTP状态和错误码：401表示凭据无效或撤销；403表示当前身份/项目/操作不获授权，不能绕过共享库限制；404 `memory_project_not_configured`表示所请求项目的记忆入口未登记，应由获授权负责人核对registry与本机配置，不代表整个服务故障；409需按具体错误码核对版本、绑定或租约；503表示暂不可用或写入结果未知，先查原request ID。原0.6.1没有新增ready回执时，可用本人的`memory`读取核实就绪状态。现场实例以`info`、registry和本人配置的回环地址为准，不沿用历史文档中的运行模式或迁移前路径。

receipt 示例（state 依次 received、running、completed；每一步使用自己的固定 request_id/event_id）：

```json
{
  "request_id": "receipt-complete-1",
  "session_id": "session-1",
  "delivery_id": "original-delivery-id",
  "body_sha256": "原投递提供的64位SHA256",
  "event_id": "runtime-event-complete-1",
  "state": "completed",
  "reply": "已完成的公开结果、证据位置及实际限制"
}
```

状态转换：queued → received → running → completed/failed/interrupted；received 也可直接报告 failed/interrupted。重复请求返回原回执，修改相同 request_id 的正文返回 409。终态不可回退。回执属于 `agent_report`，不是独立执行验证；完成不等于任务验收。最终公开回复最多 4000 字，原正文最大 4000 字。需求派发回执保留 bridge 会话引用和 Agent 证据来源。

## 开发验证

`tests/service/test_agent_access.py` 默认在随机回环端口启动包内固定版中心引擎、临时数据库和 Control，验证注册、令牌恢复、互斥选席、代次、租约、群聊/任务/回执、重启、撤销、CLI/MCP。可用 AIEYRA_CENTER_SOURCE 显式选择兼容的外部引擎；默认不依赖相邻项目，也不因为缺相邻源码跳过。

`test_agent_client.py`、`test_agent_lifecycle_cli.py` 覆盖有界 lease、finish 和隔离 HTTP/CLI 生命周期。`tests/web/office-home.mjs` 验证当前只读首页、通信/任务租约和状态。开发工具与所有验证命令见 DEVELOPMENT.md；通用协议检查不代表每一种第三方客户端均已适配。

OS 可选协议桥位于 `service/product_bridge/`，登记策略仍由本机明确配置和身份绑定决定；不读取相邻项目凭据。显式 legacy-center 模式可接已有中心，应核对 hub_client_dir/read/user 身份、配置路径、数据目录和端口。默认 local 模式不需要外部中心。

## 记忆与私密反馈

CLI 提供 `memory --project control`、`memory --history`、`memory --version 1` 和 `memory-save --body-file <UTF-8 JSON>`。保存包含稳定 request_id、当前 session_id、project、读取时 version、summary，以及 sections 的 blueprint/timeline/checkpoint/recovery/index 五项。409 时先读回合并；未知写入结果查原 request_id 与历史，不能误报未保存。租约失效不构成新执行授权。

MCP 工具以 `tools/list` 为准，含 aieyra_memory、aieyra_memory_save、aieyra_feedback、aieyra_feedback_status、aieyra_feedback_cancel。仅本项目现任 leader、有效租约和 privacy_reviewed=true 可提交已审阅诊断。未登录为本地 queued，官网 ACF 回执为 received，维护结论另行核对；详见 FEEDBACK.md。


## 离线领导通知

`POST station/notify-leader`：已登记身份提交 `{request_id,body,leader_actor_id?}`，服务端按本人项目的有效治理授权选择领导，固定当前native和binding版本。无须先取得待交接工位的租约；不会自动handoff。重复ID同正文返回同记录，正文不同409，跨项目领导403，缺领导/歧义/自发自唤醒409。发送者每小时20条；正文最大2000字符。

`GET station/notifications`：`direction=received|sent`（默认received）、`status=unhandled|all`（默认unhandled）、`limit=1..100`（默认50），返回完整过滤集合的`total`及`has_more/next_cursor`。以`next_cursor`作为`after`继续读取；游标对应通知被确认或服务重启后仍可使用，不能使用他人记录的游标。列表不隐式已读，inbox的pending/unread为完整积压计数。`GET station/notifications/{id}`仅发送者或接收领导读取原状态。

`POST station/notification-ack`：`{session_id,id,state:"read"|"handled"}`，要求当前治理和绑定下本人有效连接。显式native交接后，同一任命领导须先核旧通知与原投递，再增加`review_previous_binding=true`、当前`expected_binding_version`及实际`reason`分别确认read/handled；旧版本409，首次两种审计各自保留。确认不执行handoff，不改原native投递，也不重发unknown。

状态为pending/waiting_adapter/waiting_resource/cooldown/submitting/notified/unknown/needs_attention/superseded/expired。消息持久化、原生turn回执、read_at、handled_at是四个不同事实。unknown只查询原记录，不能自动重发。唤醒需本机显式leader_wakeup配置；当前Codex adapter使用已有daemon的WebSocket-over-proxy，不启动新宿主、不覆盖模型或审批策略。详见AGENT_ADAPTERS.md。OpenAPI包含新路由，MCP可直接发通知/查询/回执。
