# 主子 Session 简化协议草案

版本：2。状态：讨论稿，尚未替换当前代码中的版本 1。

Main 管理任务、依赖和输入来源；每个 Role 对应一个常驻子进程，子进程持有原生 Artemis Session，连续执行 Main 派发的任务。

本协议约定任务如何派发、结果如何返回，以及取消和故障时如何处理。业务字段由任务定义，例如会议编号、用户 ID、文件路径。

## 1 消息格式

连接沿用本机 TCP 和 UTF-8 JSONL，每条消息一行，最大 1 MiB。大文件保存到本地，通过路径引用传递。

所有消息共用一个 Envelope：

```json
{
  "protocol_version": 2,
  "kind": "execute",
  "message_id": "66666666-6666-4666-8666-666666666666",
  "in_reply_to": null,
  "sequence": 2,
  "workflow_id": "11111111-1111-4111-8111-111111111111",
  "role_id": "host",
  "child_session_id": "22222222-2222-4222-8222-222222222222",
  "worker_epoch": "33333333-3333-4333-8333-333333333333",
  "task_id": "44444444-4444-4444-8444-444444444444",
  "attempt_id": "55555555-5555-4555-8555-555555555555",
  "payload": {}
}
```

| 字段 | 用途 |
| --- | --- |
| protocol_version | 协议版本。版本 1 和 2 不混用 |
| kind | 消息类型 |
| message_id | 这一条消息的 UUID，重发原消息时保持不变 |
| in_reply_to | 回复的原消息 ID，主动消息为 null |
| sequence | 发送方的递增消息编号，用于排查顺序，不作为去重依据 |
| workflow_id | 本次工作流的 UUID |
| role_id | 任务所属 Role |
| child_session_id | 常驻子 Session 的 UUID |
| worker_epoch | 子进程运行实例的 UUID，进程重建后更换 |
| task_id | 逻辑任务的 UUID |
| attempt_id | 本次执行的 UUID，明确重试时更换 |
| payload | 对应消息的内容 |

会话级消息的 task_id、attempt_id 同时为 null。一个子 Session 同时最多执行一个 attempt；后续任务复用该 Session 的上下文。

首次连接沿用启动认证与 hello 握手；ready 表示初始化成功。新进程使用新身份，不冒用旧进程继续未完成的 attempt。

## 2 消息类型

| 方向 | kind | 含义 |
| --- | --- | --- |
| 子 → 主 | hello / ready | 连接认证 / 已准备接任务 |
| 主 → 子 | execute | 派发一个任务 attempt |
| 子 → 主 | accepted / rejected | 已持久化接收 / 尚未执行并拒绝 |
| 子 → 主 | started | 开始执行，仅作为进度通知 |
| 子 → 主 | result | 已收尾并持久化的任务终态 |
| 主 → 子 | ack | Main 已持久化收到的 result |
| 主 → 子 | cancel_task | 取消指定 task_id 和 attempt_id |
| 子 → 主 | cancel_received | 取消请求是否被接受 |
| 子 → 主 | heartbeat / session_status | 进程活性与会话当前状态 |
| 主 → 子 | close_session | 停止接任务，收尾并关闭 |
| 子 → 主 | closed | 关闭结果 |
| 主 → 子 | inspect_session / observe_session | 查询上下文 / 观察设备 |
| 子 → 主 | inspection / observation | 查询或观察的结果 |
| 双向 | error | 控制或协议错误 |

正常任务流程：execute → accepted → started → result → ack。

accepted、started、ack 均不代表任务业务验收通过。Main 校验 result 后决定是否解锁下游任务；继续向该 Role 派任务还需要它处于 READY。

## 3 任务内容

execute.payload 使用下列字段：

| 字段 | 含义 |
| --- | --- |
| goal | 这次任务要做什么 |
| inputs | 执行需要的具体数据，默认 {} |
| output_requirements | 需要返回的字段及自然语言说明，默认 {} |
| completion | 完成条件，至少一项 required 为 true |
| constraints | 执行约束，默认 [] |
| execution_timeout_seconds | 原生 Artemis 执行预算 |
| verification_timeout_seconds | 独立验收预算 |
| settlement_timeout_seconds | 每一次收尾的预算 |
| max_steps | 最大执行步骤数，默认 30 |

三个超时均使用正数秒。未指定时，草案默认执行 180 秒、验收 60 秒、每次收尾 30 秒；工作流还可设置总预算。

output_requirements 是“字段名 → 说明”的简单映射。例如：

```json
{
  "output_requirements": {
    "meeting_id": "可供其他参会者加入会议的编号，按字符串返回并保留前导零"
  }
}
```

声明的字段在成功结果中必须返回，未声明的字段不返回。没有需要交给后续任务的数据时写 {}。这里不使用复杂 JSON Schema，只检查 JSON 对象和声明字段是否齐全；类型等要求写在说明中，业务含义通过验收核对。

completion 每项固定为 id、description、required。id 在该任务内唯一，用于关联结果中的 checks。Main 决定完成条件，子 Session 的独立 Checker 根据实际执行证据核验。

inputs 只携带值。上游结果引用、数据来源、有效期和失效关系由 Main 内部保存并在派发前解析，不发送 input_sources。

## 4 结果内容

result.payload 使用下列字段：

| 字段 | 含义 |
| --- | --- |
| status | succeeded / failed / blocked / cancelled / unknown |
| outputs | 按 output_requirements 返回的数据，无输出时为 {} |
| checks | 对 completion 的逐项验收结论 |
| observed_at | 生成终态结果时的 UTC 时间，使用带时区的 ISO 8601 |
| step_range | 本 attempt 的原生步骤范围；没有步骤时可省略 |
| artifact_refs | 额外交付产物的路径引用，默认 [] |
| error | 未完成的原因，成功时为 null |
| observations | 可选的辅助观察，普通结果省略 |

succeeded 表示全部 required 检查通过且要求的输出齐全。failed 表示执行或验收失败；blocked 表示缺少信息或条件而无法继续；cancelled 表示取消并完成收尾；unknown 表示无法确认任务最终状态，例如 Main 发现进程丢失。

未成功的结果允许只包含已经取得的部分输出。Main 只使用成功且仍有效的结果解锁下游任务。

checks 的字段固定：

| 字段 | 含义 |
| --- | --- |
| id | 对应 completion.id |
| verdict | passed / failed / inconclusive / unchecked |
| evidence | Checker 根据截图、步骤或探测结果给出的具体判断依据 |
| evidence_refs | 程序保存的证据文件路径数组 |

passed 表示条件成立；failed 表示条件不成立；inconclusive 表示证据不足；unchecked 表示未完成检查。报告成功时，required 检查必须有可读取的证据引用。

evidence 是文字解释，evidence_refs 指向实际文件。验收报告保存为 contract_report.json，有验收截图时保存 contract-screen.jpg。多个检查项可以引用同一份报告，但报告需要说明各自依据。

artifact_refs 用于整个任务的额外交付物，例如导出的文件；不重复罗列 checks 已引用的证据。manifest 在本地保存完整文件索引、大小和哈希。所有引用均相对于对应子 Session 的产物根目录，Main 检查路径、归属和文件内容。

observations 可按需提供 key、value 两个字段，例如 {"key": "waiting_count", "value": 2}。它只是辅助信息，不直接成为下游任务输入；需要交接的数据应声明在 output_requirements 并返回到 outputs。

error 仅保留 code 和 description。需要用户补充信息时也使用 error，不再单独设置 blocker。

## 5 登录示例

以下例子展示消息的 payload。外层 Envelope 按消息类型和实际身份填写。登录、创建会议是不同 task；同一个 host 子 Session 连续执行，保留应用登录状态和上下文。

Main → host，execute.payload：

```json
{
  "goal": "登录指定账号，并停留在登录后的首页。",
  "inputs": {
    "account": "测试账号A"
  },
  "output_requirements": {},
  "completion": [
    {
      "id": "login_success",
      "description": "应用已进入登录后的页面，当前不存在待完成的认证步骤。",
      "required": true
    },
    {
      "id": "account_match",
      "description": "当前实际登录的账号与指定账号一致。",
      "required": true
    }
  ],
  "constraints": ["不要切换到其他账号"],
  "execution_timeout_seconds": 180,
  "verification_timeout_seconds": 60,
  "settlement_timeout_seconds": 30,
  "max_steps": 30
}
```

host → Main，result.payload。证据文字用于说明格式，实际内容必须来自观察；路径中的 {task_id}、{attempt_id} 需要替换成真实 UUID。

```json
{
  "status": "succeeded",
  "outputs": {},
  "checks": [
    {
      "id": "login_success",
      "verdict": "passed",
      "evidence": "已进入登录后的首页，未出现待完成的认证步骤。",
      "evidence_refs": [
        "tasks/{task_id}/{attempt_id}/contract_report.json",
        "tasks/{task_id}/{attempt_id}/contract-screen.jpg"
      ]
    },
    {
      "id": "account_match",
      "verdict": "passed",
      "evidence": "登录过程中取得的账号信息显示当前账号为测试账号A，验收报告记录了观察来源。",
      "evidence_refs": [
        "tasks/{task_id}/{attempt_id}/contract_report.json"
      ]
    }
  ],
  "observed_at": "2026-10-21T00:00:00Z",
  "step_range": {"first": 1, "last": 8},
  "artifact_refs": [],
  "error": null
}
```

登录成功通过 checks 表达，不再重复返回 logged_in: true。如果后续任务确实需要用户 ID，只需将 output_requirements 改为 {"user_id": "当前实际登录账号的用户ID"}，并在 outputs 中返回 user_id。登录成功和账号正确这两项检查仍然保留。

## 6 创建会议和交接编号示例

Main → host，execute.payload。此任务依赖登录成功，依赖关系由 Main 管理：

```json
{
  "goal": "创建会议，进入主持人会中页面并取得会议编号。",
  "inputs": {
    "meeting_name": "方案评审"
  },
  "output_requirements": {
    "meeting_id": "可供其他参会者加入会议的编号，按字符串返回并保留前导零"
  },
  "completion": [
    {
      "id": "meeting_active",
      "description": "主持人已进入会中页面，且已核验会议编号。",
      "required": true
    }
  ],
  "constraints": ["不结束会议", "不要批准未指定的参会者"],
  "execution_timeout_seconds": 180,
  "verification_timeout_seconds": 60,
  "settlement_timeout_seconds": 30,
  "max_steps": 30
}
```

host → Main，result.payload：

```json
{
  "status": "succeeded",
  "outputs": {"meeting_id": "001234567"},
  "checks": [
    {
      "id": "meeting_active",
      "verdict": "passed",
      "evidence": "主持人会中页面显示会议编号001234567，报告记录了该观察。",
      "evidence_refs": [
        "tasks/{task_id}/{attempt_id}/contract_report.json",
        "tasks/{task_id}/{attempt_id}/contract-screen.jpg"
      ]
    }
  ],
  "observed_at": "2026-10-21T00:05:00Z",
  "step_range": {"first": 9, "last": 15},
  "artifact_refs": [],
  "error": null
}
```

Main 验收成功后，把具体编号放入 guest 任务的 inputs。下面展示需要主持人审批时的任务边界：guest 提交申请后先返回，不悬挂等待 host 审批。

Main → guest，execute.payload：

```json
{
  "goal": "用指定参会者名称申请加入目标会议，提交申请后等待主持人审批。",
  "inputs": {
    "meeting_id": "001234567",
    "participant_name": "B"
  },
  "output_requirements": {},
  "completion": [
    {
      "id": "join_requested",
      "description": "已向编号001234567的目标会议提交参会者B的入会申请，当前显示等待主持人审批。",
      "required": true
    }
  ],
  "constraints": ["不要重复提交申请"],
  "execution_timeout_seconds": 120,
  "verification_timeout_seconds": 60,
  "settlement_timeout_seconds": 30,
  "max_steps": 20
}
```

guest 按同一 result 格式返回 join_requested 的检查结果。Main 再给 host 派“批准B入会”，然后给 guest 派“核验已经入会”。不需要审批的会议可以直接把完成条件设为“B已进入目标会议”。

## 7 取消和故障

三个预算分别约束：执行 → 收尾 → 独立验收 → 收尾。每次收尾单独计时。执行超时触发停止请求，必须等待实际操作、后台任务和写入收尾，不能立即认为设备已空闲。

Main 取消任务时，cancel_task 携带精确的 task_id、attempt_id，payload 只需要 reason：

```json
{
  "reason": "用户取消本次操作"
}
```

cancel_received.payload 示例：

```json
{
  "accepted": true
}
```

accepted: true 仅代表已接受取消。最终仍需收到 cancelled 的 result，或确认进程退出后将任务记为 unknown。旧 attempt 的取消不能影响新 attempt；结果已进入提交阶段时可以拒绝取消并说明原因。

缺少验证码时，子 Session 完成收尾，返回受阻结果：

```json
{
  "status": "blocked",
  "outputs": {},
  "checks": [
    {
      "id": "login_success",
      "verdict": "unchecked",
      "evidence": "当前停留在短信验证码页面，尚未完成登录。",
      "evidence_refs": []
    },
    {
      "id": "account_match",
      "verdict": "unchecked",
      "evidence": "认证尚未完成，无法核验最终登录账号。",
      "evidence_refs": []
    }
  ],
  "observed_at": "2026-10-21T00:00:00Z",
  "artifact_refs": [],
  "error": {
    "code": "missing_input",
    "description": "需要用户提供当前账号收到的短信验证码。"
  }
}
```

blocked 任务已经结束；Main 取得所需信息后另派任务，子 Session 可保留当前页面和上下文。

取消、超时或进程丢失后，不自动重新执行业务任务。Main 先观察或派核验任务，确认现场，再决定下一步。进程重建时更换 Session 身份，并使依赖旧 Session 的状态失效；历史结果保留，不把旧成功状态直接用于新进程。

## 8 会话状态和结果确认

子 Session 状态包括 STARTING、READY、EXECUTING、VERIFYING、SETTLING、COMMITTING、FAULTED、CLOSING、CLOSED。

任务是否成功看 result，能否继续接任务看会话状态。heartbeat、session_status 的 payload 使用同一结构：

```json
{
  "state": "READY",
  "active_task_id": null,
  "active_attempt_id": null
}
```

READY 表示当前没有 active attempt，原生操作和任务写入已完成收尾。收尾失败进入 FAULTED。Main 对该 Role 停止派发，确认旧进程退出后才能启动替换进程。

子进程先把 result 和证据保存到本地，再发送 result。Main 保存收到的结果后发送 ack，in_reply_to 指向 result.message_id，payload 为 {}。ACK 只确认收到并保存该结果，不代表业务验收通过，也不表示传输了文件。

ACK 丢失时补发原 result；Main 按身份和消息 ID 去重。相同 attempt 的请求内容不可改变，冲突则拒绝。重发只覆盖仍在等待结果的原请求；Main 已超时或放弃的 attempt 不重发业务指令。

同机文件引用沿用 manifest 的路径和哈希验证；ACK 不再重复携带 result_hash。原生设备动作不具备 exactly-once 保证，不能因为消息重发就重做动作。

close_session 请求停止准入并取消、收尾当前任务；closed 报告清理结果。Main 需要确认进程退出，无法收尾时强制回收进程树。设备接管由 Main 执行，不向失联进程发送一个消息就视为接管成功。

inspect_session 和 observe_session 沿用现有只读诊断能力。需要导航、点击或其他业务操作时，仍派 execute。诊断快照用于辅助判断，不直接替代某个任务的成功验收。

## 9 与当前实现的对应

| 项目 | 简化协议 |
| --- | --- |
| outputs_schema | 替换为 output_requirements，自然语言描述所需字段 |
| input_sources | 从消息删除，Main 内部保存来源与失效关系 |
| effect_policy | 删除，统一采用先核验再决定后续任务的恢复规则 |
| blocker | 删除，受阻原因写入 error |
| suggested_next_actions | 删除，后续任务由 Main 决定 |
| side_effect_state | 删除，取消、超时、失联不据此自动重做 |
| session_reusable | 删除，由会话状态判断是否可继续派任务 |
| observations | 可选辅助观察，默认不发送 |
| artifact_refs | 保留额外交付物，不重复列出检查证据 |
| ack | 保留结果接收确认，去掉重复的结果 hash |
| protocol_version | 使用 2，避免新旧字段被静默误解 |

原有实现说明见 [运行文档](persistent-role-sessions-usage.md)，详细背景见 [设计文档](persistent-role-sessions-design.md)。
