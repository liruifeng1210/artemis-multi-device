"""Written milestone order is the case's execution order, even across roles."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage, ToolMessage

from artemis.graph.state import State
from artemis.runtime.multi_device.plan_policy import RoleBindingValidator
from artemis.utils.notes import save_note_content

pytest_plugins = ("test_multi_device",)

PLAN = "- [/] [role:a] Log in A\n- [ ] [role:b] Log in B\n- [ ] [role:a] Open meeting"


def test_a_b_a_order_cannot_be_skipped_with_later_active(role_plan):
    policy = RoleBindingValidator(role_plan)
    after_a = PLAN.replace("[/] [role:a] Log in A", "[x] [role:a] Log in A")
    assert policy.target(after_a).text == "[role:b] Log in B"
    for status in (" ", "!"):
        candidate = after_a.replace("[ ] [role:b]", f"[{status}] [role:b]")
        candidate = candidate.replace("[ ] [role:a] Open", "[/] [role:a] Open")
        with pytest.raises(ValueError, match="later milestone"):
            policy.target(candidate)
    blocked = after_a.replace("[ ] [role:b]", "[!] [role:b]")
    with pytest.raises(ValueError, match="Blocked milestone"):
        policy.target(blocked)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace(
            "- [ ] [role:b] Log in B\n- [ ] [role:a] Open meeting",
            "- [ ] [role:a] Open meeting\n- [ ] [role:b] Log in B",
        ),
        lambda text: text.replace("- [ ] [role:b] Log in B\n", ""),
        lambda text: text.replace("[role:b] Log in B", "[role:b] B already logged in"),
        lambda text: text.replace("[ ] [role:b] Log in B", "[x] [role:b] Log in B"),
    ],
)
def test_operator_cannot_reorder_delete_rewrite_or_complete_predecessor(role_plan, mutation):
    with pytest.raises(ValueError):
        RoleBindingValidator(role_plan).validate(mutation(PLAN), PLAN, "a")


def test_same_role_successor_and_nested_active_cannot_jump(role_plan):
    policy = RoleBindingValidator(role_plan)
    text = "- [ ] [role:a] Log in\n- [/] [role:a] Open meeting"
    with pytest.raises(ValueError, match="later milestone"):
        policy.target(text)
    text = "- [ ] [role:a] Log in\n- [ ] [role:b] Open meeting\n  - [/] Send invite"
    with pytest.raises(ValueError, match="later milestone"):
        policy.target(text)


def test_operator_cannot_reopen_completed_milestone_but_repair_can(role_plan):
    policy = RoleBindingValidator(role_plan)
    before = "- [x] [role:a] Log in A\n- [/] [role:b] Log in B"
    after = before.replace("[x] [role:a]", "[/] [role:a]").replace("[/] [role:b]", "[ ] [role:b]")
    with pytest.raises(ValueError, match="reopen"):
        policy.validate(after, before, "b")
    assert policy.validate(after, before).top_level[0].is_active
    assert policy.target(after).role_id == "a"


@pytest.mark.asyncio
async def test_runtime_selects_b_before_returning_to_a(runtime):
    after_a = PLAN.replace("[/] [role:a] Log in A", "[x] [role:a] Log in A")
    save_note_content(runtime.engine.base_dir, "task_plan", after_a)
    await runtime.begin_turn(State.initial("A logs in, B logs in, A opens meeting"))
    assert runtime.active_role == "b"
    assert "Log in B" in runtime.operator_instructions("b")
    assert "Do not start work from a later milestone" in runtime.operator_instructions("b")


@pytest.mark.asyncio
async def test_operator_handoff_pairs_every_tool_call_and_defers_screen_action(
    runtime, monkeypatch
):
    import artemis.agents.operator.operator as operator_module

    save_note_content(runtime.engine.base_dir, "task_plan", PLAN)
    await runtime.begin_turn(State.initial("Case"))
    node = object.__new__(operator_module.OperatorNode)
    node.ctx = runtime.contexts["a"]
    node._available_device_actions = lambda: {"click"}

    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "save_note", "args": {"key": "task_plan"}, "id": "plan", "type": "tool_call"},
            {"name": "click", "args": {"target": [500, 500]}, "id": "screen", "type": "tool_call"},
        ],
    )
    monkeypatch.setattr(operator_module, "acomplete", AsyncMock(return_value=response))

    async def write_plan(**kwargs):
        save_note_content(
            runtime.engine.base_dir,
            "task_plan",
            PLAN.replace("[/] [role:a] Log in A", "[x] [role:a] Log in A"),
        )
        return ToolMessage(content="Plan saved", tool_call_id="plan", name="save_note")

    monkeypatch.setattr(operator_module, "invoke_tool_with_injection", write_plan)

    class Span:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(operator_module, "TraceSpan", Span)
    llm = MagicMock()
    messages = []
    state = State.initial("Case")
    actions, *_ = await node._invoke_llm_loop(
        llm, messages, [SimpleNamespace(name="save_note")], [], state
    )
    assert actions is None
    replies = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    assert set(replies) == {"plan", "screen"}
    assert "Not executed" in replies["screen"].content


@pytest.mark.asyncio
async def test_second_cancel_waits_for_lease_worker_before_releasing():
    from artemis.runtime.multi_device.leases import DeviceSetLease

    started, finish = threading.Event(), threading.Event()
    releases = []

    class Lock:
        device_id = "phone-a"

        def acquire(self, **kwargs):
            started.set()
            assert finish.wait(3)

        def release(self):
            releases.append("phone-a")

    lease = DeviceSetLease(["phone-a"], "case", timeout=3)
    lease.locks = [Lock()]
    entering = asyncio.create_task(lease.__aenter__())
    assert await asyncio.to_thread(started.wait, 2)
    entering.cancel()
    for _ in range(100):
        if lease.cancelled.is_set():
            break
        await asyncio.sleep(0.001)
    assert lease.cancelled.is_set()
    entering.cancel()
    await asyncio.sleep(0)
    assert not entering.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await entering
    assert not lease.held
    assert releases == ["phone-a"]
