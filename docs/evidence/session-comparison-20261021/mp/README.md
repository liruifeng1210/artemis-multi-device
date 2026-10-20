# 多进程真机运行：真实 Main，最终 blocked

源码为 `00708fa`，Main 输入的 [WorkflowSpec](request.json) 中 `tasks=[]`。生产 MainPlanner 实际调用 Luna/xhigh，生成八项任务图并驱动两台设备。驱动仅观察、采集及调用一次正式 `Main.cancel(task_id)`，没有替 Main 创建任务或选择手机动作。

主证据：[Main提案与派发时间线](observer-events.json)、[生产journal](events.json)、[最终结果](outcome.json)、[配置](config.json)、[源码SHA](source.json)、[离线审计](execution-audit.json)、[检查台账与最终答复](check-evidence.json)、[公开任务文件索引](artifact-index.json)。时间为UTC。

| 场景 | Main TaskResult | 屏幕/业务 | 原生Graph Checker | Main合同检查 |
|---|---|---|---|---|
| A 初算 | cancelled，900秒保护在Outputter触发 | [200](boundaries/d0103c14-0dcb-4da4-8d25-1640e49ea846/screen.png)，业务屏幕通过 | completed；2 passed | 2 unchecked |
| B 初算 | blocked | [8](boundaries/47f9ce37-8a02-4f3f-8f41-9c7b3ab1d249/screen.png)，业务屏幕通过 | completed；2 passed | 2 inconclusive，文本身份不匹配 |
| A 连续×3 | failed | [仍为200](boundaries/625cf4bc-0b38-466c-a6ea-0a3c85f78baf/screen.png)，未执行×3，未得到600 | completed；1 passed / 1 inconclusive | 1 failed / 1 passed |
| B 连续+17 | blocked | [仍为8](boundaries/84651aa3-19cf-480f-a66a-13d1d9e63bf1/screen.png)，未执行+17，未得到25 | completed；2 passed / 2 inconclusive | 2 inconclusive |
| A 独立取消 | cancelled，reusable=true | Planner/LLM等待阶段，0个设备action；B同时运行 | 无完整run_outcome | 2 unchecked |
| B 同时×4 | blocked | [8×4=32](boundaries/e6b73f62-f4cf-49d0-b167-6de77df28f77/screen.png)；A取消后继续发生3次点击。调度/动作独立性通过，完整业务100失败 | completed；3 passed（检查的是生成的条件目标） | 2 inconclusive |
| A 取消后+10 | cancelled，900秒保护 | [仍为200](boundaries/47b19b44-c9f2-4edf-930c-b4ac1ac5f9f5/screen.png)，同Session接受任务，但+10未执行 | 无完整run_outcome | 2 unchecked |
| B 历史报告 | blocked | 输出首次8、当前32，与本Session实际历史一致；最终前台出现轨迹外变化，屏幕连续性不确定 | completed；1 passed / 1 inconclusive | 1 inconclusive |

Main终态：4 blocked、3 cancelled、1 failed，0 succeeded。原生Graph有6条completed记录，合计11 passed / 4 inconclusive；这不是6项端到端成功。Main合同共1 passed / 1 failed / 7 inconclusive / 6 unchecked，检查集合不同，不与原生统计合并。原生/合同文件及完整Task/attempt UUID可由公开索引复核；目录UUID缩为8位只是解决Windows长路径，索引保留原始全路径。

原始八项任务都使用同一A Session `079e2a2f-a1c9-4e70-8974-19c2c4fa86f8` 和B Session `570caa92-a5e2-460a-893b-8d547145ac42`；worker启动器PID为27344/22940，实际Role解释器PID为35468/39616。[八次新任务状态](task-start-states.json)中的临时字段已重置；持久Role历史与临时执行状态分别核查。

取消请求到确认0.007191秒，到TaskResult约0.081508秒。B在A取消后实际点击×、4、=，得到32。未发现旧任务终态后迟到的设备action记录；该结论受trace覆盖限制。排队/设备操作中的取消、进程崩溃注入未验证，不能泛化为所有取消阶段均可靠。

Main消费全部结果后又产生三次模型响应：两次恢复提案经生产Scheduler规则的离线重放均因“Recovery must preserve the original role and acceptance contract”被拒绝，最后一次只请求诊断。生产重规划循环预算耗尽且计划revision未前进，Main最终blocked。恢复任务没有被驱动代派发。重放只解释验证结果，不执行设备、不替代模型、不计作真机业务通过。

原生trace没有直接Role/device/task执行元数据：2158条缺device字段，26条检查记录缺直接task字段；Session→Role/device/运行PID可由worker绑定和DB sessions间接关联，step有task/attempt。引用审计未发现跨Session/step/parent错误，但不能宣称直接字段全覆盖。最终7条running trace仍保留，这不是活进程证明。history_chunks采用追加版本：2个pending v1由相同Session/step范围的2个ready v2取代，当前2个范围均ready，不能把历史pending误报为未完成摘要。[数据库摘要](database-summary.json)同时保留全版本和当前范围统计。

边界inspect多数已经看到下一任务，source.active_task_id与被归档任务不同；这属于驱动观测时序，不是据此证明串线。公开result文件保留inspection_source和consistency。最后A/B均有未截断的ledger尾部，但Operator ledger不等于Outputter/Checker全部对话；终答、检查流和任务结果另行保留。

最后B报告时前台转到“关于本机”，没有对应测试动作，来源未确定；[两张原图因非业务设备信息未公开](evidence-omissions.json)，原始SHA保留。本阶段只从历史评估8/32，不以该屏幕证明计算器前台保持。

[正常关闭](closed.json)中两个worker graceful=true、exit=0、lease_released=true、errors=[]；[关闭后独立核查](cleanup-probe.json)无采样后代存活、双锁可取得释放、ADB forward为空、SQLite quick_check=ok且10秒记录数稳定。它不抹去未终结台账。[进程/RSS统计](process-metrics.json)与[原始采样](process-samples.jsonl)可复算；这里只测量一次工作流，不作吞吐或稳定性排名。
