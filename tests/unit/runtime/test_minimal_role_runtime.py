"""Behavioral boundaries for the small role-aware Pro runtime."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from artemis.context import ArtemisContext, DeviceContext, ExecutionSetup
from artemis.graph.state import State
from artemis.runtime.execution_scope import ExecutionScope, execution_scope
from artemis.runtime.multi_device.models import RolePlan
from artemis.runtime.multi_device.runtime import MultiDeviceRuntime
from artemis.utils.notes import save_note_content


@pytest.fixture
def runtime(tmp_path):
    engine = SimpleNamespace(
        base_dir=tmp_path,
        current_session_id=uuid4(),
        _publish=lambda *_: None,
    )
    contexts = {}
    for role in ("a", "b"):
        ctx = ArtemisContext(
            device=DeviceContext(device_id=f"device-{role}"),
            execution_setup=ExecutionSetup(disable_checker=True),
        )
        object.__setattr__(ctx, "data_engine", engine)
        contexts[role] = ctx
    role_plan = RolePlan(roles=[{"id": "a", "case_actor": "A"}, {"id": "b", "case_actor": "B"}])
    return MultiDeviceRuntime(role_plan, contexts)


@pytest.mark.asyncio
async def test_same_role_successor_cancels_old_turn_and_starts_with_fresh_state(runtime):
    plan = "- [/] [role:a] First\n- [ ] [role:a] Second"
    save_note_content(runtime.engine.base_dir, "task_plan", plan)
    await runtime.begin_turn(State.initial("Case"))
    old_scope = runtime.scope(phase="validator", role_id="a")
    old_step = str(uuid4())
    previous = State(
        initial_goal="Case",
        current_step_id=old_step,
        structured_decisions='[{"action":"tap"}]',
        last_execution_result={"status": "success"},
        operator_feedback=["Earlier milestone feedback"],
        latest_screenshot="old-screen.png",
        operator_raw_data={"screenshot_b64": "old"},
        indexed_points=[[7, 8]],
        indexed_elements=[{"text": "old"}],
    )
    ledger = SimpleNamespace(has_staged_turn=True, commit_staged=MagicMock())
    runtime.contexts["a"].transcript_ledger = ledger
    save_note_content(runtime.engine.base_dir, "task_plan", plan.replace("[/]", "[x]", 1))

    with execution_scope(old_scope):
        with pytest.raises(RuntimeError, match="fresh observation"):
            runtime.require_device_scope("a")

    update = await runtime.begin_turn(previous)
    ledger.commit_staged.assert_called_once_with(
        step_key=old_step, validator_result={"status": "success"}
    )
    assert runtime.active_role == "a"
    assert runtime.turn_id != old_scope.turn_id
    for key in (
        "structured_decisions",
        "last_execution_result",
        "operator_feedback",
        "latest_screenshot",
        "operator_raw_data",
        "indexed_points",
        "indexed_elements",
        "current_step_id",
    ):
        assert update[key] is None, key

    async def observe(state):
        assert state.operator_raw_data is None
        with execution_scope(runtime.scope(phase="perception", role_id="a")):
            runtime.require_device_scope("a")
        return {"operator_raw_data": {"screenshot_b64": "new"}}

    runtime.nodes = {"a": {"perception": observe}}
    result = await runtime.wrap_node("perception", observe)(previous.model_copy(update=update))
    assert result["operator_raw_data"]["screenshot_b64"] == "new"


@pytest.mark.asyncio
async def test_same_milestone_next_turn_keeps_execution_feedback(runtime):
    save_note_content(runtime.engine.base_dir, "task_plan", "- [/] [role:a] Work")
    await runtime.begin_turn(State.initial("Case"))
    previous = State(
        initial_goal="Case",
        open_incident={"kind": "blocked"},
        last_execution_result={"status": "failed"},
        operator_feedback=["Action failed"],
    )
    update = await runtime.begin_turn(previous)
    current = previous.model_copy(update=update)
    assert current.open_incident == previous.open_incident
    assert current.last_execution_result == previous.last_execution_result
    assert current.operator_feedback == previous.operator_feedback


@pytest.mark.asyncio
async def test_return_to_role_does_not_restore_earlier_milestone_incident(runtime):
    plan = "- [/] [role:a] First\n- [ ] [role:b] Second\n- [ ] [role:a] Third"
    save_note_content(runtime.engine.base_dir, "task_plan", plan)
    await runtime.begin_turn(State.initial("Case"))
    save_note_content(runtime.engine.base_dir, "task_plan", plan.replace("[/]", "[x]", 1))
    to_b = await runtime.begin_turn(
        State(initial_goal="Case", open_incident={"kind": "first"})
    )
    assert runtime.active_role == "b" and to_b["open_incident"] is None
    save_note_content(
        runtime.engine.base_dir,
        "task_plan",
        plan.replace("[/]", "[x]", 1).replace("[ ] [role:b]", "[x] [role:b]"),
    )
    to_a = await runtime.begin_turn(
        State(initial_goal="Case", open_incident={"kind": "second"})
    )
    assert runtime.active_role == "a" and to_a["open_incident"] is None


@pytest.mark.asyncio
async def test_planner_exit_rejects_invalid_initial_role_plan(runtime):
    async def invalid_planner(state):
        runtime.plan_path.parent.mkdir(parents=True, exist_ok=True)
        runtime.plan_path.write_text("- [ ] Unbound work", encoding="utf-8")
        return {}

    with pytest.raises(ValueError, match="role"):
        await runtime.wrap_node("planner", invalid_planner)(State.initial("Case"))


def test_plan_content_conflict_preserves_latest_file(runtime):
    initial = "- [/] [role:a] First"
    latest = "- [x] [role:a] First\n- [ ] [role:b] Second"
    save_note_content(runtime.engine.base_dir, "task_plan", initial)
    save_note_content(runtime.engine.base_dir, "task_plan", latest)
    with pytest.raises(ValueError, match="changed; read it again"):
        runtime.commit_plan(
            runtime.plan_path,
            initial,
            "- [x] [role:a] First",
            ExecutionScope(phase="operator", role_id="a"),
        )
    assert runtime.read_plan() == latest


@pytest.mark.asyncio
async def test_plan_and_action_same_reply_pairs_calls_without_second_model_turn(
    runtime, monkeypatch
):
    import artemis.agents.operator.operator as operator_module

    plan = "- [/] [role:a] First\n- [ ] [role:b] Second"
    save_note_content(runtime.engine.base_dir, "task_plan", plan)
    await runtime.begin_turn(State.initial("Case"))
    node = object.__new__(operator_module.OperatorNode)
    node.ctx = runtime.contexts["a"]
    node._available_device_actions = lambda: {"click"}
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "save_note", "args": {}, "id": "plan", "type": "tool_call"},
            {"name": "click", "args": {}, "id": "screen", "type": "tool_call"},
        ],
    )
    model = AsyncMock(return_value=response)
    monkeypatch.setattr(operator_module, "acomplete", model)

    async def write_plan(**kwargs):
        save_note_content(runtime.engine.base_dir, "task_plan", plan.replace("[/]", "[x]", 1))
        return ToolMessage(content="saved", tool_call_id="plan", name="save_note")

    monkeypatch.setattr(operator_module, "invoke_tool_with_injection", write_plan)

    class Span:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(operator_module, "TraceSpan", Span)
    messages = []
    with execution_scope(runtime.scope(phase="operator", role_id="a")):
        actions, *_ = await node._invoke_llm_loop(
            MagicMock(), messages, [SimpleNamespace(name="save_note")], [], State.initial("Case")
        )
    assert actions is None
    model.assert_awaited_once()
    replies = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    assert set(replies) == {"plan", "screen"}
    assert replies["screen"].status == "error"
    assert runtime.policy.target(runtime.read_plan()).role_id == "b"
