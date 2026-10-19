# Pro 多设备运行

现有 Home / Workspace 和 `artemis run --profile pro` 入口先生成一次 RolePlan，再决定设备路径。只有一个角色时沿用 Artemis 原生单设备 Pro 图；多个角色时分配互不相同的设备并进入多设备扩展图。Flash 维持原有路径。RolePlan 的角色是固定参与者身份，回复消息的接收者仍是同一角色。

```powershell
artemis run "A 发送消息，B 回复，A 确认回复" --profile pro
```

可从 SDK 明确启动多设备用例并绑定设备：

```python
from artemis.sdk import MultiDeviceAgent, MultiDeviceRequest

request = MultiDeviceRequest(
    case="A 发送消息，B 回复，A 确认回复",
    role_bindings={
        "a": {"serial": "device-a", "account_ref": "sender"},
        "b": {"serial": "device-b", "account_ref": "receiver"},
    },
)
runner = MultiDeviceAgent()
result = await runner.run_case(request)
```

`run_case` 生成一次 RolePlan。已经规划过的调用方可以传入 `role_plan=plan`；SDK 会先验证角色绑定和 feature 约束，再接触设备。运行时可调用 `runner.stop_current_task()`；取消会等待动作会话和设备资源排空后释放租约。

RolePlan 先于设备分配。Planner 在统一 `notes/task_plan.md` 中按原有 Markdown 语法生成带角色的顶层 milestone：

```markdown
- [ ] [role:a] 发送本次运行的消息
  - verify: 消息已发出
- [ ] [role:b] 收到消息并回复
  - verify: 回复已发出
- [ ] [role:a] 确认对应回复
  - assert: 回复内容正确
```

调度器始终选书面顺序中的第一个未完成 milestone，因此 A → B → A 和 A → A 都会进入新一轮 Perception。旧轮的动作被中止；同一回复中已发出的工具调用会收到匹配的 ToolMessage。每个角色有独立设备上下文、Operator 对话、坐标与工具会话，共享计划和普通笔记。标准 UI 动作仍交给 Validator。

任务计划写入有统一校验和并发内容冲突检测。Checker 的 checkpoint 绑定原 milestone 的角色和证据 step；换手前会结算检查。不同角色下的同文案检查依 `check_key` 分开记录和计数，含糊的报告不能替任一角色通过。verify 失败在原角色修复；最终检查读取各设备带角色标签的观察。原有 checker 开关仍生效。

如用例需要目标 App 或账号，先在对应设备准备好。`app_profile` 可指定包名、环境说明及可选 APK；`role_bindings` 可携带 `account_ref` 和已准备的 `prepared_features`。当前不自动登录、不修改业务 feature，也不实现 KB、CaseAdapter 账号流程或 PlanReviewer 具体规则。

执行资料写入 trace 目录的 `<run_id>/`，包括 `multi_device.json`、`case_progress.json`、`case_result.json`、标准步骤和 Checker 台账。仅支持同一 Artemis 主机可直接寻址的 Android 设备；Cloud 单设备网关不支持多设备执行。

实现边界和保留的核心 hook 见 [低侵入实现说明](minimal-role-runtime.md)。先前的 Case HTTP、MCP、独立页面及作业框架设计保留在其他设计文档中供历史参考，当前运行入口不提供这些接口。
