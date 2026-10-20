# Artemis 主子常驻 Session 实施方案

状态：已在独立实现分支落地，真机验收待执行。日期：2026-10-20。

代码基线：`351ca8422f7b5b54e80a9c1ce03a222e02415b6b`；实现分支：`codex/master-child-sessions-implementation`。本文保留设计依据；实际入口、接口与边界见 [实现与运行说明](persistent-role-sessions-usage.md)，会议配置见 [示例](meeting-workflow.example.json)。第 2 节描述改造前基线。

## 1 最终决策与范围

采用一个 Main 进程，加每个 Role 一个常驻 worker 进程。worker 内只承载一个原生 Artemis Session，串行接收多个任务。Main 使用 asyncio 管理连接、调度和诊断；不同 Role 的 Context、模型对话、Driver、执行图通过进程隔离。

子 Session 保留 Context、TranscriptLedger、StepMemory、DataEngine、设备连接和设备租约。每个任务重新创建原生 State、图和节点。任务结束回到 READY，不退出 Session、不重新登录、不清空角色记忆。常驻不意味着执行图永远运行，idle 不调用模型。

Main 拥有设备分配、观察、诊断、取消、关闭和故障接管的管理权。正常业务动作由对应子 Session 独占执行。Main 接收完整业务目标，维护任务依赖和结构化事实，按需读取截图、工具记录与上下文。

首版交付：

1. 本机 Android、Pro、固定 Role/device/profile 的常驻执行。
2. Main 调度、可靠控制协议、验收、事实传递、失败处理。
3. 设备观察、上下文诊断、异常后控制权转移。
4. 原有 Agent.run_task 和 Flash 一次性路径兼容。
5. 双设备会议流程、连续任务和故障注入测试。

首版不实现跨主机、常驻 Flash、运行中换 profile、崩溃后完整内存恢复、Main 崩溃后自动续跑、管理前端重构。worker 存活时上下文连续；崩溃后建立新 Session/epoch，从持久事实和可用历史恢复业务并重新观察设备。

选择多进程是为了保留原生“每进程一个活跃 Session”的假设，降低升级时共享状态审计成本。代价是每 Role 一份解释器、SDK、线程和连接；常驻只摊薄启动成本，不消除内存开销。协程也需要本方案的生命周期改造，另需隔离 Engine、日志、取消和其他进程级状态，因此不符合本次升级成本优先的目标。

## 2 现有代码事实

| 已核对位置 | 当前行为 | 改造要求 |
| --- | --- | --- |
| [AgentBase._run_task](../third_party/mobile_use/sdk/agent.py) | 一次调用创建 Context、启动数据 Session、执行图、结束 Session 并释放设备 | 拆会话生命周期，不能在 worker 简单循环 run_task |
| [Agent._prepare_tracing](../artemis/sdk/agent.py) | 创建 ExecutionSetup、DataEngine 并 start_session | 常驻只执行一次 |
| [Context.__aexit__](../artemis/context.py) | 关闭 ActionSession、处理记忆/后台任务、关闭 MCP/Engine | 每任务不退出 Context |
| [get_graph](../artemis/graph/graph.py) | 按 Context 创建图与节点 | 每任务复用工厂，保持拓扑 |
| [Operator](../artemis/agents/operator/operator.py) | Ledger 在 Context；末轮先 stage，下轮构建提示才 commit | 每任务末轮必须显式提交 |
| [TranscriptLedger](../artemis/memory/transcript.py) | 静态前缀只能设置一次；前缀含 initial_goal | 公开任务边界 API，刷新目标、保留历史 |
| [Checker](../artemis/agents/checker/checker.py) | 默认读整 Session；verify inconclusive 可允许原生放行 | 本任务历史隔离；Main 严格验收独立于原生放行 |
| [Outputter](../artemis/agents/outputter/outputter.py) | 默认读整 Session 计划和步骤 | 限定当前任务提取范围 |
| [DataEngine](../artemis/data_engine/engine.py) | shutdown 等写入后关闭 IPC；后台失败主要记日志 | 非关闭式 flush，保留已完成写入的失败记录 |
| [ActionSession](../artemis/mcp/action_session.py) | caller 取消后底层 call_tool 可继续；observe_screen 会更新索引 | 动作停止屏障；诊断不能任意更新索引 |
| [ADB 命令](../artemis/tools/command_tool.py) | 短命令、尚未 handoff 的长命令并非已登记后台任务 | 从 spawn 到退出都要跟踪和清理 |
| [LLM 暂停](../artemis/services/llm.py) | 与 Engine 共用 PAUSE_FILE | 常驻 Session 独立暂停路径 |

原生层只增加通用生命周期、任务边界和诊断接口，不导入 Main、Role 或 IPC。禁止复制整份 _run_task、monkey patch、共享 Python Context、逐 Agent 添加 Role 判断、改写图为多设备路由图。

## 3 模块结构

```text
Main process
  MainSession
    MainPlanner -> PlanProposal
    Scheduler / FactStore / Journal / DeviceManager
    WorkerHandle A <---- JSONL TCP ----> Role A process
    WorkerHandle B <---- JSONL TCP ----> Role B process

Role process
  Worker control loop + one execution coroutine
    PersistentSession
      one Context / DataEngine / Ledger / device lease
      fresh State and native graph for every task
    ContractVerifier / Diagnostics / ResultStore
```

| 新文件 | 职责 |
| --- | --- |
| artemis/runtime/session_scope.py | 通用 TaskScope、步骤范围、暂停 resolver；不依赖编排包 |
| artemis/runtime/session_operations.py | 登记真实底层任务/子进程，取消后仍跟踪到底层结束，输出停止报告 |
| artemis/sdk/persistent_session.py | PersistentSession、SessionOptions、NativeExecutionResult；组合 SDK 生命周期 |
| artemis/sdk/session_boundary.py | begin/end、Context 重置、末轮提交、停止屏障、flush；不处理 IPC |
| artemis/runtime/session_orchestration/models.py | 严格协议模型、WorkflowSpec、TaskSpec、TaskResult、诊断 DTO |
| .../main_session.py | Main 对外入口、事件循环、工作流结果和关闭 |
| .../planner.py | MainPlanner、模型适配、受约束 PlanProposal；可注入静态计划测试 |
| .../scheduler.py | DAG、就绪检查、输入绑定、任务/attempt 状态、预算 |
| .../facts.py | 带来源、版本、时效和证据的事实；冲突和失效 |
| .../worker.py | 子进程入口、认证、控制循环、单任务准入、heartbeat |
| .../worker_handle.py | spawn、进程监控、连接、日志 drain、定向取消、停止验证 |
| .../transport.py | 编解码、有界队列、身份校验、RPC 关联、同进程重连 |
| .../contract.py | 请求适配、受限 schema、独立契约复核、结果判定 |
| .../diagnostics.py | 快照、观察、分页、敏感字段过滤、证据定位 |
| .../device_manager.py | 分配、lease 查询、外部有限观察、故障接管 |
| .../artifacts.py | 固定归档清单、manifest、结果/快照原子写入 |
| .../journal.py | 单写者日志、request/result 去重、ack 状态 |
| .../compatibility.py | 能力检查、有效配置、并发额度与原生补丁清单 |
| .../__main__.py | Main CLI 入口 |

`.../` 均指 `artemis/runtime/session_orchestration/`。SDK 类型留在 SDK，协议类型留在编排包。共用的轻量 TaskScope、操作报告不反向依赖编排。Main 不创建原生执行用 Context/DataEngine。

## 4 身份与所有权

| ID | 规则 |
| --- | --- |
| workflow_id | 一次 Main 工作流 UUID |
| role_id | 流程内稳定角色名称 |
| child_session_id | 一个真实 Artemis Session UUID，Main 预分配 |
| worker_epoch | 一次 worker 启动 UUID，重启必换 |
| task_id | 逻辑任务 UUID，另设 display_name |
| attempt_id | 一次尝试 UUID；业务重试换 ID，网络重发不换 |
| message_id | 单条协议消息 UUID；原消息重发不变 |
| snapshot_id / fact_id | 不可变快照/事实版本引用 |

一个 worker 一个 Session，一个 Session 最多一个 active attempt。设备 lease 从 open 持有至 close，包括 idle。协议和目录使用验证过的 ID；下文 A1/A2 为易读简称，不能把模型提供的名称直接用于路径拼接。

| Session 保留 | Task 重建或清理 |
| --- | --- |
| 设备、连接、profile、lease、Driver、Controller | Task、State、图和节点实例 |
| DataEngine、Session ID、连续步骤序号 | 当前步骤指针、active TaskScope |
| Ledger、StepMemory、历史摘要、普通业务笔记 | 当前目标、task_plan、检查台账、输出报告 |
| 包信息缓存、可用的 ActionSession/MCP、录像 | 检查任务、局部预算、停止标志、规划验证状态 |

进程隔离不隔离磁盘：Session 路径、暂停文件、设备锁仍须分域。共享 SQLite 沿用现有 session_id 归属，不给各 Role 建独立数据库副本。

## 5 Main 的调度模型

### 5.1 WorkflowSpec 与计划

WorkflowSpec 包含完整用户目标、RoleSpec 列表、业务约束、总预算、故障策略、可选初始任务图。RoleSpec 指定设备、职责、profile、凭据引用。凭据本地解析，不进入普通事实和诊断文本。

MainPlanner 读取目标、当前计划、事实索引、任务结果、worker 健康和按需诊断，输出 PlanProposal：新增/修改未执行任务、依赖、信息查询、诊断请求或结束建议。复用项目模型服务，模型不直接调用 Driver、不改调度存储。

首版 MainPlanner 输出限定为 propose_tasks、revise_pending_task、request_observation、request_inspection、request_input、finish 六类结构化建议。模型调用适配到现有 LLM 配置，Main 维护自己的消息历史和预算；不建立原生设备执行图。cancel/retry/takeover 由 Scheduler 的故障策略决定，模型只能建议并说明证据。

每个 proposal 带 base_plan_revision。Scheduler 用代码检查 Role、DAG 无环、类型、预算、恢复动作权限；过期 proposal 拒绝或重算。不得改已 accepted attempt 的请求、删除历史、把 failed 改写成 succeeded。格式/计划错误允许最多两次纠正，仍无合法方案则工作流 blocked。

模型参与首次规划和需语义处理的结果、阻塞、冲突、用户新指令。heartbeat 不触发模型；已有确定依赖由代码推进。

### 5.2 任务准入

dispatchable 必须同时满足：

1. 工作流 RUNNING，任务未执行且未取消。
2. 依赖成立，输入已解析为接受的不可变事实版本。
3. 事实未失效，要求新鲜观察时满足 max_age_seconds。
4. Role READY，epoch 一致，设备未隔离。
5. 工作流/任务预算、并发额度和业务限制允许。

依赖使用结构化 all/any，叶条件只允许任务终态、指定 check verdict、输出标量等值、事实新鲜度；禁止 eval。默认依赖前序成功，恢复分支必须显式声明允许的失败终态。

同 Role 待办在 Main 排队，worker busy 时拒绝，不隐式排队。不同 Role 可并行。首版 progress 不释放依赖；需要中间状态交接时将任务拆到该状态结束并返回 result。

### 5.3 FactStore 与信息不足

Fact 字段：fact_id、key、value、source_role、child_session_id、task_id、attempt_id、result_revision、observed_at、evidence_refs、validity、expires_at。validity 为 observed、reported、inferred、invalidated；模型推断不能自动变成 observed。

例如 A2 不仅返回成功，还返回 MeetingID、是否需要密码、是否需批准、会议当前状态。Main 在派发前就要求下一步所需字段。新观察产生新版本，历史不覆盖；冲突时阻塞相关依赖并重观测，不能按最后消息覆盖事实。

缺信息依次选择：读已有快照；请求受限只读观察/核验；派显式业务任务导航查信息。导航可能改界面，必须走 execute，不能伪装诊断。child 报告 blocker 与建议，Main 对建议重新校验。

### 5.4 Main 事件循环

Main 维护 STARTING、RUNNING、BLOCKED、CLOSING、COMPLETED、FAILED、CANCELLED。BLOCKED 保留已完成事实与诊断，可接收显式补充输入；没有待定外部输入、预算或合理恢复路径时结束为 FAILED。不能在所有 worker idle 时自动判断流程成功。

事件循环按以下顺序工作：处理/持久化到达事件 → 更新 attempt 和 worker 状态 → 验收 result 并更新事实 → 失效/冲突传播 → 按需请求模型 proposal → 校验并提交新计划 revision → 派发所有当前可运行任务 → 等待消息或最近 deadline。模型请求在独立 coroutine 中，不阻塞 heartbeat/cancel；返回后按 base_plan_revision 再校验。

只有工作流要求的终态事实/检查均成立，且没有未结算业务任务，才可 COMPLETED。模型 finish 建议不能绕过这道门。close 资源失败单独报告 cleanup_status；业务成功与资源清理失败同时存在时，不把两者压成一个误导性的成功标志。

## 6 公共接口与运行边界

以下是拟新增签名，不是可运行实现；省略 import 与完整泛型。

```python
Agent.open_session(options: SessionOptions) -> AsyncContextManager[PersistentSession]
PersistentSession.execute(request: TaskRequest, scope: TaskScope, completion: AttemptCompletion | None = None) -> Awaitable[NativeExecutionResult]
PersistentSession.request_cancel(task_id: str, attempt_id: str) -> CancelReceipt
PersistentSession.inspect(query: InspectQuery) -> Awaitable[NativeSnapshot]
PersistentSession.observe(query: ObserveQuery) -> Awaitable[ObservationSnapshot]
PersistentSession.close(reason: str) -> Awaitable[CloseResult]

ArtemisContext.reset_task_state() -> None
TranscriptLedger.begin_task(scope: TaskScope, system_prefix: list) -> None
TranscriptLedger.export_snapshot(query: TranscriptQuery) -> TranscriptSnapshot
DataEngine.begin_task(scope: TaskScope) -> None
DataEngine.end_task() -> None
DataEngine.flush(timeout_seconds: float, strict: bool = True) -> Awaitable[FlushReport]
ActionSession.wait_idle(timeout_seconds: float) -> Awaitable[OperationDrainReport]
TaskOperationTracker.drain(timeout_seconds: float) -> Awaitable[OperationDrainReport]

MainSession.run(workflow: WorkflowSpec) -> Awaitable[WorkflowResult]
MainSession.observe(role_id: str, query: ObserveQuery) -> Awaitable[ObservationSnapshot]
MainSession.inspect(role_id: str, query: InspectQuery) -> Awaitable[DiagnosticSnapshot]
MainSession.cancel(task_id: str, reason: str) -> Awaitable[CancelReceipt]
MainSession.takeover(role_id: str, reason: str) -> Awaitable[TakeoverResult]
MainSession.close(reason: str) -> Awaitable[WorkflowCloseResult]
```

SessionOptions 固定 device/profile、应用准备、录像/trace、持久化根和取消策略；任务覆写这些配置直接 rejected。安装/解锁/环境初始化仅在 open。业务任务重登或启动 App 仍由原生任务执行。

NativeExecutionResult 包含原生 outcome、输出、末态本地快照、步骤范围、末轮关联、异常分类和操作停止报告，不仅是文本。State 用于本地结算，不通过 IPC 全量同步。

为避免“原生图返回、worker 尚在验收时已可接下一任务”，采用明确的两层边界：

- PersistentSession 提供内部 begin_attempt、run_native、finish_attempt 和 attempt_guard；整个 guard 生命周期内均拒绝第二个任务。
- worker 在 guard 内依次执行 native、ContractVerifier、最终结算和 durable result 写入。
- 公开 execute 组合这几个阶段，允许注入本地 completion adapter。adapter 不在原生层硬编码 Main/IPC；异常走同一收尾路径。
- request_cancel 同时作用于当前 native/verification 阶段；无法取消已提交 result。协议 listener 不直接修改 Context。

close 与 Context 的进入/退出由同一个生命周期 owner 管理；ActionSession 的 anyio transport 继续由其独立 owner task 管理。

AttemptCompletion 是本地 SDK 扩展协议，固定两个 async hooks：verify(ctx, native_result) 返回可序列化 VerificationBundle；persist(ctx, settled_result) 写任务归档和最终结果，返回持久化引用。verify 在第一次操作屏障和步骤 flush 后调用；persist 在最后停止/flush 后调用；两者完成前不释放 attempt guard。SDK 不解释编排协议，只保留 completion_data、settlement_report、persistence_ref。worker 实现这两个 hooks，普通 SDK 未提供时走原生完成语义。persist 失败禁止 READY，进入 FAULTED。

关键内部数据结构按下表实现；均用明确字段模型，禁止 dict 随意扩展跨层协议。

| 类型 | 最少字段与约束 |
| --- | --- |
| TaskScope | task_id、attempt_id、goal、started_at、start_step_exclusive、end_step_inclusive、scope_revision；无步骤时 end 不大于 start，查询返回空 |
| WorkerHandle | role/session/epoch、PID/creation_time、process/connection、last_heartbeat、session_state、active_attempt、pending RPC、日志 drain tasks |
| NativeExecutionResult | native_outcome、output、last_state、step_range、terminal_turn、execution_error、completion_data、settlement_report、persistence_ref |
| OperationDrainReport | producer_count、pending_operation_ids、failed_operation_ids、external_effect_uncertain、drained；未知底层状态不能 drained=true |
| FlushReport | submitted_watermark、completed_watermark、failures、optional_warnings；关键 failures 不为空不得复用 |
| CloseResult | device_operations_stopped、engine_flushed、context_closed、client_disconnected、lease_released、errors；任何未证实项为 false/unknown |
| TaskManifest | 身份、request_hash、原生目标 hash、步骤范围、阶段时间、归档状态、artifact 索引、result_hash、停止/写入报告 |
| WorkflowResult | business_status、各任务结果引用、接受事实、未达目标、失败/阻塞原因、cleanup_status、诊断引用 |

## 7 主子协议

### 7.1 传输与 bootstrap

Main 监听 loopback TCP，每 worker 一条双向 UTF-8 JSONL 连接。使用 asyncio，无额外消息中间件、无 pickle。控制消息与 stdout/stderr 分离，Main 持续 drain 日志管道；不将现有 best-effort SSE 用作可靠控制通道。

通过 sys.executable 启动独立解释器：

```text
python -m artemis.runtime.session_orchestration.worker
```

Windows 隐藏窗口。wrapper 复用 ProcessSupervisor 的环境准备和验证终止能力，使用可传 stdin 的 subprocess 启动；不要求 fork。bootstrap 通过 stdin 传一条有界 JSON 后关闭，包含 endpoint、身份、nonce、临时认证 token。日志不打印密钥。

hello 完成版本和身份认证；初始化/lease 成功后才 ready。首次认证后建立单独 reconnect secret，同 epoch 重连不复用启动 token；Main 撤销旧连接，确保每 epoch 只有一个有效控制连接。

### 7.2 Envelope

```json
{
  "protocol_version": 1,
  "kind": "execute",
  "message_id": "msg-a2-1",
  "in_reply_to": null,
  "sequence": 12,
  "workflow_id": "workflow-1",
  "role_id": "host",
  "child_session_id": "session-a",
  "worker_epoch": "epoch-a-1",
  "task_id": "A2",
  "attempt_id": "A2-1",
  "payload": {}
}
```

示例 ID 为缩写，实际用第 4 节类型。sequence 按发送方/epoch 单调增长，重连不归零，重发原消息保留原值；用于查缺口，去重依赖 ID 与内容 hash。Session 级消息的 task/attempt 为 null。跨进程 UTC 时间不决定全局事件顺序。

严格校验字段、版本、kind、身份、大小；未知关键内容不猜。单帧最大 1 MiB，大截图/视频/日志只传 artifact 引用。控制队列 256，progress 队列 64；progress 可合并，control/result 不可丢。持续背压触发连接故障流程，禁止无限队列。

### 7.3 消息表

| 方向 | kind | 语义 |
| --- | --- | --- |
| 子→主 | hello / ready | 认证 / 已准备接任务 |
| 主→子 | execute | 派一个完整 attempt |
| 子→主 | accepted / rejected | 持久化接收且占位 / 尚未执行及拒绝原因 |
| 子→主 | started / progress | 开始 / 阶段与观察，不是终态 |
| 子→主 | needs_input | 缺项；随后结算 blocked，不悬挂原图等输入 |
| 子→主 | result | 已持久化终态、输出、证据、会话可复用性 |
| 主→子 | ack | Main 已保存 result，引用 message_id 与 hash |
| 主→子 | cancel_task | 取消精确匹配的 task/attempt |
| 子→主 | cancel_received | 收到取消，不表示动作已停止 |
| 主→子 | observe_session / inspect_session | 请求设备观察 / 分页上下文 |
| 子→主 | observation / inspection | 有来源/时间的快照或 unavailable |
| 主→子 | close_session | 停准入、结算并关闭整个 Session |
| 子→主 | heartbeat / session_status / closed | 通信活性 / 状态 / 关闭报告 |
| 双向 | error | 稳定错误码和关联请求 |

takeover_session 是 Main 本地管理 API，组合 cancel/close、退出验证和 lease 转移；不能靠向失联 worker 发一个消息完成。

heartbeat 含状态、active attempt、已持久化事件高水位、最后进度时间、当前操作开始时间、设备健康摘要。它不证明任务推进或业务成立。

### 7.4 TaskSpec

```json
{
  "goal": "创建会议，进入主持人会中页面并取得入会信息",
  "inputs": {},
  "completion": [
    {"id": "meeting_active", "description": "主持人已进入正在进行的会议", "required": true}
  ],
  "outputs_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["meeting_id", "approval_required"],
    "properties": {
      "meeting_id": {"type": "string"},
      "approval_required": {"type": "boolean"}
    }
  },
  "constraints": ["不结束会议", "不要批准未明确指定的参会者"],
  "effect_policy": "verify_before_retry",
  "execution_timeout_seconds": 180,
  "verification_timeout_seconds": 60,
  "settlement_timeout_seconds": 30,
  "max_steps": 30
}
```

请求不可变。inputs 支持 value/source_fact_id，Main 先解析再派发；worker 不读取会被覆盖的共享变量。MeetingID 必须为字符串，保留前导零。至少一个 required completion；信息任务可用“已取得并核验 MeetingID”作为条件。

outputs_schema 首版限定顶层 object、properties、required、additionalProperties=false；字段支持标量、标量 enum、标量数组、nullable。映射为原生 output_format 的 Pydantic 类型并独立校验结果。禁止远程引用、任意 Python 类型导入、递归 schema；不支持就提前拒绝。

effect_policy 为 read_only、retry_safe、verify_before_retry。普通 execute 中 read_only 是业务声明，不是工具权限沙箱；真正无动作的诊断走受限 observe/inspect。模型把创建/发送标为 retry_safe 不足以授权盲重试，代码仍按业务副作用规则限制。

### 7.5 TaskResult

```json
{
  "status": "succeeded",
  "outputs": {"meeting_id": "001234567", "approval_required": true},
  "checks": [
    {"id": "meeting_active", "verdict": "passed", "evidence_refs": ["artifact:contract-screen-1"]}
  ],
  "observations": [
    {"key": "meeting_active", "value": true, "basis": "observed", "evidence_refs": ["artifact:contract-screen-1"]}
  ],
  "blocker": null,
  "suggested_next_actions": [],
  "observed_at": "2026-10-20T11:00:00Z",
  "step_range": {"first": 12, "last": 18},
  "artifact_refs": ["artifact:attempt-manifest"],
  "side_effect_state": "confirmed",
  "session_reusable": true,
  "error": null
}
```

status：succeeded/failed/blocked/cancelled/unknown。check：passed/failed/inconclusive/unchecked。side_effect_state：none/confirmed/uncertain；与任务终态独立，cancelled 也可能已发送消息。

blocker 包含 code、description、missing_inputs、related_roles、evidence_refs。observations 可各带 observed_at，不能把旧证据当新观察。suggested_next_actions 仅供 Main 参考。

error 包含 code、category、stage、retry_hint、diagnostic_ref。至少定义 busy、stale_attempt、identity_mismatch、invalid_contract、device_unavailable、execution_timeout、verification_timeout、operation_not_drained、persistence_failed、archive_failed、worker_lost、output_invalid、contract_unverified。

ArtifactRef 在 manifest 中解析为 artifact_id、child_session_id、task_id/attempt_id（允许 Session 级为空）、relative_path、media_type、size_bytes、sha256、captured_at、source_step_ids。主子同机，通过受限本地读取取得内容；协议只传 ID。路径 resolve 后必须仍在 Session artifact 根内，校验大小/hash 后才提供给 Main 模型。诊断产生的图片可没有 step，但必须有自己的 capture 时间，不能为它杜撰 step。

### 7.6 去重与可靠性

Main 先落盘 request/dispatch 再发 execute；worker 在任何业务动作前先落盘 accepted/request hash。同 attempt 同内容只补状态/结果；同 ID 不同内容报冲突。busy 拒绝不记已执行。

worker 先保存 result 再发送；Main 保存后 ack。丢 ack 补发 result，不重做动作。旧 attempt 结果仅归历史，不能完成新 attempt。重连只同步同一个存活进程，不允许新进程冒用旧 epoch 接续 accepted。

设备操作不提供 exactly-once 保证。动作已发生但结果未持久化就崩溃，状态 unknown；创建会议/发消息先查现场，不能以超时为由直接重试。

## 8 Session 生命周期

### 8.1 状态机

```text
STARTING -> READY -> EXECUTING -> VERIFYING -> SETTLING -> READY
                    |              |            |
                    +--------------+------------+-> FAULTED
READY / EXECUTING / VERIFYING / SETTLING / FAULTED -> CLOSING -> CLOSED
```

Session owner 串行提交状态，control loop 只提交请求。业务失败不必 FAULTED；无法证明停止、关键持久化/归档失败则 FAULTED，拒绝新任务。

Main 的 WorkerHandle 另有 CONNECTED/DISCONNECTED/UNRESPONSIVE/EXITED，不能把断线直接当业务失败。Main task 为 pending/dispatched/accepted/running/terminal；terminal 存 TaskResult。

### 8.2 open 一次

1. 检查 Role/device 映射、Pro/transcript/checker/outputter 有效配置、额度和路径。
2. worker 认证并取得设备 lease；超时失败。
3. 创建一次 Context、ExecutionSetup、DataEngine，start_session 一次。
4. 连接、解锁、安装/准备 App、应用锁初始化一次。
5. 生命周期 owner 进入 Context，按配置启动 Session 录像，发送 ready。

原 DeviceExecutionLock 的全局额度随 lease 持有，包括 idle；必须容纳所需常驻 Role，不能静默禁用限额。部分启动失败，Main 有界关闭已启动 worker 并验证资源释放，防止整组各占资源互等。

### 8.3 begin attempt

1. 验证 READY、身份、请求，落盘 accepted 并占唯一 attempt guard。
2. 确认上一归档提交，旧 producer/ADB/设备调用已结束。
3. 建 TaskScope：身份、原始目标/验收、步骤高水位、开始时间、预算和 artifact 根。
4. 显式重置任务字段，创建 Task 和 State.initial(rendered_goal)。Task.id 用 attempt ID，数据 Session.id 始终 child_session_id。
5. rendered_goal 包含本次目标、输入、约束、completion 原文，记录 hash。
6. get_graph(ctx) 新建图；Operator 首次构建本任务提示时更新 Ledger 边界。

reset_task_state 必须覆盖 planner_task、pending_checkpoints、checkpoint_tasks、checkpoint_attempt_seq、checkpoint_repairs、assert_halt、final_check_attempts、last_validated_plan、pending_validated_plan、guidance_unprotected_checks、guidance_retired_checks、run_outcome、checker_attempt_traces、checker_findings、checker_finding_items。

background_tasks/background_jobs/ADB registry 先停/收割再清理；不能 clear 掩盖活任务。长期客户端、ActionSession owner、录像、StepMemory/chunker 不当普通任务取消。Engine 清当前 step 指针但保留连续序号和历史。

常驻路径不让 Agent._tasks 永久保留每个 Task 的完整末态和输出。当前 Task 加最近 20 个已完成 Task 作为内存窗口，更早任务从 manifest/result 查询；归档成功后释放图、节点、State 和执行 coroutine 的引用。普通一次性 SDK 的任务列表行为不变。Session 历史保留在原生存储和 Ledger 压缩机制中，不依赖无限追加完整 Task 对象。

### 8.4 end attempt 的顺序

正常路径：

1. 原生图终审和 Outputter 返回。
2. 阻止新设备操作，停止/收割本任务规划、检查、ADB 等 producer。
3. 等待 ActionSession barrier 和操作 tracker，确认已有底层调用完成。
4. 提交最后 staged turn，flush 步骤写入，建立稳定验收输入。
5. 独立契约复核，保存对应截图/探测证据。
6. 关闭本 attempt 新诊断请求准入，收割复核/观察操作。
7. 最终记忆/数据 flush、归档、result 落盘。
8. 清 active scope，设置 READY 或 FAULTED，发布 result/status。

取消/异常时跳过不能安全开展的业务复核，但仍有界执行停止、flush、归档；已有事实可保留。不把取消动作记成功。零步骤范围为 null。

结算使用总 deadline，不能逐项超时累加成无限等待。操作未停止或关键写入不可靠则 session_reusable=false。result 落盘失败只能发非持久化 error，Main 保留 unknown 并关闭 worker，不能伪造正常结果握手。

### 8.5 取消屏障

cancel_received 只表示接收。TaskResult.cancelled 表示结算结束，但副作用仍可 uncertain。closed 加退出/lease 验证后才能接管。

cancel_task 精确匹配 task/attempt，旧取消返回 stale_attempt。常驻禁用原 per-task PID/session marker watcher，改用协议定向取消；普通 run_task 保持原控制行为。

ActionSession.wait_idle 在旧 producer 停止后，将 barrier 排到 owner 队列末尾；前面真实工具调用完成才确认。queue.empty 不证明无在途调用。owner 死亡使 barrier 失败，aclose 不得在 owner 未退出时丢失引用或虚报成功；不直接 cancel MCP transport call。

ADB 从 spawn 开始登记，包括短命令和未 handoff 长命令。finally 负责 terminate/wait/必要时 kill；handoff 只转移 owner，不能漏登记或双重管理。to_thread 类操作保留真实 task 引用并 shield，caller 取消后 tracker 仍等待底层结束；无法证明结束则 FAULTED。

停止宿主 adb 不保证设备端任意后台 shell 已退出；可能留下未登记设备工作时记录 external_effect_uncertain，隔离设备再查现场。框架不自动回滚 App 或远端服务副作用。

### 8.6 close 与断线

close 先停准入，结算 active attempt，停录像/操作，记录数据 Session 终态，flush，退出 Context，关客户端并释放 lease，最后 closed。初始化半途失败按已获得资源逆序清理，各阶段记录完成情况以支持幂等关闭。

常驻路径不在任务结束调用原 _finalize_tracing 的 end_session/目录清理。抽取“可选 trace 导出”，正式 end_session 仅在 close 一次；artifact 根稳定，不因导出移动。普通单任务导出/返回语义保留。

断线宽限期内原任务可继续，不接新任务；超期取消并关闭。Main 崩溃后首版只读日志、清理、重观测，不自动重放 accepted。新 worker 必须新 child_session_id/epoch。

## 9 契约验收

completion 原文与 ID 保存在不可变 request.json。不能依赖 Planner 保留原句，也不能只用计划台账模糊匹配。

ContractVerifier 将每个条件构造为原生 CheckItem(kind="verify", when="at_end", parent_key=None)，text 为唯一的 "[contract:<id>] <description>"，复用现有只读 run_final_check。增加可选 review_id 和观察证据回传，原生默认调用语义保持；契约复核有独立命名空间，不与 final#1 混淆。

复核输入包括原任务、当前计划、当前步骤、输出值及来源，截图/probe 保存 artifact。复用 Checker 的只读 history/note/枚举 probe，不授予动作、写笔记、子任务工具。复核不写回原 run_outcome，不自动触发返修；Main 决定是否新 attempt。

逐项严格匹配完整标记文本：缺项 unchecked、重复/歧义 inconclusive，额外项不能满足原请求。每 attempt 最多一次独立复核，独立 deadline，不重复问到通过。文字 evidence 必须关联真实记录，不能凭空生成 step ID。原生证据不可得时标 unavailable。

判定顺序：

1. 中断且效果不明：unknown，或取消场景 cancelled + uncertain。
2. 缺必要输入/验收未证实：blocked；明确必要条件失败：failed。
3. 输出 schema 不符或必需输出缺失：failed/output_invalid。
4. 原生无未解决执行错误、所有 required checks=passed、schema 有效，才 succeeded。
5. session_reusable 独立由收尾报告计算；业务成功不代表会话健康。

Main 再检查 request hash、身份、完整 check 集合、schema、证据可解析性，接受事实后才释放依赖。原生 fail-open、非空 Outputter、进程退出码 0 均不足以单独放行。模型判断仍可能出错；协议保证不跳过验收、证据可追溯，不保证视觉判断绝对正确。关键业务需把双端确认等条件写入契约。

## 10 连续记忆与数据

### 10.1 对话边界

Ledger.begin_task 在无 staged turn 时调用，同 attempt 重入幂等。使用原 renderer 更新含 initial_goal 的系统前缀，追加当前任务/输入来源边界，保留 active/frozen 历史及步骤关联；旧 set_static_prefix 单次语义不变。

每次 Operator 渲染确认 Ledger attempt 与 active_task 一致；不能依赖可能被压缩掉的单条边界消息。末轮从真实末态提取 step/validator_result，复用原提交条件；没有对应 step 时标未绑定，不能误绑上任务末步，tool-call/result 必须成对。

保留原生压缩。同名 subgoal 跨任务的归属由 task/attempt metadata 和边界维持；不能合并验收。若 chunker 公开接口不足，只补任务元数据接点。诊断导出实际保留原文和摘要，压缩不意味着完整原文仍可恢复。

### 10.2 路径与归档

```text
<traces>/<child_session_id>/
  steps/                              连续步骤与图像
  notes/                              普通笔记持续保留
    task_plan.md                      当前 attempt 工作文件
    subgoal_hash_chain.json
    output.md
  check_ledger.jsonl
  check_streams.jsonl
  run_outcome.json
  control/pause
  diagnostics/<snapshot_id>/
  tasks/<task_id>/<attempt_id>/
    request.json / accepted.json / manifest.json
    contract_report.json / result.json
    native/                           本任务工作文件快照

<orchestration_root>/<workflow_id>/
  workflow.json / plan.json / facts.jsonl / events.jsonl
  roles/<role_id>/worker.json
  tasks/<task_id>/<attempt_id>/request.json / result.json
```

原数据库 schema 不变。record_step 在生成调用时捕获 task/attempt 到 extra_metadata，不在异步写入时读可能已变化的 active_task。

Checker/Outputter 取本任务起止步骤并交叉验证 metadata；调用已有 get_agent_friendly_steps_in_range 必须传明确 end，不能误用默认单步或空范围自动交换行为。Operator 可读全 Role 历史；旧 injected_instruction 不自动成为新任务授权/验收。

固定归档清单：task_plan、subgoal_hash_chain、output、check_ledger、check_streams、run_outcome，以及统一写入出口登记的 Checker 专用 artifact。不通配清空 notes/*.md，普通业务笔记保留。

归档状态 PREPARED → SNAPSHOT_WRITTEN → COMMITTED → ACTIVE_CLEARED。所有 writer 停止后，写准备记录、复制并校验、提交 manifest，最后清旧 active。多文件不是一次原子事务，中断按 journal 补完或拒绝新任务。

manifest/result 使用同目录临时文件、flush/fsync、replace；journal 单写者和完整记录边界，损坏尾记录不参与恢复。此约定覆盖进程崩溃窗口，不承诺任何硬件掉电下绝对持久。

### 10.3 flush、暂停、日志、录像

Engine.flush 维护提交高水位和已完成写入失败记录，覆盖 async/thread；当前 done callback 会丢弃任务，不能只检查 pending。等待调用前已提交写入并报告错误；任务结束先停 producer 再 flush。shutdown 复用 flush 后关 IPC，普通路径由调用层保持原容错。

关键步骤/检查/结果写入失败不可复用；可选摘要缺失可降级并记录。晚到日志只有可靠父 trace 时归任务，否则留 Session 级，不能按出队时 active_task 猜归属。

pause resolver：常驻用 Session 路径，普通路径回退 PAUSE_FILE。LLM 等待/重试与 Engine start/end 使用同一 resolver。A 暂停不影响 B，旧全局 UI 按钮不当作每 Role 控制。

录像按 Session start/stop 一次，manifest 保存任务时间范围；idle 可能录像，需工作流时长/磁盘预算，达到预算有序关闭。首版不做隐式切段。close 自动化资源不自动结束设备上的会议，业务收尾必须显式派任务。

## 11 设备观察与上下文诊断

### 11.1 权限

| 能力 | Main 权限 | 设备动作 owner |
| --- | --- | --- |
| 发现、分配、在线状态、lease 查询 | 直接管理 | 无业务动作 |
| 已有截图、计划、工具/检查记录 | 读已发布快照 | 原 worker |
| 新截图/UI | 请求 worker，遵循观察边界 | 原 worker |
| 点击、输入、审批、发送、导航 | 派普通任务 | 原 worker 独占 |
| 故障连接恢复/维护 | 关闭旧 owner 后接管 | 维护 helper / 新 worker |

Main 与 child 不同时拥有设备可写控制权。getter 可能初始化 helper、重连、改索引，不能统称无副作用。

### 11.2 ObserveQuery

字段：mode=cached|fresh，include=screenshot|ui|foreground|connectivity，timeout_seconds。结果含 snapshot_id、capture 起止时间、各组件时间、身份、最近提交 step、consistency、evidence_refs、unavailable_fields。

首版采用保守边界，避免新增图内暂停协议：

1. EXECUTING 的 cached 返回不可变快照和 age/stale。fresh 只允许已审计不更新索引的截图路径，经 ActionSession owner 排队；不调用 observe_screen。
2. EXECUTING 请求 fresh UI 返回 busy_requires_boundary。Main 等任务结束；急需时取消并结算后观察，不能在旧坐标上暂停再继续。
3. READY 观察先取得维护互斥占位，阻止 execute，等待在途操作后采集，再释放；新任务从新观察开始。
4. VERIFYING/SETTLING/CLOSING 默认只提供 cached/持久化快照；已接受观察跟踪到底层完成，不能越过收尾。
5. worker 无响应时 Main 可使用固定白名单、超时受控的 adb 截图/在线查询，不安装 UI helper、不改全局 adb server；标 external_uncoordinated，不是 Context 一致快照。

截图与 UI 可能时刻不同，各自保留时间。fresh 失败明确 unavailable，不能拿旧图冒充新图。运行中截图不得改变动作索引；无法证明的 backend 禁用该能力，只支持 cached 和 READY 观察。

### 11.3 InspectQuery

level=summary|task|history，选择 sections，默认最近 20 轮，每页最多 100 条，返回 cursor/truncation。Main 不序列化 Python Context，不访问 Ledger 私有数组，不默认灌入全部历史。

| 层级 | 内容 |
| --- | --- |
| summary | 当前目标/阶段/计划摘要、最后动作/观察、blocker、后台操作计数 |
| task | 请求、计划、保留模型消息、tool calls/results、检查、动作时间线、异常和证据 |
| history | 指定 attempt/时间/step 的历史分页、笔记、trace、摘要 |

Snapshot 字段：snapshot_id、revision、captured_at、source_epoch、active_attempt、step_high_watermark、consistency、sections、omitted_fields、next_cursor。consistency=settled|stream_checkpoint|best_effort。

SDK 在原 astream values 消费点发布轻量不可变状态；Ledger.export_snapshot 在事件循环内复制可见消息，不跨 await 迭代活容器；DB/文件读取按高水位。来源不能对齐时标各自版本，不声称全局原子快照。分页固定 snapshot_id，不随新消息漂移。

压缩部分标 summary，缺失标 unavailable。导出实际记录的输入输出与工具记录，不伪造不可得的思考过程。过滤凭据/token/key；artifact 访问限定发送 Session 根和 manifest，不允许任意路径读取。

死进程只能读已提交 artifact，不能指望它补导内存。Main 可根据原始观察和记录诊断，不只相信 child 的原因摘要。

## 12 故障与接管

| 情况 | Main 处理 | 会话 |
| --- | --- | --- |
| 原生仍在预算内局部失败 | 原生先修复，Main 看进度 | 保留 |
| 耗尽预算/契约失败 | 冻结依赖，诊断、改计划或有限新 attempt | 收尾成功可保留 |
| 缺 ID/密码/相关 Role 信息 | blocked，补输入/等待事实 | 收尾成功可保留 |
| 心跳正常但无进展 | 看操作耗时和等待条件，到任务 deadline 取消 | 视收尾 |
| 断线进程仍活 | 不重派，同 epoch 重连 | 宽限内继续 |
| 设备断开/无法 drain | FAULTED，禁止新任务 | 关闭/恢复 |
| 进程崩溃无可靠 result | attempt unknown，失效易变事实 | 新 Session |
| result/ack 丢失 | 重发去重，不重做动作 | 可保留 |
| 写入/归档失败 | 保留证据，停止准入 | 不可复用 |

只冻结失败 Role 的依赖，独立任务可继续；全局不变量风险按工作流策略取消相关任务。补偿必须显式任务，不自动猜测撤销。

重试需旧 attempt 已结算、安全停止或换 owner、预算允许、输入齐全且业务可重试。verify_before_retry 先查创建/发送是否已发生；已发生采用事实，不重复；仍不明则 blocked。

takeover 固定流程：

1. Role 标 RECOVERING，停该 Role 和相关依赖准入。
2. 定向 cancel、等待结算、close；超时用 PID+creation time 校验的进程树终止。
3. 验证旧进程/登记操作退出，正常重取 DeviceExecutionLock；不手删锁绕过活 owner。
4. 旧 owner 未退、lease 不可取、设备端遗留操作未排除则 QUARANTINED，不启动新业务。
5. 得到 lease 后由短生命周期 maintenance helper 做白名单观察/连接恢复；Main 管理，原生设备调用在 helper，避免污染 Main 全局状态。
6. 关 helper 释放 lease，再起新 child_session_id/epoch，重新观察登录/会议状态。
7. 通过恢复摘要和已接受事实作为新任务输入；旧 epoch 迟到消息只归档，不复活旧 Context。

控制权转移不证明 App/远端服务异步效果已消失，恢复后仍要核验业务事实。

## 13 原生文件修改清单

当前基线确认 12 个必要接点；文件数不是工期。新增模块见第 3 节。

| 原文件/接口 | 必要代码变更 | 兼容要求 |
| --- | --- | --- |
| third_party/mobile_use/sdk/agent.py：_run_task/_finalize_tracing/取消 | 抽 open/run/close，共用执行主体，Task/Session ID 分离，trace 导出拆 end_session | run_task 签名返回和 Flash 保留；不复制图循环 |
| artemis/sdk/agent.py：tracing/环境 hooks | 稳定 Session 根，一次 start/end，每任务输出初始化分开；常驻取消独立 | 单任务默认不变 |
| artemis/context.py：Context/ExecutionSetup | active_task、operation_tracker、暂停路径、reset_task_state，关闭 helper | 不清长期记忆/设备字段 |
| artemis/data_engine/engine.py | flush 高水位与失败收集、begin/end_task、生成时 metadata、pause resolver | DB schema/storage 不变 |
| artemis/memory/transcript.py | begin_task、export_snapshot、turn 任务元数据、末轮接口 | 旧 prefix 和压缩语义兼容 |
| artemis/agents/operator/operator.py | 按 attempt 更新前缀；抽可复用末轮提交规则 | 模型循环/动作翻译/原提示 renderer 不变 |
| artemis/agents/checker/checker.py | 本任务历史；review_id、观察证据回传、probe 跟踪 | 默认检查行为不变，不扩工具权限 |
| artemis/agents/outputter/outputter.py | 本任务范围和输出归属 | 原结构化输出机制 |
| artemis/graph/checkpoints.py | 统一事件/持久化出口增加 task/attempt/review、登记专用 artifact | 保留真实 trace ID，不改 Role 路由 |
| artemis/mcp/action_session.py | barrier、wait_idle、操作状态、可验证 aclose | anyio owner 规则，不 cancel transport call |
| artemis/tools/command_tool.py | spawn 起登记，取消/异常 finally 清理，handoff 转移 ownership | 参数/返回不变，补资源收尾 |
| artemis/services/llm.py | 所有暂停等待/hold 走 resolver | 模型选择/重试/fallback 主体不变 |

默认不改 graph.py 拓扑、State schema、Planner/Validator 决策、Driver/Controller 接口、StorageManager、DeviceExecutionLock 算法、logger 全局 Engine。每进程只有一个子 Session，无需全层 Role-aware。

条件接点：chunking.py 在测试证明归属丢失时补元数据；supervisor.py 仅在 wrapper 不足以支持隐藏启动/停止验证时补能力。新增接点需记录实际调用链和失败用例。对于尚未覆盖的后台设备渠道，兼容性检查拒绝可复用承诺或标 FAULTED，不能拿 MCP barrier 代表所有渠道都停止。

## 14 配置与启动

新增编排配置，不向所有原生 AgentConfig 塞 Main 字段。Main CLI 拟为：

```text
python -m artemis.runtime.session_orchestration --workflow workflow.json
```

workflow.json 指定目标、Role/device/profile、预算和可选 DAG；应用可用 MainSession.run 同一接口。worker 只能由认证 bootstrap 启动，不让模型构造任意 shell 命令。

| 配置 | 默认/规则 |
| --- | --- |
| startup_timeout_seconds | 120 |
| heartbeat_interval / timeout | 2 / 10 秒；超时先标 unresponsive，不重做动作 |
| reconnect_grace_seconds | 15，超期取消并关闭 |
| execution_timeout_seconds | 任务必填正数 |
| verification / settlement timeout | 60 / 30 秒，可在全局上限内覆盖 |
| close_timeout_seconds | 45，超期验证终止 |
| diagnostic_timeout_seconds | 10；查询超时不自动取消业务 |
| max_attempts_per_task | 2，仍须满足重试规则 |
| max_workflow_seconds / max_artifact_bytes | 工作流必填有界 |
| 帧/队列/分页限制 | 第 7、11 节默认，可配置但设硬上限 |

deadline 用 monotonic clock，UTC 只做报告；worker 执行阶段 deadline 为权威，Main 使用稍大的 watchdog。全部阶段计入工作流预算，业务等待也有限时。STARTING 适用 startup deadline，不因 SDK 导入期间错过 ready 后的 heartbeat 阈值而提前判死；worker 入口尽早完成 bootstrap，再初始化重资源。

启动检查有效 transcript 开启、Checker 未禁用、所需 Outputter 可用、路径可写、额度足够、设备不重复、Session trace/暂停路径正确。不能给 Context 写一个原生加载器不会读取的开关后宣称配置已生效。

## 15 会议调度示例

| 任务 | 派发条件 | 结果和完成条件 |
| --- | --- | --- |
| A1 登录 | A ready | 正确账号登录，凭据不进入事实 |
| B0 登录 | B ready，可与 A1 并行 | B 身份确认 |
| A2 创建会议 | A1 已接受事实 | MeetingID、批准要求、必要凭据引用；主持人会中，不结束会议 |
| B1 提交申请 | A2、B0 | request_submitted 或 joined；不等审批才报告提交 |
| A3 等待批准 B | A2、B0，与 B1 并行 | 找到指定 B 申请并批准，保存证据 |
| B2 确认入会 | A3 已批准，或采用 B1 可靠 joined 证据 | B 确认自己进入目标会议 |
| A4 给 B 发消息 | 已接受 B 入会事实，明确对象/内容/私聊群聊 | 指定消息已发送；发送不等于对端收到 |
| B3 确认收到 | 业务目标要求时 | B 观察对应内容和来源 |
| 显式业务收尾 | 按用户目标 | 结束/退出会议后 close，或只关自动化资源 |

A2 完成后 A 的进程、Context、Ledger、lease 保留。A3 是有预算的业务等待，需要观察；idle 不调用模型。不能等 B 已入会才给 A 派审批。

A3 超时：Main 结合 B1 状态、MeetingID、B 身份和两端截图诊断。A4 超时：先检查发送记录，禁止直接再发。A worker 崩溃时 B 可留在会议，依赖 A 新动作的任务停止，直至新 A Session 重观测恢复事实。

## 16 实施顺序和验收

| 阶段 | 交付 | 门槛 |
| --- | --- | --- |
| P1 生命周期抽取 | SDK open/run/close、PersistentSession、ID 分离 | Pro/Flash 单任务回归；半初始化清理；无复制执行主体 |
| P2 任务边界 | reset/Ledger/flush/范围/归档/暂停 | 同 PID/Session 两任务，新目标生效、历史连续、末轮完整、同名检查隔离 |
| P3 停止可靠性 | ActionSession barrier、ADB tracker、严格 close | caller 取消底层仍执行不 READY；短/长命令取消覆盖；超时 FAULTED |
| P4 单 worker 协议/契约 | TaskSpec/Result、独立复核、协议 | 缺条件/歧义/错误输出不通过；断线和 ack 去重 |
| P5 Main | Scheduler、Facts、Planner、进程生命周期 | DAG、事实时效/冲突、过期 proposal、局部失败依赖隔离 |
| P6 诊断与接管 | inspect/observe/分页/DeviceManager | 不改运行中索引；一致性标记；旧 owner 未退不接管 |
| P7 端到端 | 双 worker fixtures、真实双设备会议、操作说明 | 审批并行，消息不盲重试，长期运行资源有界 |

拟新增 tests/session_orchestration：

| 测试文件 | 核心场景 |
| --- | --- |
| test_native_compatibility.py | 单任务返回、终审、取消、Flash、trace 导出 |
| test_persistent_session.py | 同对象连续任务、目标变更、零步骤、旧停止/检查预算归零 |
| test_task_boundary.py | staged 末轮、tool 配对、归档中断、同名 subgoal、普通笔记保留 |
| test_operation_drain.py | 慢 MCP/owner 死亡、spawn 窗口/短命令/后台 handoff 取消、底层线程未退 |
| test_contract.py | inconclusive 不放行、缺/重复 check、schema 错、证据缺失 |
| test_protocol.py | 身份/重复不同 payload、accepted/result/ack 丢失、重连、背压 |
| test_scheduler.py | DAG 环、all/any、事实失效、运行中计划不可改、预算 |
| test_diagnostics.py | 分页稳定、UI 请求边界、不改索引、敏感字段 |
| test_recovery.py | 崩溃 unknown、PID 验证、lease 冲突、外部效果不明不重发 |
| test_two_workers.py | 两真实 OS 进程配 fake actuator，A 取消/暂停不影响 B，SQLite 归属 |

真实设备记录连续任务数、阶段耗时、峰值/稳态工作集、线程/句柄、后台任务、artifact/录像增长、idle 模型调用。idle 调用必须 0，任务结束登记操作归零，已完成 Task/图/后台 future 不得持续滞留。原生历史和缓存仍可能增长，用工作流时长、步骤及存储预算限制会话规模；完整实例测量后设资源门槛，不宣称常驻 Session 内存恒定。

此前本机仅导入 SDK 的三次观察为约 6.0–10.4 秒、158 MiB 工作集、117 线程，只说明基础成本，不代表完整 Session 峰值或可简单相乘的物理内存总量。

## 17 升级与完成定义

compatibility.py 记录基线、公开接点和行为能力；启动按能力检查，不只比 SHA。补丁按生命周期、记忆边界、验收输入、持久化/暂停、动作停止、诊断维护。升级先查这些接点，再跑对应回归；Main 包独立演进，原生 Agent 不理解 Role。

完成条件：

1. 双设备流程可运行，每 Role 一次启动、连续接任务。
2. Main 按契约/事实/诊断调度，不能凭一句成功放行。
3. 单 Role 故障终态、停止屏障、重试规则明确可测。
4. Main 能观察、诊断，并在旧 owner 退出后接管。
5. 原 run_task/Flash 回归通过。
6. 原生改动有清单，额外接点有失败用例和升级说明。

独立评估线程/协程可参考 [评估提示词](role-session-concurrency-review-prompt.md)，将共同的常驻生命周期改造与同进程额外隔离工作分开核算。
