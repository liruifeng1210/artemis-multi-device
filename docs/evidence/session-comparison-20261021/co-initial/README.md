# 协程首轮：预算错误样本，不是修复后复测

实际源码 `ac57e30`，graph recursion_limit=30，CLI=gpt-6-luna/xhigh。没有加载后续 Diagnoser、Object Detector、关闭归属或模型 PID 日志修复。[配置](config.json)、[源码记录](source.json)、[任务状态](results.json)、[时间线](timeline.json)。

| 场景 | SDK 终态 | 屏幕/业务结论 | Checker |
|---|---|---|---|
| A 初算 | failed / GraphRecursionError | `125+`，`=125`；灰色旧历史200不计本轮成功 | 未完成 |
| B 初算 | cancelled / 900秒超时保护 | 空白，未得到8 | 未完成 |
| A 连续×3 | failed / GraphRecursionError | 误入计算器设置，未得到600 | 未完成 |
| B 连续+17 | failed / GraphRecursionError | 空白，未得到25 | 未完成 |
| A 独立取消 | cancelled，reusable=true | Planner/LLM等待中，尚无设备 action；正式 task/attempt cancel 耗时0.03309秒 | 不适用 |
| B 同时×4 | completed | 空白，未得到100；取消A未取消B | 0 passed / 1 inconclusive |
| A 取消后+10 | failed / GraphRecursionError | 同Session可接受任务并发生新动作，但屏幕为 `1+1+`，`=2`，未完成+10 | 未完成 |
| B 历史报告 | completed | notes明确48÷6未完成、旧25不是当前值，当前为空白；无法证明记住本轮8和100 | 没有声明检查项 |

8项中 SDK completed=2，failed=4，cancelled=2。业务未跑通，不能将两个 completed 当作计算验收通过。每轮原始截图见相应 `01-*` 至 `04-*` 目录；截图只包含计算器及系统状态栏图标，无通知正文。

[执行审计](execution-audit.json)显示同Role的context/driver/ledger身份保持，任务终态后未发现归属于旧任务的迟到设备action记录。该结论受trace覆盖范围限制；排队/设备操作中取消均未验证。[归属审计](ownership-audit.json)的8项错误均为关闭日志缺device_id；没有发现错误Session/Role/device或跨task step引用。[数据库摘要](database-summary.json)保留13条running台账；历史chunk为5个pending v1及5个ready v2，相同Session/step范围的当前5个最高版本均ready，历史pending不代表仍有摘要任务运行。

[关闭后检查](cleanup-probe.json)：采样身份无存活后代、双设备锁可取得并释放、ADB forward为空、SQLite quick_check=ok、10秒内表记录数稳定。这里证明有界观察窗口中的资源收尾，不把pending/running元数据解释成活进程，也不抹去台账未终结问题。[资源统计](process-metrics.json)与[采样](process-samples.jsonl)仅作本轮测量，不能与不同预算的样本直接排序性能。
