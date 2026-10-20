# 协程修正预算真机运行：部分业务成功，未交付智能Main

实际执行源码`a2afa8d1a28d848182d65b7caf23b4fd08266555`；模型Codex/gpt-6-luna/xhigh，CLI300秒、原生Checker600秒、每任务900秒、图递归100。固定测试驱动调用正式Role SDK，不是智能Main。本轮是新A/B Session，不能接续首次错误预算运行的Session。

主证据：[配置](config.json)、[实际源码](source.json)、[完整时间线](timeline.json)、[任务结果](results.json)、[检查台账及答复](check-evidence.json)、[归属审计](ownership-audit.json)、[执行/取消审计](execution-audit.json)、[任务文件索引](artifact-index.json)。时间均UTC。

| 场景 | RoleTaskResult.status | 屏幕业务 | Checker/交付 |
|---|---|---|---|
| A 125+75 | failed，GraphRecursionError(100) | [125+，当前125](01-initial-A/screen.png)，未完成；灰色200为旧历史 | 无完整run_outcome |
| B 48÷6 | completed | [8](01-initial-B/screen.png)，通过 | 2 passed |
| A接结果×3 | cancelled，900秒超时 | [仍125+](02-continue-A/screen.png)，未得到600 | 无完整run_outcome |
| B接结果+17 | completed | [8+17=25](02-continue-B/screen.png)，无清空重输，通过 | 1 passed / 1 inconclusive，未全检查通过 |
| A长任务独立取消 | cancelled，reusable=true | [取消后仍125+](03-cancel-A/screen.png)，该任务0个设备动作 | Planner/LLM等待阶段；接口0.041169秒，未覆盖排队/设备操作阶段 |
| B同时接结果×4 | failed | [25×4=100](03-independent-B/screen.png)，屏幕业务通过；A取消后4次实际点击 | 1 passed / 1 failed。Checker声称最终仍25，与最终截图冲突；SDK据失败检查返回failed，未改判 |
| A取消后+10 | completed | 同一Session观察125+当前125后，输入10并等号，[135](04-reuse-A/screen.png)，通过 | 3 passed / 1 inconclusive；不证明此前×3成功 |
| B历史报告 | completed，output=None | [仍100](04-reuse-B/screen.png)，该任务无设备动作 | **业务失败**：只在calculation_results笔记写“首次结果无法确认，当前100”，没有交付8/100历史报告；run_outcome=None，未完整检查 |

SDK状态4 completed / 2 failed / 2 cancelled。四个任务有完整原生Graph completed outcome，原生最终检查合计7 passed / 1 failed / 2 inconclusive / 0 unchecked；缺失完整outcome的任务不是“零检查也通过”。四项算术屏幕目标通过：B初算、B连续、B×4、A取消后+10。A初算/×3及B历史交付未通过。B×4中checker_success布尔仍true，但存在failed assert，不能只看该布尔值。

A Session `14127587-b5ff-45ad-9943-8879e13864cc`、B Session `633269ea-9548-43bc-bfed-ad37525de38e`，实际共同解释器PID16572。各Role的context/driver/ledger对象跨四任务一致，Task/attempt和工作区独立，所有结果reusable=true、cleanup_errors=[]。A取消后再次派发并真正完成+10，证明本轮同Session可复用；它不是新建Session替代。

A精确取消发生于Planner/LLM等待，该任务0个设备action；B之后点击乘号两次、4、等号，实际得到100。重复乘号尝试如实保留；每个Task只有一个attempt，不等于原生agent没有重复动作。未发现旧Task终态后发起或结束的迟到设备action，动作duration均有记录；此结论受trace覆盖限制，没有做崩溃注入。

动态activation观察器没有预建输出目录，八个activation文件缺失；该SDK调用路径也没有可解析的graph-start状态日志（0条）。这是采集缺口，不宣称已直接观察所有临时字段清零或TaskContext对象身份。代码使用State.initial/新TaskControl，运行证据验证了新Task/attempt归属及新目标，但不足以替代缺失的起始快照。最终提交修正mkdir和observer_error记录，仅做编译检查，未冒充该补丁已真机重测。

最终A/B ledger_has_staged_turn均false，turn_count分别26/19，retained_runs=0、模板context的background计数0。B最后任务没有新增Operator回合，且没有有效历史输出，不能把ledger持续存在说成历史交付通过。数据库44steps中40条summary ready，4条没有动作及summary字段；后台jobs表为空。history_chunks全版本14 pending v1+14 ready v2；按生产Storage相同Session/step范围的最高版本规则，当前14个范围全部ready。摘要写完不保证下一任务Planner正确取得所需历史事实。

[数据库摘要](database-summary.json)：2853 traces，8条仍为running台账；直接设备字段缺口0，检查记录task字段缺口0，Session/Role/device/task/attempt及step/parent引用审计错误0，检查文件归属错误0。结构审计没有发现串线，不能穷尽证明所有模型语义或所有未来副作用都隔离。

[关闭快照](closed.json)两Role closed、锁无owner、children=[]；[关闭后检查](cleanup-probe.json)无采样PID+创建时间身份存活、两锁可取并释放、ADB forward无有效条目（仅空行）、SQLite quick_check=ok、10秒行数稳定。running trace保留为可观察性缺口，不能据此推定活进程泄漏，也不抹成成功。[过程采样](process-samples.jsonl)、[统计](process-metrics.json)含CLI及conhost开销，不作吞吐/稳定性推广。
