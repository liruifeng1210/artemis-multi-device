# 常驻 Role Session：实现与运行

本实现从 `351ca8422f7b5b54e80a9c1ce03a222e02415b6b` 增量开发。入口是独立的
`artemis.runtime.session_orchestration`，原有 `Agent.run_task`、Flash 和原生图拓扑保持原入口。
设计背景见 [设计文档](persistent-role-sessions-design.md)。

## 启动

使用项目现有 Python 环境和模型配置，不新增服务、消息队列或数据库依赖。
每个 Role 指定一个不同的本地 Android 设备，使用 Pro/default profile；必须开启 transcript、
Checker、final check、Outputter。持有全局设备额度直到 Session 关闭，因此总额度不能小于 Role 数。
不支持 cloud/bridge 设备。

```powershell
python -m artemis.runtime.session_orchestration --workflow docs/meeting-workflow.example.json --validate-only
python -m artemis.runtime.session_orchestration --workflow docs/meeting-workflow.example.json
```

示例中的设备序列号、账户标识、应用和消息均需替换。示例只说明任务拆分，不会替你配置凭据。
`--validate-only` 只校验协议、输出 schema、Role、依赖 DAG；不会获取设备或调用模型。
执行成功退出码为 0，blocked/failed 等业务未完成状态为 2。
省略 `workflow_id` 会生成新 UUID；已存在的工作流目录拒绝覆盖，不自动从崩溃现场续跑。
设置 `fake_worker: true` 只会运行测试后端，不执行真实设备动作，不能用作真机验收。

## 两层 API

```python
from artemis.sdk.agent import Agent
from artemis.sdk.persistent_session import SessionOptions
from artemis.runtime.session_scope import TaskScope
from third_party.mobile_use.sdk.types.task import TaskRequest
from uuid import uuid4

agent = Agent(device_id="DEVICE_SERIAL")
async with agent.open_session(SessionOptions(goal="会议主持人")) as session:
    result = await session.execute(
        TaskRequest(goal="登录", profile="pro"),
        TaskScope(str(uuid4()), str(uuid4()), "登录"),
    )
    # 下一次 execute 保留 Context、Ledger、StepMemory、设备 lease 和连续步骤。
    # 这个底层 SDK 返回原生执行结果；Main 的 ContractVerifier 负责工作流验收。
```

工作流 API：

```python
from artemis.runtime.session_orchestration import MainSession, WorkflowSpec

main = MainSession()
result = await main.run(WorkflowSpec.model_validate(workflow_json))
```

`run` 在工作流完成、受阻、耗尽预算或取消时关闭全部 worker。
运行期间可从同一 event loop 调用：

| 接口 | 行为 |
|---|---|
| `inspect(role_id, query)` | 默认 20 条消息；snapshot_id/offset/limit 分页，最多缓存 20 个不可变快照 |
| `observe(role_id, {"mode": "cached"})` | 最近已发布状态，不触发设备动作 |
| `observe(role_id, {"mode": "fresh"})` | READY 边界内截图/UI；忙时返回 busy_requires_boundary |
| `cancel(task_id, reason)` | 携带当前 attempt_id，返回接收回执；完成须等终态结果 |
| `takeover(role_id, reason)` | 关闭旧 owner、验证退出、新 epoch 获取原有设备锁、观察当前设备 |
| `close(reason)` | 停止调度，取消/结算任务，关闭进程；超时验证 PID+创建时间后终止进程树 |

Main 不创建原生设备 Context，也不与正常运行的 child 争用设备。
接管后的观察由替换 worker 执行，随后该 worker 常驻接任务；本版没有另外常驻的 maintenance helper。
死进程只能读已提交结果，无法恢复它丢失的内存。

## 协议与生命周期

控制链路是 loopback TCP + 有界 JSONL（单帧 1 MiB），启动密钥走 stdin，重连使用 session secret。
每条消息携带 workflow/role/child_session/worker_epoch/message_id/sequence，
任务消息额外携带 task_id + attempt_id。

`hello → ack → ready → execute → accepted → started → result → ack`。
相同 attempt 的请求内容必须一致；已提交结果直接重放。断线宽限期内重连原进程、重发原消息，
不重新执行设备动作。Main 先持久化结果再 ack；child 在 ack 前保留待确认结果。

worker 内部按 `READY → EXECUTING → SETTLING → VERIFYING → SETTLING → COMMITTING → READY` 执行。
每次重新构建 State 和原生图节点；底层 Context、Ledger、DataEngine、设备 lease 不重建。
取消和超时必须等待原生生产者、主机命令进程、MCP owner 屏障和持久化写入。
任何不能证明完成的结算都会进入 FAULTED，不发布“可复用”。
SETTLING 期间的取消会被记住并写入取消终态；COMMITTING 开始后返回 already_committing，
等待已确定的终态。取消 SDK execute 的调用协程也会先完成收尾，再向调用方抛出 CancelledError。
Worker 刚接受、尚未启动原生执行的窗口同样支持定向取消，此时副作用状态为 none。

每个任务包含显式 completion 条目、typed outputs schema、执行/验收/收尾预算和副作用策略。
支持 string/integer/number/boolean、这些标量的数组、enum 和 nullable；不支持任意 JSON Schema 引用。
MeetingID 应定义为 string，保留前导零。
独立 Checker 按原始 contract ID/文本验收，不能只凭 Planner 的完成判断。
Main 校验全部 required check、schema、证据路径、manifest 和内容哈希后，才将 outputs 接受为 facts。
绑定输入保存 source fact/task/attempt/session/observed_at，防止来源被覆盖。

## 故障与恢复

Role 的失败只冻结依赖它的任务，其余可执行任务继续。
只有显式 read_only/retry_safe、结算成功且无不确定副作用的失败允许有限新 attempt。
取消、进程丢失和有不确定副作用的动作不会自动重放。
`effect_policy` 是调度与业务重试声明，不是对任意原生工具代码的安全沙箱。

MainPlanner 获得已接受 facts、结果和诊断快照，可通过 PlanProposal 请求更多 inspect/observe，
调整未执行任务、请求故障 Role 重启，以及提交 recovery 映射。
调度层拒绝过期 revision、环、修改已执行任务、无来源输入和不合规恢复：
恢复必须保留原 Role、required 条目及输出 schema；不确定副作用只能先做 read_only 状态核验。
原失败结果永久保留，恢复另建 task，不把 unknown 改写成 succeeded。
进程退出或接管时，按旧 child_session_id 失效其已接受结果，包括没有输出字段的“已登录”等状态。
失效沿依赖传播到已经执行的下游任务；历史结果保留，result.json 的 invalidated_tasks 单独标记。
依赖失效时向仍在执行的下游 attempt 发定向取消；即使取消回执丢失，也不会接受其迟到结果。
旧进程迟到的成功结果只保留为证据，不重新进入调度事实。恢复另建 read_only 核验任务，
并将未执行下游任务的依赖/输入重新绑定到新任务；不能直接用旧成功状态解锁下游。
模型不能用 finish 字段绕过所有任务的验收。

业务状态与 cleanup 分别记录在 result.json。cleanup 中包含进程退出码、child close report 和
graceful 标志；强制终止不会伪装成正常关闭。
超时或放弃等待的 execute 不再在重连时重发，所属 worker 被隔离。
心跳监控与模型调用独立；新 worker 收到 ready 前使用启动预算。
启动、接管和关闭通过生命周期锁协调，关闭不会遗漏正在启动的替换进程。
Worker 收尾失败时不释放 lease：优先由 Main 回收；Main 不可达时自行强制回收后代和自身。
若操作系统拒绝终止后代，保留故障 owner 和锁，不能宣称已释放设备。

## 数据与升级

原生 SQLite schema、StorageManager、设备锁算法、Driver/Controller 和 graph.py 不变。
不同进程沿用原生 SQLite 并发能力，以 child_session_id 区分数据。
新增 session_storage.native_storage_startup 仅用 OS 文件锁串行保护 DataEngine 初始化和
首个 Session 的落盘，避免新库并发切换 WAL 模式报 database is locked；正常任务写入继续并发。
锁随进程退出由 OS 释放，不更改 StorageManager、连接参数或 schema。
步骤 extra_metadata 增加 task/attempt/scope_revision；Checker/Outputter 只读本任务步骤并校验归属，
Operator 仍读 Role 连续历史。暂停文件位于各 Session 的 control/pause。

每个 attempt 在 tasks/task_id/attempt_id 保存请求、接受记录、独立 contract 报告/截图、
native 工作文件快照、manifest、result。归档为 PREPARED → SNAPSHOT_WRITTEN →
COMMITTED → ACTIVE_CLEARED；不完整归档拒绝接新任务，普通业务笔记保留。
任务工作文件归档属于 SDK 生命周期，无 ContractVerifier 的直接 SDK 调用也执行同一归档流程。
manifest/result 使用临时文件、fsync、replace；不承诺硬件掉电或远端服务异步效果的原子性。

原生补丁集中在以下接点，升级时先复核这些接口：

| 文件 | 接点 |
|---|---|
| third_party/mobile_use/sdk/agent.py | open_session、共享 _execute_pro_task；一次性调用仍走原清理流程 |
| artemis/context.py | reset_task_state、active_task、operation_tracker、Session 暂停路径 |
| artemis/data_engine/engine.py | begin_task/end_task、严格 flush、写入时捕获归属 |
| artemis/memory/transcript.py | begin_task、保留历史并切换 goal、只读导出 |
| artemis/agents/operator/operator.py | 任务边界与终态 turn commit |
| artemis/agents/checker/checker.py | 当前任务步骤、独立 contract review、只读 probe 跟踪 |
| artemis/agents/outputter/outputter.py | 当前任务步骤 |
| artemis/graph/checkpoints.py | 检查事件归属及专用工作文件登记 |
| artemis/mcp/action_session.py | wait_idle、确认 owner 关闭 |
| artemis/tools/command_tool.py | 操作追踪、主机进程从 spawn 开始登记 |
| artemis/services/llm.py | Session 级 pause 解析 |

新 SDK 生命周期在 persistent_session.py/session_boundary.py；调度、协议、facts、诊断和进程管理
全部在 session_orchestration 包，原生 Agent 不理解 Role。
共享归档与路径处理在 runtime/session_artifacts.py、session_paths.py，SDK 不反向依赖工作流包。

### 复核后的升级约束

本轮修复没有扩大上述 11 个原有源码文件的接入范围，也没有新增运行时依赖。
“最小改动”指限制原生接入面，不代表包装层可以省略取消、落盘和进程回收的正确性保障。
升级 Artemis 时重点检查共享 Pro 执行方法、Context reset、Transcript turn 提交和 DataEngine flush
的调用顺序；不要另拷贝一套 Operator、Checker、图或数据库实现。
新增原生后台任务、命令启动方式或 MCP owner 时，必须确认它们加入 settle_task 的等待范围。

测试运行：

```powershell
python -m pytest tests/session_orchestration -q
```

此目录覆盖真实 OS 进程/TCP、重连去重、定向取消、崩溃隔离、接管、原生 SQLite 归属、
连续 Context/Ledger、MCP 屏障、写入失败、契约证据和诊断分页。
还覆盖两次 SETTLING 内取消、重复取消调用方、无输出状态失效、失效依赖传播、
接受后启动前取消、过期恢复提案、启动取消、接管与关闭竞争，以及孤儿进程树清理。
这些测试不调用真实会议应用或付费模型；发布前仍须在两台独占设备上跑实际会议流程。
本版不支持跨主机、Flash 常驻执行、Main/worker 崩溃后的自动内存续跑。
诊断每份快照最多捕获 2000 条保留消息，并显式标记截断；原生压缩已丢弃的原文不会被恢复。
