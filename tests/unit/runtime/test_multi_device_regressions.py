"""Regression contracts for role routing, review, and resource cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from artemis.graph.state import State
from artemis.runtime.execution_scope import execution_scope
from artemis.utils.notes import save_note_content
from artemis.utils.plan_grammar import parse_plan


pytest_plugins = ("test_multi_device", "test_multi_device_lifecycle")


@pytest.mark.asyncio
async def test_failed_verify_with_successor_already_active(runtime):
    from artemis.agents.checker.checker import CheckReport, CheckVerdict
    from artemis.graph.checkpoints import CheckpointRun, PendingCheckpoint

    text = "- [x] [role:a] Send\n  - verify: Sent\n- [/] [role:b] Receive"
    save_note_content(runtime.engine.base_dir, "task_plan", text)
    runtime.control_ctx.execution_setup.disable_checker = False
    item = parse_plan(text).top_level[0]
    ci = parse_plan(text).check_items[0]
    task = asyncio.create_task(
        asyncio.sleep(
            0,
            result=CheckReport(
                verdicts=[
                    CheckVerdict(
                        kind="verify",
                        item_text=ci.text,
                        status="failed",
                        evidence="Unsent",
                        check_key=ci.key,
                        role_id="a",
                    )
                ]
            ),
        )
    )
    checkpoint = PendingCheckpoint(item.key, item.text, (ci,), text, 0, "a")
    runtime.control_ctx.checkpoint_tasks[item.key] = CheckpointRun("review#1", task, checkpoint)
    await runtime.begin_turn(State.initial("Send then receive"))
    assert runtime.active_role == "a"
    assert sum(i.is_active for i in parse_plan(runtime.read_plan()).top_level) == 1
    assert runtime.plan_revision > 0


@pytest.mark.asyncio
async def test_identical_other_role_check_cannot_hide_deleted_check(runtime):
    from artemis.graph.graph import build_pro_nodes
    from artemis.tools.tool_wrapper import invoke_tool_with_injection

    runtime.engine.current_step_id = None
    runtime.engine.record_trace = MagicMock(return_value=uuid4())
    setup = runtime.control_ctx.execution_setup
    setup.disable_checker = False
    setup.disable_midway_checks = False
    setup.disable_planner_validation = True
    text = "- [/] [role:a] Login A\n  - verify: Logged in\n- [ ] [role:b] Login B\n  - verify: Logged in"
    save_note_content(runtime.engine.base_dir, "task_plan", text)
    state = State.initial("Both users log in")
    await runtime.begin_turn(state)
    native = build_pro_nodes(runtime.contexts["a"])["operator"].tools
    tool = next(t for t in runtime.guard_tools(native, "a") if t.name == "save_note")
    candidate = text.replace("[/] [role:a]", "[x] [role:a]").replace(
        "  - verify: Logged in\n", "", 1
    )
    with execution_scope(runtime.scope(phase="operator", role_id="a")):
        result = await invoke_tool_with_injection(
            tool,
            {"key": "task_plan", "content": candidate},
            "review-delete",
            state,
            record_trace=False,
        )
    assert result.status == "success"
    checks = parse_plan(runtime.read_plan()).check_items
    assert {ci.role_id for ci in checks} == {"a", "b"}
    assert runtime.control_ctx.pending_checkpoints


@pytest.mark.asyncio
async def test_guidance_retires_only_the_deleted_roles_check(runtime):
    from artemis.graph.checkpoints import compute_test_summary
    from artemis.graph.graph import build_pro_nodes
    from artemis.tools.tool_wrapper import invoke_tool_with_injection

    runtime.engine.current_step_id = None
    runtime.engine.record_trace = MagicMock(return_value=uuid4())
    runtime.control_ctx.execution_setup.disable_planner_validation = True
    text = (
        "- [/] [role:a] Login A\n  - verify: Logged in\n"
        "- [ ] [role:b] Login B\n  - verify: Logged in"
    )
    save_note_content(runtime.engine.base_dir, "task_plan", text)
    original = parse_plan(text).check_items
    runtime.control_ctx.guidance_unprotected_checks.add(("verify", "Logged in"))
    state = State.initial("Both users log in")
    await runtime.begin_turn(state)
    native = build_pro_nodes(runtime.contexts["a"])["operator"].tools
    tool = next(t for t in runtime.guard_tools(native, "a") if t.name == "save_note")
    candidate = text.replace("  - verify: Logged in\n", "", 1)
    with execution_scope(runtime.scope(phase="operator", role_id="a")):
        result = await invoke_tool_with_injection(
            tool,
            {"key": "task_plan", "content": candidate},
            "retire-a-only",
            state,
            record_trace=False,
        )
    assert result.status == "success"
    assert runtime.control_ctx.guidance_retired_check_keys == {original[0].key}
    assert [ci.key for ci in parse_plan(runtime.read_plan()).check_items] == [original[1].key]
    summary = compute_test_summary(
        list(parse_plan(runtime.read_plan()).check_items),
        [
            {
                "check_key": original[0].key,
                "kind": "verify",
                "item_text": "Logged in",
                "role_id": "a",
                "status": "failed",
            },
            {
                "check_key": original[1].key,
                "kind": "verify",
                "item_text": "Logged in",
                "role_id": "b",
                "status": "passed",
            },
        ],
        retired=runtime.control_ctx.guidance_retired_check_keys,
        policy=runtime.check_policy,
    )
    assert summary.retired == 1 and summary.passed == 1 and summary.failed == 0


def test_case_cannot_bypass_existing_global_concurrency_limit(tmp_path, monkeypatch):
    from artemis.runtime.device_lock import DeviceExecutionLock, DeviceBusyError
    from artemis.runtime.multi_device.leases import DeviceSetLease

    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _: tmp_path)
    other = DeviceExecutionLock("busy-device", session_id="existing", max_concurrency=1)
    case = DeviceSetLease(["device-a", "device-b"], "new-case", max_concurrency=1, timeout=0)
    other.acquire()
    try:
        with pytest.raises(DeviceBusyError, match="concurrency"):
            case.acquire()
    finally:
        case.release()
        other.release()


def test_case_honors_global_concurrency_mode_without_numeric_cap(tmp_path, monkeypatch):
    from artemis.runtime.device_lock import DeviceExecutionLock, DeviceBusyError
    from artemis.runtime.multi_device.leases import DeviceSetLease

    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _: tmp_path)
    other = DeviceExecutionLock("busy-device", session_id="existing", concurrency_mode="global")
    case = DeviceSetLease(
        ["device-a", "device-b"], "new-case", concurrency_mode="global", timeout=0
    )
    other.acquire()
    try:
        with pytest.raises(DeviceBusyError, match="concurrency"):
            case.acquire()
    finally:
        case.release()
        other.release()


async def completed_graph(runtime):
    async def invoke(*_, **__):
        runtime.plan_path.parent.mkdir(parents=True, exist_ok=True)
        runtime.plan_path.write_text(
            "- [x] [role:a] Send\n- [x] [role:b] Receive", encoding="utf-8"
        )
        return {"run_outcome": {"task_status": "completed", "tests": {}}}

    return SimpleNamespace(ainvoke=invoke)


@pytest.mark.asyncio
async def test_cancel_during_recording_start_cleans_partially_started_recorder(
    runner_environment, monkeypatch
):
    env = runner_environment
    entered = asyncio.Event()
    active = set()

    class Controller:
        def __init__(self, ctx):
            self.role = ctx.role_id

        async def take_screenshot(self):
            return "aW1hZ2U="

        async def start_video_recording(self, **_):
            active.add(self.role)
            entered.set()
            await asyncio.Event().wait()

        async def stop_video_recording(self):
            active.discard(self.role)
            return SimpleNamespace(success=False)

    monkeypatch.setattr(env.module, "UnifiedMobileController", Controller)
    env.spec.record_video = True
    runner = env.module.MultiDeviceAgent(config=env.config)
    task = asyncio.create_task(runner.run_case(env.spec))
    await asyncio.wait_for(entered.wait(), 3)
    runner.stop_current_task()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not active, "Recorder remains active after device leases are released"


@pytest.mark.asyncio
async def test_cancel_during_finalization_still_cleans_all_devices(runner_environment, monkeypatch):
    env = runner_environment
    stopping = asyncio.Event()

    class Controller:
        def __init__(self, ctx):
            self.role = ctx.role_id

        async def take_screenshot(self):
            return "aW1hZ2U="

        async def start_video_recording(self, **_):
            return SimpleNamespace(success=True)

        async def stop_video_recording(self):
            stopping.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(env.module, "UnifiedMobileController", Controller)
    monkeypatch.setattr(env.module, "get_multi_device_graph", completed_graph)
    env.spec.record_video = True
    runner = env.module.MultiDeviceAgent(config=env.config)
    task = asyncio.create_task(runner.run_case(env.spec))
    await asyncio.wait_for(stopping.wait(), 3)
    runner.stop_current_task()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.cleaned == ["device-a", "device-b"]


@pytest.mark.asyncio
async def test_transcript_receives_previous_step_and_validator_result(runtime, monkeypatch):
    import artemis.agents.operator.operator as module
    from langchain_core.messages import HumanMessage
    from artemis.memory.transcript import TranscriptLedger

    save_note_content(runtime.engine.base_dir, "task_plan", "- [/] [role:a] Work")
    await runtime.begin_turn(State.initial("Work"))
    previous_id = str(uuid4())
    previous = State(
        initial_goal="Work",
        current_step_id=previous_id,
        structured_decisions='[{"action":"tap"}]',
        last_execution_result={"status": "success"},
    )
    updates = await runtime.begin_turn(previous)
    state = previous.model_copy(update=updates)
    ledger = TranscriptLedger()
    ledger.set_static_prefix([HumanMessage(content="static")])
    ledger.stage_turn([HumanMessage(content="previous observation")])
    captured = MagicMock(wraps=ledger.commit_staged)
    monkeypatch.setattr(ledger, "commit_staged", captured)
    node = object.__new__(module.OperatorNode)
    node.ctx = runtime.contexts["a"]
    node.prompts = {}
    node._previous_turn_silent = False
    node._ensure_transcript_ledger = lambda state: ledger

    class NoOpComponent:
        async def __call__(self, *args, **kwargs):
            return None

    for name in (
        "PlanRecitationPromptComponent",
        "ExecutionIncidentPromptComponent",
        "ObservationPromptComponent",
        "ScreenshotSimilarityPromptComponent",
        "HistoricalStateHintPromptComponent",
        "InjectedInstructionPromptComponent",
        "BackgroundTasksPromptComponent",
        "FeedbackPromptComponent",
        "ReasoningReminderPromptComponent",
        "ToolLimitWarningPromptComponent",
    ):
        monkeypatch.setattr(module, name, NoOpComponent)
    await node._build_prompt_transcript(state, "", "", 2, [], runtime.read_plan())
    assert captured.call_args.kwargs["step_key"] == previous_id
    assert captured.call_args.kwargs["validator_result"] == previous.last_execution_result


@pytest.mark.asyncio
async def test_handoff_commits_previous_roles_staged_transcript(runtime):
    save_note_content(
        runtime.engine.base_dir,
        "task_plan",
        "- [x] [role:a] Send\n- [/] [role:b] Receive",
    )
    runtime.active_role = "a"
    ledger = SimpleNamespace(has_staged_turn=True, commit_staged=MagicMock())
    runtime.contexts["a"].transcript_ledger = ledger
    previous_id = str(uuid4())
    state = State(
        initial_goal="Send then receive",
        current_step_id=previous_id,
        structured_decisions='[{"action":"tap"}]',
        last_execution_result={"status": "success"},
    )
    await runtime.begin_turn(state)
    ledger.commit_staged.assert_called_once_with(
        step_key=previous_id, validator_result={"status": "success"}
    )
    assert runtime.active_role == "b"


@pytest.mark.asyncio
async def test_final_retry_feedback_reaches_reactivated_role(runtime):
    from artemis.graph.checkpoints import append_ledger_record

    text = "- [x] [role:a] Send\n  - verify: Sent\n- [x] [role:b] Receive"
    save_note_content(runtime.engine.base_dir, "task_plan", text)
    ci = parse_plan(text).check_items[0]
    runtime.active_role = "b"
    append_ledger_record(
        runtime.engine.base_dir,
        {
            "checkpoint_id": "final",
            "check_key": ci.key,
            "kind": "verify",
            "status": "failed",
            "item_text": ci.text,
        },
    )
    runtime.prepare_final_retry()
    state = State(initial_goal="Case", operator_feedback=["[verify failed] A message is unsent"])
    update = await runtime.begin_turn(state)
    assert runtime.active_role == "a"
    assert update.get("operator_feedback", state.operator_feedback) == state.operator_feedback


def test_task_level_final_verify_has_a_repair_target(runtime):
    from artemis.graph.checkpoints import append_ledger_record

    text = "- [x] [role:a] Send\n- [x] [role:b] Receive\n\n- verify@end: Both accounts synchronized"
    save_note_content(runtime.engine.base_dir, "task_plan", text)
    ci = parse_plan(text).check_items[0]
    assert ci.parent_key is None
    append_ledger_record(
        runtime.engine.base_dir,
        {
            "checkpoint_id": "final",
            "check_key": ci.key,
            "kind": "verify",
            "status": "failed",
            "item_text": ci.text,
        },
    )
    runtime.prepare_final_retry()
    assert runtime.policy.target(runtime.read_plan()) is not None


@pytest.mark.asyncio
async def test_final_checker_infrastructure_error_is_not_completed(runner_environment, monkeypatch):
    import artemis.graph.graph as graph_module

    env = runner_environment
    env.config.disable_checker = False
    env.config.disable_final_check = False
    monkeypatch.setattr(
        graph_module, "run_final_check", AsyncMock(side_effect=TimeoutError("LLM unavailable"))
    )

    async def graph(runtime):
        async def invoke(*_, **__):
            runtime.plan_path.parent.mkdir(parents=True, exist_ok=True)
            runtime.plan_path.write_text(
                "- [x] [role:a] Send\n- [x] [role:b] Receive", encoding="utf-8"
            )
            return await graph_module.exit_settlement_node(
                State.initial("A sends B receives"), runtime.control_ctx
            )

        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(env.module, "get_multi_device_graph", graph)
    result = await env.module.MultiDeviceAgent(config=env.config).run_case(env.spec)
    assert result["status"] != "completed", result
    assert result["final_review_status"] == "inconclusive"


def test_nested_active_task_selects_its_owning_role(role_plan):
    from artemis.runtime.multi_device.plan_policy import RoleBindingValidator

    text = "- [ ] [role:a] A waits\n- [ ] [role:b] B sends\n  - [/] Send message"
    assert parse_plan(text).active_milestone().role_id == "b"
    with pytest.raises(ValueError, match="later milestone"):
        RoleBindingValidator(role_plan).target(text)


def test_case_correlation_token_is_identical_for_both_roles(runtime):
    token = runtime.correlation_token
    assert token.startswith("artemis_") and len(token) > len("artemis_")
    assert token in runtime.operator_instructions("a")
    assert token in runtime.operator_instructions("b")


@pytest.mark.asyncio
async def test_guidance_on_second_role_is_seen_by_plan_write_policy(runtime):
    from artemis.graph.perception import _unprotect_current_check_lines
    from artemis.graph.graph import build_pro_nodes
    from artemis.tools.tool_wrapper import invoke_tool_with_injection

    runtime.engine.current_step_id = None
    runtime.engine.record_trace = MagicMock(return_value=uuid4())
    runtime.control_ctx.execution_setup.disable_planner_validation = True
    text = "- [x] [role:a] A sends\n- [/] [role:b] B responds\n  - assert: Exact wording"
    save_note_content(runtime.engine.base_dir, "task_plan", text)
    state = State.initial("Case")
    await runtime.begin_turn(state)
    # This is the actual helper Perception invokes when it consumes user guidance.
    _unprotect_current_check_lines(runtime.contexts["b"])
    native = build_pro_nodes(runtime.contexts["b"])["operator"].tools
    tool = next(t for t in runtime.guard_tools(native, "b") if t.name == "save_note")
    with execution_scope(runtime.scope(phase="operator", role_id="b")):
        await invoke_tool_with_injection(
            tool,
            {"key": "task_plan", "content": text.split("\n  - assert:")[0]},
            "review-guidance",
            state,
            record_trace=False,
        )
    assert not parse_plan(runtime.read_plan()).check_items


@pytest.mark.asyncio
async def test_role_prompt_does_not_shift_staged_transcript_boundary(runtime, monkeypatch):
    import artemis.agents.operator.operator as module
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.tools import StructuredTool

    runtime.engine.current_step_id = None
    runtime.engine.record_trace = MagicMock(return_value=uuid4())
    text = "- [/] [role:a] Work"
    save_note_content(runtime.engine.base_dir, "task_plan", text)
    ledger = SimpleNamespace(stage_turn=MagicMock())
    runtime.contexts["a"].transcript_ledger = ledger
    node = module.OperatorNode(runtime.contexts["a"], tools=[])
    node._get_history_and_plan = lambda: ([], text)
    node._available_device_actions = lambda: set()

    async def prompt(*args, **kwargs):
        # The real transcript builder computes this before returning messages.
        node._transcript_turn_base = 1
        return [SystemMessage(content="fixed prefix"), HumanMessage(content="current observation")]

    node._build_prompt = prompt
    node._invoke_llm_loop = AsyncMock(return_value=(None, "done", None, False))
    monkeypatch.setattr(module, "get_llm", MagicMock())
    monkeypatch.setattr(module, "trace_langchain_tool", lambda tool, ctx: tool)
    dummy = StructuredTool.from_function(
        lambda: "ok", name="analyze_task_output", description="Read output"
    )
    monkeypatch.setattr(module.analyze_task_output_wrapper, "tool_fn_getter", lambda ctx: dummy)
    state = State(
        initial_goal="Work",
        latest_ui_hierarchy=[],
        operator_raw_data={"screenshot_b64": "aW1hZ2U=", "xml_hierarchy": []},
    )
    with execution_scope(runtime.scope(phase="operator", role_id="a")):
        await node(state)
    staged = ledger.stage_turn.call_args.args[0]
    assert [m.content for m in staged] == ["current observation"]


@pytest.mark.asyncio
async def test_handoff_stops_previous_role_background_adb_before_switch(runtime, monkeypatch):
    from artemis.tools.command_tool import get_adb_task_registry

    save_note_content(
        runtime.engine.base_dir,
        "task_plan",
        "- [x] [role:a] Send\n- [/] [role:b] Receive",
    )
    runtime.active_role = "a"
    registry = get_adb_task_registry(runtime.contexts["a"])
    registry.background["old-command"] = object()
    stopped = []

    async def shutdown(ctx):
        stopped.append(ctx.role_id)
        registry.background.clear()
        return 1

    monkeypatch.setattr("artemis.tools.command_tool.shutdown_adb_background_tasks", shutdown)
    await runtime.begin_turn(State.initial("Send then receive"))
    assert stopped == ["a"]
    assert runtime.active_role == "b"


@pytest.mark.asyncio
async def test_handoff_refuses_to_leave_running_background_adb(runtime, monkeypatch):
    from artemis.tools.command_tool import get_adb_task_registry

    save_note_content(
        runtime.engine.base_dir,
        "task_plan",
        "- [x] [role:a] Send\n- [/] [role:b] Receive",
    )
    runtime.active_role = "a"
    get_adb_task_registry(runtime.contexts["a"]).background["old-command"] = object()
    monkeypatch.setattr(
        "artemis.tools.command_tool.shutdown_adb_background_tasks",
        AsyncMock(return_value=0),
    )
    with pytest.raises(RuntimeError, match="background ADB"):
        await runtime.begin_turn(State.initial("Send then receive"))
    assert runtime.active_role == "a"
