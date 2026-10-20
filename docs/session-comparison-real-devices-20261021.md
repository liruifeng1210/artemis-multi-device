# Artemis 多进程与常驻协程：真实双设备测试（2026-10-21）

本文区分执行机制、当前功能交付和本次业务验收。`completed`、屏幕业务成功和 Checker `passed` 是三个独立指标。有限样本不支持模型能力、吞吐量或长期稳定性的泛化结论。

## 代码、入口和公平性

起始提交：多进程 `b7045f32ba615f0f7c5fc55fa4d66076e5c8a681`，协程 `f007d82e8f489ce9a3d2f5a2ee7242acc2b14eb6`。共同祖先为 `351ca8422f7b5b54e80a9c1ce03a222e02415b6b`。最终报告提交与实际执行代码提交分别记录，后续补充证据不冒充重测。

| 项目 | 多进程 | 常驻协程 |
|---|---|---|
| Main 是否存在 | `MainSession.run` 默认调用真实 `MainPlanner.propose`，模型生成/修订任务图，Scheduler 验证协议和依赖 | 只有 `RoleSession`；两个原示例均为固定流程，没有已实现的智能 Main |
| 本次入口 | 测试驱动调用生产 `MainSession().run(WorkflowSpec)`，`tasks=[]`；不替换 planner，不预填任务图 | 明确标记为确定性测试驱动，直接调用 Role SDK；不是 Main 能力验收 |
| 取消决策 | 驱动在 Main 派发 `cancel-A` 后计时，通过正式 `Main.cancel(task_id)` 请求取消 | 驱动通过正式 `RoleSession.cancel(task_id, attempt_id)` 取消 |
| 自主取消能力 | 当前 PlanProposal 无模型 cancel 动作；不能把驱动的外部取消时机说成 Main 自主决策 | 没有模型 Main，自主取消编排未交付 |
| 生命周期 | 一 Main + 每 Role 子进程；子进程内复用 PersistentSession | 一解释器/事件循环，每 Role 串行消费队列，共享进程、保留独立 SessionResources |

入口与实际执行源码可按固定SHA复核：[MainSession](https://github.com/liruifeng1210/artemis-multi-device/blob/00708faf1ada903c5dffbfc3b49f4a07c2a35a23/artemis/runtime/session_orchestration/main_session.py)、[MainPlanner](https://github.com/liruifeng1210/artemis-multi-device/blob/00708faf1ada903c5dffbfc3b49f4a07c2a35a23/artemis/runtime/session_orchestration/planner.py)、[多进程测试驱动](https://github.com/liruifeng1210/artemis-multi-device/blob/00708faf1ada903c5dffbfc3b49f4a07c2a35a23/examples/calculator_main_comparison.py)；[RoleSession](https://github.com/liruifeng1210/artemis-multi-device/blob/a2afa8d1a28d848182d65b7caf23b4fd08266555/artemis/sdk/role_session.py)、[协程测试驱动](https://github.com/liruifeng1210/artemis-multi-device/blob/a2afa8d1a28d848182d65b7caf23b4fd08266555/examples/calculator_comparison.py)。这些链接在两个目标分支推送后均可访问。

环境为 Windows 11 build 26200、Python 3.12.14、Codex CLI 0.163.0、ADB 33.0.2；[完整版本快照](evidence/session-comparison-20261021/common/environment.json)包含依赖及计算器版本。A 为 Android 9 / 计算器11.0.1，B为 Android 16 / 计算器16.4.2。两台设备固定分配：A=MI 6 (`21850fc5`)，计算器 `com.miui.calculator`；B=PJZ110 (`a8acf864`)，计算器 `com.coloros.calculator`。两个方案顺序占用这两台设备，没有同时控制同一手机，本次代理和驱动没有手工修正计算结果；多进程B最后报告阶段检测到轨迹外的前台变化，来源未确定，见下文。

统一真实模型为 provider=`codex`、model=`gpt-6-luna`、reasoning effort=`xhigh`。每次调用使用正常已登录 CLI 的 ephemeral exec；未读取、复制或提交登录令牌。真实调用证据来自进程的有限配置字段、LLM trace 和实际返回结果。单元测试中 Google 默认配置 fixture 的占位 key 只存在于测试进程，不用于真机调用。

两分支都提交了[工作区模型/Agent配置](../config/artemis.jsonc)，并在每轮保存解析后的config.json，避免仅根据文档默认值推测生效后端。

每任务保护预算为 900 秒，Checker checkpoint/settlement 配置为 600 秒；CLI 单调用 300 秒，后台摘要 flush 仍有自己的 30 秒限制。修正后的图递归预算为 100，不等于“允许 100 个手机动作”。视频录制关闭，保留逐轮截图。多进程额外有合同复核和 Main 规划预算，故实际验证成本并不完全等同。所有结论以保存的生效配置为准。

MP模型生成任务时还收到execution=900、额外verification=600、settlement=60秒的明确约束；Main单次规划300秒、总工作流4500秒、关闭90秒。CO固定驱动的900秒覆盖handle.result等待，Role cleanup=60秒。公共原生Checker预算相同，额外Main/合同与收尾预算不相同，不能用总耗时直接评价执行机制效率。

### 有依据的局部修复

| 发现 | 修改 | 分类与验证边界 |
|---|---|---|
| 多进程起点没有 f007d82 的已登录 CLI 路由及 Windows MIME 修复 | 仅应用 f007d82 对模型、辅助 LLM、摘要、配置验证和 MIME 的提交差异 | 公共适配；未合并协程架构或覆盖 Main |
| 真实 MainPlanner 返回 `revision/name/description` 等错误字段，65 秒预检超时 | 明确提供 PlanProposal JSON Schema，并将 Main 单次规划预算从 60 对齐到 300 秒 | Main 接入缺口；schema 修复后一次真实返回耗时 40.156 秒，是有效观察请求，**尚非任务派发** |
| 该观察请求使用不支持的 `query.purpose` | 在 Main 提示中声明真实 observe/inspect 参数范围 | Main RPC 接口说明缺口；不改变 RPC 权限或替 Main 构造任务 |
| 首次新测试采用 max_steps=30，A 在 4 个动作后 GraphRecursionError | 驱动改回原示例的 100 | 测试驱动预算错误；原失败样本完整保留，不能作为架构劣势 |
| 协程运行中 Diagnoser 最终 submit_answer 走同步 invoke，被所属事件循环保护拒绝 | 两分支均改为 await ainvoke；加入真实 managed tool 回归 | 协程接入适配缺口，不是协程机制必然缺陷；当前已加载的旧运行不算修复后复测 |
| Object Detector 的旧每次10秒/整体30秒保护提前取消 CLI 调用 | 仅 Codex 后端使用已解析的 endpoint 预算；显式整体 deadline 仍优先并取消/等待子任务 | 公共适配；两分支相同修改，新增预算与取消回归通过，保留旧失败样本 |
| 旧最终审计中 8 条关闭日志缺 device_id | 协程 close scope 补设备归属 | 元数据完整性修复；旧数据不回填，不把缺字段说成串线 |

前端仅验证 HTTP 与 MIME：两个临时服务的 workspace、polyfills、主 JS 均 200，JS 为 `text/javascript`。这不证明不同打包前端的所有 UI 功能相同。见[多进程 HTTP 证据](evidence/session-comparison-20261021/common/frontend-mp.json)、[协程 HTTP 证据](evidence/session-comparison-20261021/common/frontend-co.json)。

## 场景与实际结果

本次冻结的实际执行提交：

| 运行 | 完整 as-run SHA | 条件与定位 |
|---|---|---|
| 多进程完整运行 | `00708faf1ada903c5dffbfc3b49f4a07c2a35a23` | 真实 Main；8任务；[完整逐项结果](evidence/session-comparison-20261021/mp/README.md) |
| 协程首次新运行 | `ac57e305d99f42767b2f7b354b6c29bf15e1e6fc`（[源码记录](evidence/session-comparison-20261021/co-initial/source.json)） | 30图步预算错误；保留失败，不作公平胜负样本 |
| 协程修正后运行 | `a2afa8d1a28d848182d65b7caf23b4fd08266555` | 100图步；新的A/B Session；[完整逐项结果](evidence/session-comparison-20261021/co-corrected/README.md) |

两个新协程运行之间更换Session，不能串接成同一Session成功；每个运行内部四轮保持各自原Session。MP和修正CO均只做一次完整运行，不为追求成功无限重跑。

| 场景 | 多进程真实 Main | 修正后的协程SDK | 判定依据 |
|---|---|---|---|
| A 首轮125+75 | 屏幕通过200；Main cancelled，Outputter超时 | **失败**：GraphRecursionError(100)，当前125+，灰色200为旧历史 | 首轮独立截图、动作轨迹 |
| B 首轮48÷6 | 屏幕通过8；Main blocked，合同I | **通过屏幕业务**：8；SDK completed，2P | 真实点击；[CO截图](evidence/session-comparison-20261021/co-corrected/01-initial-B/screen.png) |
| A 连续×3 | **失败**：仍200，无×3动作 | **失败**：900秒取消，仍125+，未得到600 | 同Session，没有清空重输凑结果 |
| B 连续+17 | **失败**：仍8 | **通过屏幕业务**：25；SDK completed，1P/1I | [CO截图](evidence/session-comparison-20261021/co-corrected/02-continue-B/screen.png)，不是全检查通过 |
| A按任务取消/B同时×4 | 取消机制有观察；B继续8×4=32，完整业务100失败 | 取消机制有观察；B继续25×4=100，**屏幕业务通过**；SDK failed，1P/1F | [CO最终100](evidence/session-comparison-20261021/co-corrected/03-independent-B/screen.png)与Checker结论冲突 |
| A取消后同Session+10 | 接受复用，业务失败；仍200 | **通过屏幕业务**：原Session由125+实际算到135；SDK completed，3P/1I | 先读取真实屏幕，不假设取消回滚 |
| B自身历史报告 | 正确报告首次8/当前32；Main blocked；最终前台不确定 | **失败**：笔记写首次无法确认/当前100；SDK completed但output=None、run_outcome=None | 依据自身历史，不重新计算 |
| 正常关闭、后台及锁 | graceful退出、锁可取；仍有未终结台账 | 两Role closed、锁可取、无存活采样身份；8条running trace台账 | 活进程与DB状态分别核查 |

多进程 Main 最终 **blocked**：TaskResult为4 blocked / 3 cancelled / 1 failed，0 succeeded。原生Graph有6 completed，原生最终检查11 passed / 4 inconclusive；Main合同为1 passed / 1 failed / 7 inconclusive / 6 unchecked。这三套统计对象不同。A首轮和B首轮的屏幕业务分别通过200/8，其余完整业务链未通过。B×4在实际8基础上得到32，证明动作继续执行，不能算用户要求的100通过。

修正CO的Role SDK状态为 **4 completed / 2 failed / 2 cancelled**。四项有完整原生Graph completed outcome，最终检查合计 **7 passed / 1 failed / 2 inconclusive / 0 unchecked**；B最后历史任务虽SDK completed，但没有完整outcome和输出，不能把缺失检查当作通过。算术屏幕通过四项：B=8、B接算25、B接算100、A取消后实际125+10=135；A首轮/连续×3、B历史交付失败。B×4是屏幕通过但Checker失败；A+10与B+17是屏幕通过但检查尚有inconclusive。

每个Task只有一个attempt，没有自动重派整项任务；原生agent内部存在重复动作和诊断尝试，例如CO B×4在A取消后点击乘号两次，再4、等号。所有动作均保留在时间线，不能把attempt=1说成没有重试。该重复点击没有被驱动手工修正。

协程首次新运行：2 completed / 4 failed / 2 cancelled，独立取消0.033090秒返回；B在A取消后继续跑完，但屏幕空白，业务100不通过。A后续同Session接受+10却失败；初始200仅是灰色旧历史。完整原始失败、递归错误和预算见[该次运行](evidence/session-comparison-20261021/co-initial/README.md)。


[本地IPC警告计数](evidence/session-comparison-20261021/common/warning-summary.json)保留运行中的通知连接超时，尚未定位其对业务或延迟的影响；不同初始前台状态也未人为归一。

一加在长时间模型等待时已核实 USB 供电、`Awake`、`mStayOn=true`、保持唤醒值2；没有修改30分钟屏幕超时。[运行中核查](evidence/session-comparison-20261021/common/oneplus-awake.json)、[结束后核查](evidence/session-comparison-20261021/common/oneplus-awake-final.json)。

### 本次遇到的验收与交接问题

- 多进程 B 首轮真实屏幕为8，原生最终检查2项passed；合同报告同时出现带 `- [verify] (at_end)` 前缀的passed和原始合同文本的inconclusive。`ContractVerifier`按原始文本匹配，因此没有冒充合同通过。可见Checker答复和结构化报告的文本身份不一致，是原生Checker规范与Main合同验收的整合问题；本次保留原判定，未清洗成passed。
- 多进程 A 首轮真实得到200、原生检查2项passed，但900秒保护在Outputter阶段触发，终态cancelled；“屏幕算对”不等于端到端任务交付完成。
- 部分连续任务的规划文本声称没有设备交互能力并提前标记计划完成，后续期望600/25未实现。规划阶段的工具集本来有阶段限制；同Session首轮有真实动作，持续历史文件也仍存在。因此不能单凭该文本认定连接已丢失。能力描述、规划决策与复用时工具注册/交接的具体根因尚未确定，不泛化到模型能力或并发机制。
- 多进程B最后历史报告的截图转到“关于本机”，没有对应的测试动作记录，来源未确定；受影响的是该阶段的计算器前台连续性，不能据此推翻此前独立截图中的32，也不把该页当作计算证据。两张含非业务设备信息的原图未公开，只提供[脱敏核查](evidence/session-comparison-20261021/common/external-state-observation.json)和[原图哈希/省略说明](evidence/session-comparison-20261021/mp/evidence-omissions.json)。
- 多进程边界inspect经常已落入下一任务，返回stream_checkpoint、source.active_task_id也指向下一任务。公开导出保留这些标识；这种快照可用于观察Session连续性和新任务状态，不能作上一任务冻结终态。终态判据使用提交后的任务结果、合同截图和检查台账；最后任务的对话另查实际快照截断标志。
- 修正协程B的×4任务实际截图为`25×4=100`，最终Checker却以“屏幕仍为8+17/25”为依据判定`期望100`失败；SDK据失败检查正确返回failed。`checker_success`布尔值仍为true，不能把它当作检查全通过。具体是证据选择、历史图像还是判断错误尚未确定；本次不擅自改写检查结果，原答复和逐步动作均保留。

## 既有证据复核

旧运行使用 A Session `49a0708e-47c8-4a7d-9181-fe243ac11e1f`、B Session `22e26727-d330-4945-95ae-c68eb125cce2`。它没有完整、干净的 as-run SHA 和配置快照，且未使用全部新 Checker 预算，因此只作历史证据，不能与新配置直接比较耗时或通过率。

| 旧场景 | 运行状态 | 屏幕业务 | Checker |
|---|---|---|---|
| A 125+75 | completed | 200 | 0 passed / 3 inconclusive，fail-open |
| B 48÷6 | completed | 8 | 1 passed |
| A 接结果×3 | cancelled，超时 | 未得到600 | 未完成 |
| B 接结果+17 | completed | 25 | 0 passed / 2 inconclusive |
| 独立取消、取消后复用 | 未执行 | 未验证 | 未验证 |

见[旧任务结果](evidence/session-comparison-20261021/old-co/results.json)、[最终重新审计](evidence/session-comparison-20261021/common/old-final-audit.json)。最终数据库有 A 14/B 12 steps，旧审计文件只有 A12/B10，确属未完成时快照。最终审计所报 8 条 owner 错误是关闭日志缺设备字段；尚未发现错误 Session 或跨设备归属的证据。另有 11 条持久化 running trace。running 字段不等于活 OS 工作；既不能据此宣称泄漏，也不能只凭“all data persisted”日志宣称检查台账完整终结。旧 A 乘号/历史恢复失败的根因仍未确定。

## 隔离、连续性、取消和收尾

多进程保持A/B各自的worker和子Session，实际Role解释器PID为35468/39616（Main PID19644；Windows worker启动器另计）。八个新任务起始日志显示临时执行字段重置，持久历史文件保留A=200、B=8→32。两首轮dispatch重叠，确实并发执行。取消A的请求到接口确认0.007191秒，到最终TaskResult0.081508秒；B在此后执行×、4、=三次动作。两者使用正式任务接口，没有PID级停止。

MP取消发生在Planner/LLM等待、首次设备动作之前；排队取消、设备操作中的取消均**未验证**。同Session接受取消后任务成立，+10业务未完成，因此“可再次派发”和“成功复用完成业务”分开判定。未发现旧任务终态后的迟到设备action记录；这是有覆盖限制的观察，不能证明所有外部副作用都被穷尽采集。Main首轮模型一次生成八项依赖图，常规后续派发由Scheduler按该图执行，并非每一轮都重新调用模型；真实结果进入生产Main状态，再触发后续恢复规划。

多进程Main真实消费结果后做了三次模型响应：两次恢复提案违反原角色/验收合同不变式，被Scheduler拒绝；第三次仅请求诊断，重规划尝试耗尽，revision没有推进。以实际生产验证器离线重放可复核拒绝原因，见[审计](evidence/session-comparison-20261021/mp/execution-audit.json)。Scheduler合并提案，**不是因为没有重列原八项任务而拒绝**。当前异常分支只continue，未把具体验证错误反馈给模型，属于Main恢复反馈缺口；本次没有替Main补派恢复任务。

MP原生trace有2158条缺直接device/role/task执行字段，26个检查记录缺直接task字段；可通过Session绑定、step的task/attempt和worker PID关联，不能宣称字段全覆盖。跨Session/step/parent引用错误为0。最后仍有7条running trace：正常进程退出和trace台账完全终结是不同标准。history_chunks是追加版本表，2 pending v1已分别被同范围ready v2取代，当前2个范围全部ready；不能按原始行数误报未完成摘要。最后A/B ledger快照未截断，但只覆盖Operator账本；Outputter和Checker证据另外保存。

修正CO始终使用A `14127587-b5ff-45ad-9943-8879e13864cc`、B `633269ea-9548-43bc-bfed-ad37525de38e`，共用实际解释器PID16572；各Role的context/driver/ledger身份跨四项任务不变，Task/attempt和任务文件归属独立。精确取消0.041169秒返回，A该任务没有设备动作，B随后实际有4次点击并得到100。A又在同一Session读到125+、执行+10得到135，取消后实际复用通过；队列和设备操作阶段取消未覆盖。

CO最终归属错误0、直接device字段缺口0、检查记录task字段缺口0；未发现跨Session/step/parent或check文件错误，也未发现旧任务终态后开始或结束的迟到action。[完整审计](evidence/session-comparison-20261021/co-corrected/execution-audit.json)保留各Task、attempt、设备、进程和边界标识。这不能证明所有语义内容绝不混淆。

CO动态activation采集因目录未建缺失，且该SDK路径没有可解析的graph-start日志（0条），故不能宣称所有新任务临时字段清零/TaskContext身份都被动态直接核实；代码State.initial、新目标和新任务工作区提供部分证据，采集缺口明确保留。最终A/B ledger_has_staged_turn均false，turn_count26/19、retained_runs=0；模板context后台计数0、DB background_jobs=[]。B最后任务没有新增Operator回合，只写“首次无法确认/当前100”笔记，output/outcome均空，**历史交付失败**；摘要落盘与后续Planner可用的历史事实是不同要求。

CO数据库44steps中40条summary ready，4条无action和summary字段；history_chunks为14 pending v1+14 ready v2，按生产最高版本规则当前14个范围全ready。2853 traces有8条仍running，属于终态台账可观察性缺口。MP同样21steps中17条summary ready、4条无action及summary字段；两者都不能仅因无活进程就声称每条trace已正常终结。

两方案关闭后均单独核查PID+创建时间，避免把后来复用的PID误报为残留；再用独立观察进程取得并释放两台设备锁、检查ADB forward、SQLite quick_check和10秒记录数稳定性。[MP核查](evidence/session-comparison-20261021/mp/cleanup-probe.json)、[CO核查](evidence/session-comparison-20261021/co-corrected/cleanup-probe.json)。这种有界核查不能承诺所有未来时刻无写入，也不把持久化running trace抹成完成。摘要必须按Session/step范围选择最高版本后判定，不能混合统计历史pending和当前ready。


| 机制事实 | 多进程 | 常驻协程 |
|---|---|---|
| 内存隔离 | 每 Role 独立解释器和原生全局状态；仍共享文件系统、SQLite、ADB daemon 等外部资源 | 共享解释器和全局资源，依赖 ContextVar、SessionDataEngine、TaskControl、任务工作区和上下文传播 |
| 连续性 | 同一 worker PersistentSession 保留 Context、连接、transcript；进程替换会损失内存并作结果失效处理 | 同 Role SessionResources 和 ledger 跨 assignment 保留；每次新建临时执行状态，历史计划仅为证据 |
| 并发 | 每 Role 子进程执行，Main 通过 IPC 传任务与消费结果；真实计划是否并发须看 dispatch 时间线 | 每 Role 队列串行，不同 Role 通过 await 并发；线程/子进程操作需额外归属和 drain |
| 取消 | 按 task/attempt 发 cancel_task，等待收尾；故障时可隔离/终止特定 worker | 按 task/attempt 取消 coroutine，先禁止新动作，再等已发出的动作、线程和子进程真正结束 |
| 严重故障边界（推断） | 单 worker 崩溃通常可与其他 Role 隔离；设备副作用和共享 DB 故障仍可扩散 | 进程级崩溃、事件循环阻塞或未隔离全局状态可能影响所有 Role；本次没有做真实崩溃注入 |

合同复核属于多进程分支额外实现的编排和验收层，不能据此推导进程机制本身比协程更可靠；协程SDK本次没有新增这一层。Main.cancel 的确认时间与最终任务收尾时间分别记录，不能把接口确认当作已经停止。

取消不是事务回滚。A 后续 +10 必须以取消后真实屏幕为起点。排队、LLM 等待和设备操作阶段的取消分别统计，未覆盖的阶段明确标记未验证。新建 Session 的独立场景不能证明旧 Session 可复用。

## 进程、内存与耗时

| 运行 | 墙钟时间（启动配置→关闭） | 样本数 | 进程数中位/最大 | Python最大 | Codex CLI最大 | RSS总和中位/最大MiB |
|---|---:|---:|---:|---:|---:|---:|
| [多进程真实Main](evidence/session-comparison-20261021/mp/process-metrics.json) | 48.83分钟 | 562 | 11/19 | 5 | 5 | 766.57/1124.93 |
| [协程首次，30图步](evidence/session-comparison-20261021/co-initial/process-metrics.json) | 40.66分钟 | 481 | 5/13 | 1 | 5 | 393.09/848.05 |
| [协程修正，100图步](evidence/session-comparison-20261021/co-corrected/process-metrics.json) | 51.59分钟 | 611 | 5/13 | 1 | 6 | 427.12/950.22 |


方法：psutil 从实际运行解释器开始，采集递归子进程，名义间隔5秒；多进程驱动采集边界截图时可出现较长间隔。数值是**采样最大值**，不是系统真实峰值；RSS 相加会重复计算共享页，不等于 private/unique memory。Main 的 venv 启动器、既有管理控制台、共享 ADB daemon 不在树内；worker 的 venv 启动器、短时 Codex CLI、conhost、部分 ADB 子进程可能在树内。逻辑 Role 解释器数与 Windows python.exe 进程数不能混为一谈。

设备异构、CLI 推理时长、后台摘要、Checker 合同复核、驱动采集以及主机其他负载均为混杂因素。旧失败预算运行和修正后运行分别列出，不据此排序吞吐量或长期稳定性。

## 实现复杂度、上游成本与结论

[共同祖先差异清单](evidence/session-comparison-20261021/common/change-scope.json)中，多进程起点为48文件、+6288/-100行，修改11个已有原生/运行时文件；协程起点为62文件、+4627/-278行，修改40个已有原生/运行时文件。该统计包含文档、测试和不同功能范围，并且共同祖先本身已有产品改动；不能当作纯并发代码量或质量分数。f007d82 的公共适配后来也移植到多进程，最终变化范围应结合本次提交记录阅读。

多进程将许多原生全局状态留在进程内，核心集成接触面相对少，但新增了 IPC 认证、协议、持久化 journal、结果合同、依赖与事实失效、worker 监控和恢复。协程减少常驻 Role 解释器的开销，但必须贯穿数据、工具、日志、后台写入和取消链路维护归属，本次 Diagnoser 同步提交遗漏就是具体整合成本。未来上游合并成本取决于上述接口变化，本文不承诺工期。

确定事实是MP真实Main存在且被使用、CO无Main、设备截图/状态/检查不总一致以及本轮的有界取消和收尾结果。合理推断是MP更容易隔离原生进程全局状态、CO需要更广的上下文传播整合；这些是结合代码的判断。未验证项包括队列/设备操作阶段取消、崩溃注入、长期锁竞争、长期稳定性和规模吞吐。没有证据把本次MI6业务失败直接归因于进程或协程机制。

当前没有一个分支通过完整业务验收。若目标是已有“模型Main→多设备子任务→结果消费→恢复尝试”的交付链，多进程分支是本次唯一具备且实际使用该能力的实现，适合作为继续修复编排/验收的起点；这不等于其机制在稳定性上获胜。若目标是低常驻解释器数量和Role SDK，协程分支具有明确的资源组织优势，但智能Main仍缺失，不能直接当作完整Main/Child产品交付。

优先修复的有证据问题是：原生Checker与合同文本身份不一致、连续任务的计划/工具能力交接、Outputter超时与端到端预算、Main恢复验证错误的模型反馈，以及内存摘要/trace终态的可观察性。没有证据支持仅靠更换并发机制就能修复这些问题。下一轮应针对具体失败节点做有限复测，再增加设备操作阶段取消、队列取消和worker崩溃注入；这些本次未覆盖的能力不作通过承诺。

最终运行时相对共同祖先的已有原生/运行时修改文件为MP20、CO40；本次相对各分支起点涉及12/4个已有运行时文件，见[精确路径与定义](evidence/session-comparison-20261021/common/change-scope-final.json)。文档、测试、证据、辅助脚本不算运行时整合面。功能范围不同，这些数量只帮助评估未来冲突面。

## 复核与复现

下列命令在对应隔离worktree执行；Python使用根仓库已有`.venv/Scripts/python.exe`。模型配置来自保存的config.json，未设置API Key。运行前先确认另一方案关闭且设备锁已释放。

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONUNBUFFERED='1'
# 多进程worktree，使用生产Main，不能改成预填tasks
$env:ARTEMIS_TRACES_DIR='<absolute evidence>/mp/traces'
& E:/workplace/artemis/.venv/Scripts/python.exe -m examples.calculator_main_comparison --scenario all --output '<absolute evidence>/mp'
# 协程worktree，固定驱动；没有智能Main
$env:ARTEMIS_TRACES_DIR='<absolute evidence>/co-corrected/traces'
& E:/workplace/artemis/.venv/Scripts/python.exe -m examples.calculator_comparison --scenario all --max-steps 100 --timeout 900 --output '<absolute evidence>/co-corrected'
# 场景可分别运行；cancellation会创建新Session，不能用来证明旧Session复用
# 两个入口均支持 --scenario continuity / cancellation
```

测试结束后运行`scripts/probe_calculator_cleanup.py RUN`；CO归属用`scripts/audit_role_case.py RUN --database RUN/traces/data_engine.db`；MP用`scripts/audit_main_calculator_comparison.py RUN`解释已记录提案和动作，均不发设备业务动作。`scripts/export_calculator_comparison.py RUN PUBLIC --log LOG`生成筛选证据，随后`scripts/audit_coroutine_comparison.py PUBLIC`审计协程公开快照。公开证据UUID目录缩为8位以兼容Windows长路径，artifact-index保留原完整位置；碰撞会报错。

原始采集实际位置为根仓库`scratch/comparison-20261021/{mp,co,co-corrected}`，历史源为`scratch/calculator-live-20261020`。这些原始位置不作为GitHub证据链接；[公开证据索引](evidence/session-comparison-20261021/README.md)及其相对链接随两个分支提交。

协程修正运行的activation观察器未先创建目录，缺少该批activation文件；本次不因采集器缺口重跑业务。最终提交补充mkdir及显式observer_error记录，但**没有将该补丁算作已真机复测**。MP可复核8条graph起始状态日志；CO该路径没有可解析的起始日志，不能替代缺失activation。CO临时字段清零的动态直接观察保留为未验证，只据代码、新目标和独立Task归属作有限分析。

选定回归及首次失败、重测条件见[回归记录](evidence/session-comparison-20261021/common/regressions.json)。既有“262 passed”不是本次真机验证的替代品，本报告不将它计为本次重跑结果。测试集合有重叠，不相加成唯一测试总数。

只上传经筛选的计算器截图、任务/检查文件、归属/动作时间线和资源采样；不上传 SQLite、全量提示词、系统日志、登录资料或工作区无关文件。截图均需人工复核，输出中本地原始路径仅是溯源字段；本文超链接指向实际随分支提交的文件。每个运行证据目录的 sha256.json 列出文件哈希、大小和相对位置。

原工作区暂存文档、push-to-github.bat、两个 stash 和未跟踪诊断文件不纳入本次提交。未合并到 main、未强推、未创建 PR。
