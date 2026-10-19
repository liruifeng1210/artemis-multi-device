"""Behavioral tests for the role boundary, including a complete Pro graph traversal."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from langchain_core.tools import StructuredTool
import pytest

from artemis.context import ArtemisContext, DeviceContext, ExecutionSetup
from artemis.graph.state import State
from artemis.runtime.execution_scope import CURRENT_EXECUTION, ExecutionScope, execution_scope
from artemis.runtime.multi_device.check_policy import RoleCheckPolicy
from artemis.runtime.multi_device.graph import get_multi_device_graph
from artemis.runtime.multi_device.models import MultiDeviceRequest, RolePlan
from artemis.runtime.multi_device.plan_policy import RoleBindingValidator
from artemis.runtime.multi_device.runtime import MultiDeviceRuntime
from artemis.utils.notes import save_note_content
from artemis.utils.plan_grammar import parse_plan


PLAN = "- [ ] [role:a] A sends\n- [ ] [role:b] B replies\n- [ ] [role:a] A receives"


@pytest.fixture
def role_plan():
    return RolePlan(roles=[{"id": "a", "case_actor": "A"}, {"id": "b", "case_actor": "B"}])


@pytest.fixture
def runtime(tmp_path, role_plan):
    events = []
    engine = SimpleNamespace(
        base_dir=tmp_path,
        current_session_id=uuid4(),
        _publish=lambda name, data: events.append((name, data)),
        record_trace=lambda **kwargs: None,
    )
    contexts = {}
    for role in ("a", "b"):
        ctx = ArtemisContext(
            device=DeviceContext(device_id=f"device-{role}"),
            execution_setup=ExecutionSetup(disable_checker=True),
        )
        # No storage/ADB is needed to test the actual graph's routing.
        object.__setattr__(ctx, "data_engine", engine)
        contexts[role] = ctx
    runtime = MultiDeviceRuntime(role_plan, contexts)
    runtime.events = events
    return runtime


@pytest.mark.parametrize(
    "plan",
    [
        "- [ ] No role",
        "- [ ] [role:z] Unknown role",
        "- [ ] [role:a] [role:b] Multiple roles",
        "- [ ] [role:a] Parent\n  - [ ] [role:b] Child switches devices",
        "- [/] [role:a] One\n- [/] [role:b] Two",
        "- [ ] [role:a] Same\n- [ ] [role:a] Same",
    ],
)
def test_reject_invalid_bindings(role_plan, plan):
    with pytest.raises(ValueError):
        RoleBindingValidator(role_plan).validate(plan)


def test_children_and_checks_inherit_without_changing_identity(role_plan):
    content = "- [/] [role:a] [Loop] Inspect\n  - [ ] First\n  - verify: Visible"
    plan = RoleBindingValidator(role_plan).validate(content)
    assert plan.top_level[0].is_loop
    assert plan.check_items[0].role_id == "a"
    assert plan.top_level[0].key == parse_plan(content.replace("[/]", "[x]")).top_level[0].key


def test_cannot_complete_another_role(role_plan):
    with pytest.raises(ValueError, match="another role"):
        RoleBindingValidator(role_plan).validate(
            PLAN.replace("[ ] [role:b]", "[x] [role:b]"), PLAN, "a"
        )


def test_request_rejects_reused_device():
    with pytest.raises(ValueError, match="distinct"):
        MultiDeviceRequest(
            case="Case", role_bindings={"a": {"serial": "same"}, "b": {"serial": "same"}}
        )


@pytest.mark.asyncio
async def test_actual_pro_graph_routes_a_b_a(monkeypatch, runtime):
    import artemis.runtime.multi_device.graph as graph_module

    operations, observations = [], []
    attempted = set()

    def nodes(ctx):
        async def planner(state):
            save_note_content(runtime.engine.base_dir, "task_plan", PLAN)
            return {}

        async def perception(state):
            assert CURRENT_EXECUTION.get().role_id == ctx.role_id
            observations.append(ctx.device.device_id)
            return {"operator_raw_data": {"screenshot_b64": "", "xml_hierarchy": []}}

        async def operator(state):
            if runtime.origin_hash not in attempted:
                attempted.add(runtime.origin_hash)
                return {"structured_decisions": '[{"action":"tap"}]'}
            text = runtime.read_plan()
            target = runtime.policy.target(text)
            save_note_content(
                runtime.engine.base_dir,
                "task_plan",
                text.replace(f"- [ ] {target.text}", f"- [x] {target.text}", 1),
            )
            # Even a decision returned in the same reply as a handoff must be suppressed.
            return {"structured_decisions": '[{"action":"tap"}]'}

        async def execution_check(state):
            return {"checker_success": True, "current_step_id": str(runtime.step_id)}

        async def validator(state):
            runtime.require_device_scope(ctx.role_id)
            operations.append(ctx.device.device_id)
            return {"last_execution_result": {"status": "success"}}

        async def summarizer(state):
            return {}

        return dict(
            planner=planner,
            perception=perception,
            operator=operator,
            execution_check=execution_check,
            validator=validator,
            summarizer=summarizer,
        )

    monkeypatch.setattr(graph_module, "build_pro_nodes", nodes)
    graph = await get_multi_device_graph(runtime)
    await graph.ainvoke(
        State.initial("A sends, B replies, A receives").model_dump(),
        config={"recursion_limit": 100},
    )
    assert operations == ["device-a", "device-b", "device-a"]
    assert observations == ["device-a", "device-a", "device-b", "device-b", "device-a", "device-a"]
    assert all(i.is_done for i in parse_plan(runtime.read_plan()).top_level)


@pytest.mark.asyncio
async def test_plan_handoff_blocks_immediate_helper_side_effect(runtime):
    from artemis.tools.tool_wrapper import invoke_tool_with_injection

    save_note_content(runtime.engine.base_dir, "task_plan", PLAN)
    await runtime.begin_turn(State.initial("Case"))
    calls = []

    async def adb(command: str):
        """Operate the device."""
        calls.append(command)
        return "done"

    guarded = runtime.guard_tools([StructuredTool.from_function(coroutine=adb)], "a")[0]
    with execution_scope(runtime.scope(phase="operator", role_id="a")):
        save_note_content(
            runtime.engine.base_dir,
            "task_plan",
            PLAN.replace("[ ] [role:a] A sends", "[x] [role:a] A sends"),
        )
        with pytest.raises(RuntimeError, match="fresh observation"):
            await invoke_tool_with_injection(
                guarded, {"command": "tap"}, "call-1", State.initial("Case"), record_trace=False
            )
    assert calls == []


@pytest.mark.asyncio
async def test_runtime_guard_allows_shell_in_active_role_only(runtime):
    from artemis.tools.tool_wrapper import invoke_tool_with_injection

    called = []

    async def adb(CommandLine: str):
        """Run a shell command."""
        called.append(CommandLine)

    tool = StructuredTool.from_function(coroutine=adb, name="run_adb_command")
    guarded = runtime.guard_tools([tool], "a")[0]
    save_note_content(runtime.engine.base_dir, "task_plan", PLAN)
    await runtime.begin_turn(State.initial("Case"))
    with execution_scope(runtime.scope(phase="operator", role_id="a")):
        await invoke_tool_with_injection(
            guarded,
            {"CommandLine": "input secret"},
            "call",
            State.initial("Case"),
            record_trace=False,
        )
    assert called == ["input secret"]
    with pytest.raises(RuntimeError, match="matching role scope"):
        await invoke_tool_with_injection(
            guarded,
            {"CommandLine": "input blocked"},
            "other-call",
            State.initial("Case"),
            record_trace=False,
        )
    assert called == ["input secret"]


@pytest.mark.asyncio
async def test_real_action_sessions_route_a_b_a_with_separate_drivers(runtime, monkeypatch):
    """Exercise MCP -> AdbActuator -> controller -> driver, not mocked nodes."""
    from artemis.drivers.factory import get_driver
    from artemis.mcp.action_session import get_action_session

    monkeypatch.setenv("ARTEMIS_MOCK_DRIVER", "1")
    for index, ctx in enumerate(runtime.contexts.values(), 1):
        ctx.device.device_width = 100 * index
        ctx.device.device_height = 200 * index
    save_note_content(runtime.engine.base_dir, "task_plan", PLAN)
    state = State.initial("A computes, B multiplies, A divides")
    sessions = {}
    actual_devices = []
    try:
        for role in ("a", "b", "a"):
            await runtime.begin_turn(state)
            assert runtime.active_role == role
            ctx = runtime.contexts[role]
            with execution_scope(runtime.scope(phase="operator", role_id=role)):
                session = await get_action_session(ctx)
                if role in sessions:
                    assert session is sessions[role]
                sessions[role] = session
                result = await session.call("click", {"target": [500, 500]})
                assert result.ok
                actual_devices.append(get_driver(ctx).device_id)
                other = "b" if role == "a" else "a"
                if other in sessions:
                    with pytest.raises(RuntimeError, match="matching role scope"):
                        await sessions[other].call("click", {"target": [500, 500]})
            text = runtime.read_plan()
            target = runtime.policy.target(text)
            save_note_content(
                runtime.engine.base_dir,
                "task_plan",
                text.replace(f"- [ ] {target.text}", f"- [x] {target.text}", 1),
            )
        a, b = (get_driver(runtime.contexts[role]) for role in ("a", "b"))
        assert actual_devices == ["device-a", "device-b", "device-a"]
        assert a is not b and sessions["a"] is not sessions["b"]
        assert [(r["x"], r["y"]) for r in a.action_history] == [(50, 100), (50, 100)]
        assert [(r["x"], r["y"]) for r in b.action_history] == [(100, 200)]
    finally:
        for session in sessions.values():
            await session.aclose()


@pytest.mark.asyncio
async def test_late_background_task_keeps_role_but_loses_action_authority(runtime):
    save_note_content(runtime.engine.base_dir, "task_plan", PLAN)
    await runtime.begin_turn(State.initial("Case"))
    release = asyncio.Event()

    async def late():
        await release.wait()
        assert CURRENT_EXECUTION.get().role_id == "a"
        with pytest.raises(RuntimeError, match="fresh observation"):
            runtime.require_device_scope("a")

    with execution_scope(runtime.scope(phase="operator", role_id="a")):
        task = asyncio.create_task(late())
    runtime.turn_id = "next-turn"
    release.set()
    await task


@pytest.mark.asyncio
async def test_engine_cursor_is_task_local(tmp_path, monkeypatch):
    from artemis.runtime.multi_device.data_engine import RoleDataEngine

    monkeypatch.setattr(RoleDataEngine, "_connect_ipc_locked", lambda self: None)
    ctx = ArtemisContext(
        device=DeviceContext(device_id="root"), execution_setup=ExecutionSetup(traces_path=tmp_path)
    )
    engine = RoleDataEngine(ctx)
    ready = asyncio.Event()

    async def worker(role):
        with execution_scope(ExecutionScope(engine=engine, role_id=role)):
            step = engine.allocate_step_id()
            ready.set()
            await asyncio.sleep(0)
            assert engine.current_step_id == step
            return step

    a, b = await asyncio.gather(worker("a"), worker("b"))
    assert a != b
    assert engine.current_step_id is None
    await engine.shutdown()


def test_identical_checks_from_two_roles_are_counted_separately():
    from artemis.graph.checkpoints import compute_test_summary

    snapshot = parse_plan(
        "- [x] [role:a] Login A\n  - assert: Logged in\n- [x] [role:b] Login B\n  - assert: Logged in"
    )
    a, b = snapshot.check_items
    report = compute_test_summary(
        list(snapshot.check_items),
        [
            {
                "check_key": a.key,
                "role_id": "a",
                "kind": "assert",
                "item_text": a.text,
                "status": "passed",
            },
            {
                "check_key": b.key,
                "role_id": "b",
                "kind": "assert",
                "item_text": b.text,
                "status": "failed",
            },
        ],
        policy=RoleCheckPolicy(None),
    )
    assert report.passed == 1
    assert report.failed == 1
    assert report.failed_items[0]["role_id"] == "b"


@pytest.mark.asyncio
async def test_failed_verify_returns_to_its_device_before_handoff(runtime):
    from artemis.agents.checker.checker import CheckReport, CheckVerdict
    from artemis.graph.checkpoints import CheckpointRun, PendingCheckpoint

    text = "- [x] [role:a] Send\n  - verify: Sent\n- [ ] [role:b] Receive"
    save_note_content(runtime.engine.base_dir, "task_plan", text)
    runtime.control_ctx.execution_setup.disable_checker = False
    milestone = parse_plan(text).top_level[0]
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
                        evidence="Message still unsent",
                        check_key=ci.key,
                        role_id="a",
                    )
                ]
            ),
        )
    )
    pending = PendingCheckpoint(milestone.key, milestone.text, (ci,), text, 0, "a")
    runtime.control_ctx.checkpoint_tasks[milestone.key] = CheckpointRun("test#1", task, pending)
    update = await runtime.begin_turn(State.initial("Case"))
    assert runtime.active_role == "a"
    assert runtime.policy.target(runtime.read_plan()).is_active
    assert any("verify failed" in text for text in update["operator_feedback"])


@pytest.mark.asyncio
async def test_unestablished_verify_never_authorizes_next_device(runtime):
    from artemis.graph.checkpoints import append_ledger_record

    text = "- [x] [role:a] Send\n  - verify: Sent\n- [ ] [role:b] Receive"
    save_note_content(runtime.engine.base_dir, "task_plan", text)
    milestone = parse_plan(text).top_level[0]
    ci = parse_plan(text).check_items[0]
    append_ledger_record(
        runtime.engine.base_dir,
        {
            "checkpoint_id": milestone.key,
            "check_key": ci.key,
            "kind": "verify",
            "status": "inconclusive",
            "item_text": ci.text,
        },
    )
    with pytest.raises(RuntimeError, match="handoff precondition"):
        await runtime.begin_turn(State.initial("Case"))
    assert runtime.active_role is None


def test_final_failed_verify_reopens_only_owning_role(runtime):
    from artemis.graph.checkpoints import append_ledger_record

    text = "- [x] [role:a] Send\n  - verify: Sent\n- [x] [role:b] Receive\n  - verify: Received"
    save_note_content(runtime.engine.base_dir, "task_plan", text)
    ci = parse_plan(text).check_items[1]
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
    snapshot = parse_plan(runtime.read_plan())
    assert snapshot.top_level[0].is_done
    assert runtime.policy.target(runtime.read_plan()).role_id == "b"


@pytest.mark.asyncio
async def test_checkpoint_callback_keeps_original_role_and_step(runtime):
    step = uuid4()

    async def callback(ctx):
        assert ctx is runtime.contexts["b"]
        scope = CURRENT_EXECUTION.get()
        assert scope.role_id == "b" and scope.step_id == step and scope.subgoal_hash == "origin-b"

    runtime.active_role = "a"
    await runtime.run_checkpoint("b", callback, step_id=step, subgoal_hash="origin-b")


def test_duplicate_check_text_requires_unambiguous_verdict_identity():
    from artemis.agents.checker.checker import CheckReport, CheckVerdict, _normalize_report

    items = parse_plan(
        "- [x] [role:a] A\n  - assert: Logged in\n- [x] [role:b] B\n  - assert: Logged in"
    ).check_items
    report = _normalize_report(
        CheckReport(
            verdicts=[
                CheckVerdict(
                    kind="assert", item_text="Logged in", status="passed", evidence="A is logged in"
                )
            ]
        ),
        list(items),
        policy=RoleCheckPolicy(None),
    )
    assert [v.status for v in report.verdicts] == ["inconclusive", "inconclusive"]


@pytest.mark.parametrize("key", ["task_plan.md", "TASK_PLAN.MD"])
def test_note_aliases_cannot_bypass_role_validation(runtime, key):
    save_note_content(runtime.engine.base_dir, "task_plan", PLAN)
    with execution_scope(runtime.scope(phase="operator", role_id="a")):
        with pytest.raises(ValueError):
            save_note_content(runtime.engine.base_dir, key, "- [ ] No role")
    assert runtime.read_plan() == PLAN


@pytest.mark.asyncio
async def test_transport_rechecks_queued_operation_before_sending(runtime):
    from artemis.mcp.action_session import ActionSession
    from unittest.mock import AsyncMock

    save_note_content(runtime.engine.base_dir, "task_plan", PLAN)
    await runtime.begin_turn(State.initial("Case"))
    transport = SimpleNamespace(call_tool=AsyncMock())
    session = ActionSession(None)
    session.device_context = runtime.contexts["a"]
    session._session = transport
    with execution_scope(runtime.scope(phase="validator", role_id="a")):
        queued = asyncio.create_task(session.call_raw("tap", {"x": 1, "y": 1}))
    await asyncio.sleep(0)
    runtime.turn_id = "new-turn"
    session._owner = asyncio.create_task(session._serve(transport))
    with pytest.raises(RuntimeError, match="fresh observation"):
        await queued
    await session.aclose()
    transport.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_note_wrapper_queues_completion_on_case_control_context(runtime):
    from unittest.mock import MagicMock
    from artemis.graph.graph import build_pro_nodes
    from artemis.tools.tool_wrapper import invoke_tool_with_injection

    runtime.engine.current_step_id = None
    runtime.engine.record_trace = MagicMock(return_value=uuid4())
    runtime.control_ctx.execution_setup.disable_checker = False
    runtime.control_ctx.execution_setup.disable_midway_checks = False
    runtime.control_ctx.execution_setup.disable_planner_validation = True
    text = "- [x] [role:a] A sends\n- [ ] [role:b] B replies\n  - verify: Reply sent"
    save_note_content(runtime.engine.base_dir, "task_plan", text)
    state = State.initial("A sends B replies")
    await runtime.begin_turn(state)
    tools = build_pro_nodes(runtime.contexts["b"])["operator"].tools
    tool = next(t for t in runtime.guard_tools(tools, "b") if t.name == "save_note")
    with execution_scope(runtime.scope(phase="operator", role_id="b")):
        result = await invoke_tool_with_injection(
            tool,
            {
                "key": "task_plan.md",
                "content": text.replace("[ ] [role:b]", "[x] [role:b]"),
            },
            "complete-b",
            state,
            record_trace=False,
        )
    assert result.status == "success"
    assert len(runtime.control_ctx.pending_checkpoints) == 1
    assert runtime.control_ctx.pending_checkpoints[0].role_id == "b"
    assert runtime.contexts["b"].pending_checkpoints == []
