# 双设备比较证据索引

阅读[完整比较报告](../../session-comparison-real-devices-20261021.md)。原始SQLite和全量日志保留本地，不在Git中。时间线为UTC，正文时间说明采用北京时间时明确标注。

| 证据 | 范围 |
|---|---|
| [多进程真实Main](mp/README.md) | 00708fa；真实模型生成任务、消费结果、恢复提案；完整失败与收尾 |
| [协程修正预算运行](co-corrected/README.md) | a2afa8d；固定测试驱动；无智能Main |
| [协程首次新运行](co-initial/README.md) | ac57e30；30图步错误预算，不能拿来判定架构胜负 |
| [历史协程结果](old-co/results.json) | 用户给定的旧真实运行，预算和源码快照不完整 |
| [历史最终审计](common/old-final-audit.json) | 对最终持久化记录重新检查，原旧审计只覆盖部分记录 |
| [环境](common/environment.json)、[选定回归](common/regressions.json) | 精确版本、回归结果及首次失败 |
| [原生改动范围](common/change-scope-final.json) | 共同祖先及分支起点差异定义 |

每次运行的`source.json`是实际执行提交；最终报告提交可能包含后补的只读审计/导出脚本或观察器修正，不意味着重新执行真机任务。`config.json`保存实际生效配置，`model-process-log.json`仅保留安全的CLI调用ID/PID/模型/effort/退出信息。

`artifact-index.json`将公开路径映射回原始路径。为Windows路径限制，任务文件中的UUID目录缩为8位，索引保留完整UUID；采集代码会拒绝前缀碰撞。`sha256.json`列出**公开文件**的哈希，脱敏后的JSON不应与原始文件哈希比较。`evidence-omissions.json`是明确未上传的非业务截图原始哈希及原因。

本目录的`.gitattributes`禁用文本换行转换，确保Git对象中的证据字节和SHA-256台账一致；提交前还按Git暂存对象再次校验。

检查台账中的passed/inconclusive保留原样，不因屏幕算对而改写。持久化running/pending字段不等于进程仍活着；收尾另查`closed.json`和`cleanup-probe.json`。没有直接归属字段时只作间接关联，详见各次数据库摘要的覆盖缺口。
