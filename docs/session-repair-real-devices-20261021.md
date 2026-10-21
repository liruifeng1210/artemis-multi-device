# 多进程 Main 修复与快速实机验收（2026-10-21）

本报告承接[首次真实设备比较](session-comparison-real-devices-20261021.md)。用户最终缩小范围为：只完成多进程修复、快速验收、文档和提交推送；停止协程复测及推送。本文不更新协程结论，不把已有协程文档中的 MainSession 当作实现。

## 结论

已完成多进程修复和一次受限快速实机验收，但**完整连续业务未通过**。最新实机显示A=200、B=8，原生completed为2，Main交付1 succeeded/1 failed；B因测试额外限定首次清除次数而失败。Main生成了不可满足的`first_result == null`依赖，导致两个续算和一项恢复未派发；随后已定位为依赖契约缺口并局部修复。最终补丁仅做针对性回归，按用户额度和时间限制未再真机重跑，不能把此前实测SHA冒充最终代码全通过。

已实机验证的收益包括：typed输出收尾、canonical检查身份、独立Checker模型trace恢复；完整run2另有按任务取消、另一设备继续、同Session取消后复用和正常进程收尾证据。剩余未覆盖项在下文列明。

## 固定版本、设备与执行方式

| 运行 | 干净启动的源码 commit | 范围 |
|---|---|---|
| 修复 run1 | `9259864b96dcb1a8a4838ad991b80ec1afdb0fa9` | 完整场景，保留失败 |
| 修复 run2 | `2d0d1afb119c5f3fa373bc2e6f50ca62a3a3356a` | 完整场景，取消及复用已实际覆盖，最终超时 |
| 最终快速验收 | `77e96f1`（完整值见 source.json） | continuity：四项计算；不重复长取消任务 |

代码修复提交为 `ecb4b9594ecdb13b58d5c1dca5d3edf150cf151d`，随后 `77e96f1` 仅扩充脱敏证据导出。原始运行不热改、不回写 verdict。最终文档提交会晚于上述实测 SHA。

A：MI 6，`21850fc5`，Android 9，`com.miui.calculator`。B：PJZ110，`a8acf864`，Android 16，`com.coloros.calculator`。同场景每 Role 使用同一个常驻子 Session、连接与历史。两个场景之间新建 Session，不能用新 Session 证明旧 Session 可复用。测试没有人工操作手机修正结果；两台均 USB 供电，B 观测为 Awake / mStayOn=true，原 1800000ms 熄屏设置未修改。

所有实际模型为正常已登录 CLI 的 `codex / gpt-6-luna / xhigh`，单次超时 300 秒；没有读取、复制或提交登录令牌。历史设计按用户指定由 astra/high 完成，代码由 gpt-6.1-sol/high 完成；用户禁止 astra 后未再调用。实际设备模型配置保存在各次 config.json，不以 mock 测试替代。

入口是生产 `MainSession.run(WorkflowSpec(tasks=[]))`：真实 MainPlanner 从总体目标生成任务图，Scheduler 验证并派发，Main 消费真实结果并决定下一步。`examples/calculator_main_comparison.py` 仅负责启动、观察、边界截图、采样、预算保护及正式 Main.cancel；没有预填 TaskSpec 或替 Main 派发流程。

```powershell
# 工作目录：最终源码的独立 worktree（当时为 scratch/compare-mp-next）
$env:PYTHONUTF8='1'
$env:PYTHONUNBUFFERED='1'
$env:PYTHONPATH=(Get-Location).Path
Remove-Item Env:GOOGLE_API_KEY -ErrorAction SilentlyContinue
E:/workplace/artemis/.venv/Scripts/python.exe -u examples/calculator_main_comparison.py --scenario continuity --output E:/workplace/artemis/scratch/mp-repair-20261021/acceptance
# run1/run2 同一入口使用 --scenario all，分别输出到 run1/run2。
```

运行保护仍为：整体 4500s、每任务执行 900s、独立合同检查 600s、settlement 60s、CLI 300s、graph max_steps=100、启动 180s、关闭 90s、视频关闭。不同预算对应不同阶段，不能合并理解成一个任务超时。快速验收通过减少场景数节省额度，没有放宽检查或改变模型。

最终快速验收后的依赖补丁为`9638b06e953570d8c9ef34a341fe94d9dc0ca1c8`；最后仅增加导出白名单及本报告。最终源码与实测源码不同已明确列出，不能混用。

## 已解决的问题与边界

| 问题及真实证据 | 修复 | 验证边界 |
|---|---|---|
| 初始 Planner 保存待执行计划后继续覆写为全完成，Operator 被跳过 | 有效待执行 task_plan 立即交接；拒绝初始全完成计划 | 原生规划链路问题；驱动不接管计划 |
| typed Outputter 保存报告后仍反复调用模型，撞执行预算 | 严格校验 finish_output 后终结，禁止同批后续覆盖 | run2 正常输出约31–74秒，不取消独立合同检查 |
| A 已600，但合同文本被模型加包装并改写，原项被补 inconclusive | 当前 canonical 身份枚举、完整唯一覆盖；最多一次身份纠错；合法首轮状态冻结 | 不按近似匹配猜通过，不要求模型输出 passed；冲突、耗尽仍保守失败/不确定 |
| Checker 结构化变换丢失 Runnable callbacks，独立检查无模型输入 trace | 保留原配置、按同一 context 去重回调、保持准确父 trace | 不改写旧运行缺失记录；真实 CLI 协议已覆盖一次回调和父/任务归属 |
| Main 使用不支持的 outputs_schema，多轮纠错；诊断后误增 revision | 明确支持的 schema 子集和标量示例；拒绝反馈包含实际 revision、诊断未提交回执 | 不静默删除 schema 约束，不放宽恢复合同 |
| read_only 恢复任务仍写业务 note | TaskScope 携带 effect_policy，工具执行前拦截设备动作、shell、任意 Python 和业务笔记写入 | 允许当前 task_plan 及内部记账/typed 输出；正常任务恢复原权限；最终实机未单独注入此故障 |
| run2 两项只读恢复各900秒：Operator 反复读取旧证据却不完成计划交接 | 一次有边界的真实反馈；仍不更新待执行计划则明确失败 | 不自动勾完成、不伪造 Checker；真实生产 graph 成功/失败回归均覆盖 |
| 故意取消导致整体反复恢复；后续任务缺少可信终态交接 | 白名单+预声明策略+精确 task/attempt 的正式取消记录；同 Session 注入 settled 历史 | 任务仍是 cancelled，不能冒充业务成功或 Checker passed |
| 取消、后台写入、任务切换时 trace 残留或归属旧 step | 捕获不可变 task/attempt owner；先排空生产者再终结；终态防止被迟到 running 覆盖 | 收尾失败不能伪造已结束；瞬时 retry 事件终结为 terminated/duration=0，不标 success |
| 最终截图正确但旧 XML 值冲突、Session历史被误解成 app 历史页 | 最终 Checker 使用本次截图及同图 OCR；明确 Role 历史语义 | 未部署新 APK；本轮未配置 OCR 的地方只证明截图来源 |

Planner/Operator/Checker/Outputter 和 CLI/MIME 属原生链路/公共适配；恢复 revision、预期取消、只读合同属于 Main 编排；进程生命周期与作用域归属属于并发机制。不能把这些根因统称为进程隔离问题。

## 分场景实测结果

### 完整 run2：保留失败，同时证明取消及复用

07:16:50–08:31:52（Asia/Shanghai），75.02分钟。Main 首次自主接受8任务，后续两项恢复也真实接受；最终整体 TimeoutError。结果为6 succeeded /1 blocked /3 cancelled。前8个原定任务为6 succeeded /1 blocked /1预期取消，不能称完整工作流通过。

| 场景 | 实际业务与连续性 | 独立合同 / Main结果 |
|---|---|---|
| A首次 | 一次清空，125+75=200；误输入多一个5后退格纠正，无人工介入 | 2 passed / succeeded |
| B首次 | 一次清空，48÷6=8 | 2 passed / succeeded |
| A续算 | 同Session仅×、3、等号，实际600 | 原声明2 inconclusive；额外改字的2 passed不能充当原项覆盖 / blocked |
| B续算 | 同Session 8+17=25 | 2 passed / succeeded |
| A长任务取消 | 正式 Main.cancel 精确task/attempt accepted；0设备动作 | LLM等待阶段取消 / cancelled，非业务成功 |
| B独立继续 | A取消后仍点击25×4=100 | 2 passed / succeeded |
| A取消后复用 | 同子Session重读600，再+10=610 | 2 passed / succeeded |
| B历史报告 | 自身历史首次8、当前100，0新设备动作 | 3 passed / succeeded |
| 两项Main恢复 | 读取旧A步骤/截图，未产生新设备动作；第1项违规写业务note；重复读取/交接失败 | 各自900s任务预算取消；随后整体4500s耗尽 |

取消发生于07:44:42.332，精确 attempt 的模型 span 与取消重叠；ack约0.0083s、结果约0.0726s。B在取消后完成3次新操作。A长任务没有已执行的+1，因此不证明“部分设备动作后取消”的场景。排队及设备操作阶段取消均**未验证**，不增加重跑。

原生检查原始计数22 passed /3 inconclusive，另含一条额外改字passed；独立合同原始15 passed /2 inconclusive，其中A续算的额外2 passed不计声明覆盖。原生 completed、真实屏幕业务成功、独立合同 passed、Main succeeded 分开判定，未删除 fail-open 或不确定记录。

归属审计3352条trace，完整task/attempt载荷3321条；其余31条为生命周期等记录。检测到的跨Session/task/step冲突为0，未记录终态后迟到动作。旧样本有1条 llm_retry/retrying 遥测残留，不等于活后台生产者，最终补丁修正其记录语义。旧数据库不回写。

run2 最后对话A56/B59条，分页完整，快照均晚于各自最后终态；各有1处字符串截断，不称无损全文。17条compression：16 applied、1 ready/terminated；12条明确使用摘要，不能把其余applied或ready自动算摘要换入。A摘要覆盖至step25而最终32，B至11而最终17；末轮全部摘要替换**未证明**。

run1（06:40:42–07:08:58，28.28分钟）只执行3/8项：A实际200 blocked、B8 succeeded、B续25 blocked，其余未执行。旧身份匹配、截图/层级冲突及revision反馈缺口已修复；失败证据同样公开，不拿该轮证明取消。

### 最终快速验收

08:43:47–09:00:56（Asia/Shanghai），约17.15分钟，真实Main四任务提案。后续接受一项恢复，共5项已登记任务；最终blocked，1 succeeded/1 failed/3未执行。无外部超时、无人工修正。

| 任务 | 真实业务 | Native completed / native检查 | 独立合同 | Main |
|---|---|---|---|---|
| initial-A | 125+75=200，10动作；首次清除后有模型输入纠正 | completed /6 passed | 3 passed | succeeded |
| initial-B | 48÷6=8，7动作；重复点了清除 | completed /4 passed | 2 passed、1 failed（清除次数） | failed |
| continue-A | 未派发 | 未执行 | 未执行 | 未执行 |
| continue-B | 未派发 | 未执行 | 未执行 | 未执行 |
| initial-B-reconcile | Main接受恢复映射，但未派发 | 未执行 | 未执行 | 未执行 |

这里的“清除一次”是旧测试入口附加的第一轮初始化限制，不是用户禁止第二轮重输旧值的要求。最终入口已修正首次初始化描述，仍严格禁止续算清空、重输；旧合同与失败未回写。不能把该修改算作新实机通过。

两个续算及恢复的依赖都含`output_key=first_result, equals=null`；实际输出为字符串200/8，无法满足。Main没有忽略该条件强行操作，这个安全行为正确；缺口在于接受了与所声明输出类型不相容的依赖、反馈未帮助模型修正。最终修复提交`9638b06e953570d8c9ef34a341fe94d9dc0ca1c8`：Scheduler在接受图之前校验依赖键确为来源声明字段，equals符合对应类型；非nullable字符串不接受null。合法显式nullable null过滤仍保留，缺失键不再冒充null。Main schema/prompt明确普通状态依赖应留output_key/equals为空；错误反馈进入既有纠错路径。20项定向mock回归全部通过（0.57s），真实Main.run测试证明坏提案拒绝→修正接受→A续算与B恢复均派发，B原失败历史仍保留；没有可执行计划时仍正常blocked，不忙循环。未修改run loop、未忽略真实条件；最终补丁未再次真机。原先怀疑旧ready快照，经代码和计划核查排除：循环会重新计算ready。


## 收尾、隔离与资源

run1/run2 两个worker均 graceful exit0、lease释放；独立清理探针重新获取并释放两台设备锁，采样PID+创建时间身份无存活者，SQLite quick_check=ok，10秒计数稳定，无ADB forwards。该观察有时间边界，不能证明无限未来没有写入。共享ADB及原端口8000 UI是测试前进程，未停止。

run2 866个约5秒进程树样本：总进程峰值19、Python峰值5（包含venv启动器）、Codex CLI峰值6，RSS之和峰值约1260.28MiB；run1 327样本、19/5/4、1095.54MiB。它不是私有物理内存，CLI子进程、Main规划、独立合同、模型速度和当时并行mock测试主机负载均是混杂因素。不能据此推断吞吐量或长期稳定性。

最终验收1094条trace，0 running，检测到的Session/task/step、check台账归属冲突及设备元数据缺口为0；无记录到的终态后迟到动作。4个最终Checker（含2个独立合同）共16次真实模型输入均可追踪，缺失0；这一点真实验证了run2缺失合同回调的修复，不仅是mock。

最后原生对话A49/B40条、分页完整，均在终态后采集、无staged turn；各有1处诊断字符串截断，不称无损全文。只有1个最新ready摘要，不能声称末轮对话全部完成摘要替换。两个worker graceful exit0、设备租约释放，独立锁获取成功、无采样后代存活，数据库10秒稳定且quick_check=ok。日志出现既有IPC端口连接超时警告，数据仍落盘；未用此警告推断跨Session污染。

197个约5秒采样：总进程峰值17、Python5、Codex CLI5、RSS之和峰值1114.08MiB（1168195584字节）。最终实机结束后才运行新的局部修复回归；未让两个方案同时控制手机。


## 回归与真实协议

最终MP广集402项：398通过、4个fixture/旧行为测试失败（缺stub、替换目标不唯一、旧压缩fixture返回不合法待执行计划）。修正fixture后受影响61项全过；新增瞬时事件修复46项全过。各组重叠，不相加。更早233项通过的运行保留；不为追求全绿重跑无关全集。真实生产StateGraph覆盖明确handoff成功与有限失败、只读动作/笔记拦截、精确attempt收尾、回调归属等。

真实CLI协议smoke使用虚构value=7、label=red和声明label=blue，无手机或SDK Session：一次调用，1 start/1 end/1 llm trace；精确canonical覆盖，父trace及task/attempt归属正确；如实返回1 passed/1 failed。这只证明真实结构化协议，不计真实设备成功。测试记录保留先前失败及修正，不声称回归代替实机。

## 架构与维护建议

多进程仍是一个父Main、每Role一个常驻worker，未改成协程。原生全局状态隔离于worker；每任务临时状态和trace仍需TaskScope，进程隔离不能代替归属控制。长期Session保留连接、业务notes和历史，任务计划/当前状态独立归档；run2的无清空续算及取消复用给出有限实证。

worker异常/同步阻塞的结构性影响范围较小，但Main仍承担心跳、重连、租约、恢复合同和依赖处理。没有本轮故障注入证据的崩溃/强杀/排队取消不算已验证。当前应以多进程分支继续交付真实模型Main，并保留独立合同门槛；本次尚不支持“完整业务验收全部通过”或生产稳定性结论，也不宣称进程机制普遍优于协程。

协程已有持续Role SDK但无智能Main，这是实现完成度差异。按用户最终指令不再复测、更新或推送协程分支；先前本地公共修复保留。上游兼容成本包括orchestration协议与原生Planner/Operator/Checker/Outputter、history、DataEngine边界，需随上游生命周期变更重新验证，不能只看Git无冲突。公共Codex路由及Windows静态MIME修复保持；没有整分支合并或覆盖Main。相对本地共同基线 351ca8422f7b5b54e80a9c1ce03a222e02415b6b，最终源码的 artemis/ 与 third_party/ 共改变52文件（26既有、26新增，4792+/ 207-）。含公共适配和不同产品功能，不是同功能净成本排名，也不声称基线为远端最新上游。

## 可复核公开证据

- [最终验收配置](evidence/session-repair-20261021/mp-acceptance/config.json)、[源码与命令](evidence/session-repair-20261021/mp-acceptance/source.json)、[Main提案/任务/终态时间线](evidence/session-repair-20261021/mp-acceptance/events.json)、[截图和任务文件索引](evidence/session-repair-20261021/mp-acceptance/artifact-index.json)。
- [run2精确取消与归属审计](evidence/session-repair-20261021/mp-run2/execution-audit.json)、[进程锁及数据库收尾](evidence/session-repair-20261021/mp-run2/cleanup-probe.json)、[对话与记忆](evidence/session-repair-20261021/mp-run2/session-persistence-audit.json)、[补充归属/压缩](evidence/session-repair-20261021/mp-run2/session-persistence-audit-details.json)、[Checker实际来源](evidence/session-repair-20261021/mp-run2/checker-evidence-source-audit-final.json)、[900秒恢复故障定位](evidence/session-repair-20261021/mp-run2/recovery-attempt-9-timing-audit.json)。
- [最终实机汇总](evidence/session-repair-20261021/verification/acceptance-summary.json)、[依赖修复回归](evidence/session-repair-20261021/verification/dependency-regression.json)、[最终关闭与锁检查](evidence/session-repair-20261021/mp-acceptance/cleanup-probe.json)。
- [run1证据索引](evidence/session-repair-20261021/mp-run1/artifact-index.json)、[mock回归记录](evidence/session-repair-20261021/verification/regression-summary.json)、[真实CLI协议结果](evidence/session-repair-20261021/verification/protocol-summary.json)。

每个运行目录的sha256.json可核对上传文件；仅公开计算器截图、白名单业务note、脱敏日志及有界trace元数据，不上传大型SQLite、完整模型推理/对话、XML、视频或登录信息。源scratch路径只是本地保全位置，公开复核使用上述仓库链接。
