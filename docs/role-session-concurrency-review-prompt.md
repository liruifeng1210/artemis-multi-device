# Artemis 多线程和协程方案评估提示词

以下内容可直接交给能读取仓库的模型。要求基于代码独立评估，不以当前多进程设计结论作为前提。

---

请基于 Artemis 仓库实际代码，评估“多线程”和“单进程多协程”能否以较小改动支持 Main Session 管理多个常驻 Role Session。请不要预设必须选择其中一种，也不要只做概念比较。只写分析和设计，不修改实现。

## 代码基线

基线是 `351ca8422f7b5b54e80a9c1ce03a222e02415b6b`，不包含此前 Braden Lee 的多设备改造。请以此基线为准，不把旧的共享图 Role 切换方案当作必要设计。先核实当前工作树，不能自行重置或覆盖本地修改；必要时只读查看该 commit。

如果你不能访问仓库，请明确说明哪些只能做概念分析，列出需要读取的文件，不要编造文件内容、行号、实验结果或改动数量。

## 目标和优先级

1. 最小化对原生 Artemis 的修改，便于持续合并上游更新。
2. Main Session 可以自由实现；子 Session 尽量复用原生 Planner、Operator、Validator、Checker、记忆和数据管理。
3. 每个 Role 对应一个常驻子 Session，固定绑定设备，保留对话、记忆和连接，连续接收多个任务。
4. 子 Session 只与 Main 交互，每个子 Session 同时最多执行一个任务；不同子 Session 可以并发。
5. 任务完成不等于 Session 关闭。历史继续保留，但新任务不能复用旧坐标、待执行动作、完成标志、检查预算或检查结论。
6. 普通一次性 run_task 保持兼容。首先评估 Pro，不必为证明方案而重写 Flash。

不要把“同一份图 State 切换 Role”与“多个独立 Context、图、节点和 DataEngine 在同进程中执行”混为一谈。也不要把常驻进程中反复启动全新会话，当作满足了连续对话需求。

## 业务例子

A 登录 → A 创建会议并返回 MeetingID → Main 同时派 B 提交入会申请、A 等待并批准 B → B 确认入会 → A 给 B 发消息。

A 的全部任务在同一个常驻 Session 中完成。必须区分“B 已提交申请”和“B 已经入会”，避免 Main 等 B 入会才通知 A 审批的循环等待。A2 完成后会议仍在，Session idle 不继续调用模型；A3 等待申请则是一个有预算的观察任务。

## 必须检查的代码与问题

- third_party/mobile_use/sdk/agent.py：run_task/_run_task 的初始化、图执行、取消、录像、收尾与设备锁；Task ID 和 Session ID 的关系。
- artemis/sdk/agent.py 与 artemis/context.py：Context 创建/关闭、资源归属、实例字段及共享可变配置。
- artemis/graph/graph.py、state.py：图与节点是否每个 Session 独立；哪些状态是任务级，哪些必须跨任务保留。
- artemis/data_engine/engine.py、storage.py：_CURRENT_DATA_ENGINE、current_session_id/current_step_id、后台写入、SQLite 锁与连接、flush/shutdown。
- services/llm.py、tools/tool_wrapper.py、utils/notes.py、third_party/mobile_use/utils/logger.py：对全局 Engine 的直接或间接访问。
- 特别检查日志入队时和后台线程出队时的归属：如果出队时才读取全局 Engine，怎样保证迟到日志仍属于原 Session？
- ContextVar 在 asyncio Task、to_thread、手工线程、线程池、第三方回调与 queue consumer 中的传播边界；绑定隔离不等于可变对象自动复制。
- runtime/cancel_requests.py、device_lock.py、config/paths.py：PID 级取消、按 PID 查找设备锁、全局 PAUSE_FILE、环境变量和全局并发额度。
- controllers、drivers、mcp/action_session.py、tools/command_tool.py：设备客户端、动作队列、后台 ADB registry 和清理是否按 Context 独立。
- 同步 I/O、CPU 图像工作是否阻塞事件循环；调用方取消后，底层设备操作是否仍在执行。
- agents/operator/operator.py、prompts.py、memory/transcript.py：当前目标写入静态系统提示后，下一任务如何更新目标且保留历史；如何提交最后一轮 staged 对话。
- graph/checkpoints.py、agents/checker/checker.py、agents/outputter/outputter.py：任务台账、旧指导指令、历史证据范围、检查 ID 与报告隔离。
- 如果线程方案采用“一 Role 一线程、一事件循环”，说明客户端、Future、Lock、MCP cancel scope 是否跨循环，如何通信和关闭；不要只说加锁即可。
- 区分合理共享的基础设施与必须独立的业务状态，不要求所有缓存都拆分。

## 输出要求

1. 分别给出多线程和多协程的对象所有权图、调度模型、主子协议与任务交接方式。
2. 列出必须修改的具体文件、函数/接口、修改原因，以及支持判断的实际代码位置。
3. 将工作量拆成三类：所有常驻方案都需要的生命周期改造；线程/协程额外的隔离改造；可以延后的功能。
4. 明确区分确定存在的问题、合理推断、尚需实验验证的问题。最小复现须说明是真实原方法、模拟对象还是完整集成，不能混称为验证通过。
5. 说明哪些原生执行逻辑保持不变，哪些扩展点可集中维护，哪些改动容易与上游冲突。
6. 提供最小验证方案：并发日志/trace 归属、独立取消、设备归属、目标切换、后台收尾、检查隔离、长时资源增长。
7. 和“每 Role 一个常驻进程”的方案比较资源、故障隔离、通信、实现复杂度与升级成本。进程方案也需要 Session/Task 生命周期分离，不能把这部分成本只算给协程。
8. 给出你推荐的最小补丁边界和放弃该方案的明确条件。若没有数据，不要凭空给出精确工期、内存节省比例或性能倍数。
9. 不要把“存在全局变量”直接等同于“必须重构整个系统”，也不要把“换成 ContextVar”当作完整解决方案。
10. 结论必须以“最小侵入、便于升级”为首要权重，同时说明相较于多进程真正节省了什么，又新增了哪些持续维护责任。

可对照本仓库的 persistent-role-sessions-design.md，但应独立核实其事实和改动清单，指出遗漏或过度设计。
