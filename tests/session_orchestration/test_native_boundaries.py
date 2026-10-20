import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from artemis.memory.transcript import TranscriptLedger
from artemis.runtime.session_scope import TaskScope, SESSION_PAUSE_PATH, pause_path, task_steps
from artemis.runtime.session_operations import TaskOperationTracker
from artemis.mcp.action_session import ActionSession
from artemis.mcp.action_server import build_action_server
from artemis.mcp.actuators import MockActuator
from artemis.context import ArtemisContext, DeviceContext, ExecutionSetup
from artemis.data_engine.engine import DataEngine
from artemis.sdk.persistent_session import PersistentSession, SessionOptions
from artemis.graph.state import State
from artemis.runtime.session_orchestration.models import new_id
from third_party.mobile_use.sdk.types.task import TaskRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("finalize_session", [True, False])
async def test_shared_pro_executor_keeps_one_shot_summary_and_session_semantics(
    monkeypatch, finalize_session
):
    import third_party.mobile_use.sdk.agent as module
    from datetime import datetime
    from third_party.mobile_use.sdk.types.task import Task

    state = State.initial("meeting")
    state.run_outcome = {"task_status": "completed", "tests": {"passed": 1}}

    async def stream(**kwargs):
        yield "values", state.model_dump()

    monkeypatch.setattr(
        module, "get_graph", AsyncMock(return_value=SimpleNamespace(astream=stream))
    )
    monkeypatch.setattr(module, "DataEngineCallbackHandler", lambda ctx: object())
    agent = SimpleNamespace(
        _config=SimpleNamespace(graph_config_callbacks=None),
        _extract_output=AsyncMock(return_value={"meeting_id": "001234"}),
    )
    ctx = SimpleNamespace(data_engine=MagicMock())
    task = Task(
        id=new_id(),
        device=DeviceContext(),
        status="pending",
        request=TaskRequest(goal="meeting"),
        created_at=datetime.now(),
    )
    output, _ = await module.AgentBase._execute_pro_task(
        agent, task, ctx, state, None, finalize_session=finalize_session
    )
    assert output["meeting_id"] == "001234"
    assert ("test_summary" in output) == finalize_session
    assert ctx.data_engine.end_session.call_count == int(finalize_session)
    assert task.status == "completed"


def test_ledger_new_goal_retains_history_and_rejects_staged_boundary():
    ledger = TranscriptLedger()
    first = TaskScope(new_id(), new_id(), "login")
    ledger.begin_task(first, [SystemMessage(content="login")])
    ledger.stage_turn([HumanMessage(content="logged in")])
    second = TaskScope(new_id(), new_id(), "start meeting")
    with pytest.raises(RuntimeError):
        ledger.begin_task(second, [SystemMessage(content="meeting")])
    ledger.commit_staged(step_key="one")
    ledger.begin_task(second, [SystemMessage(content="meeting")])
    snapshot = ledger.export_snapshot(limit=100)
    assert snapshot["messages"][0]["content"] == "meeting"
    assert any(m["content"] == "logged in" for m in snapshot["messages"])
    count = len(snapshot["messages"])
    ledger.begin_task(second, [SystemMessage(content="meeting")])
    assert len(ledger.export_snapshot(limit=100)["messages"]) == count


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_mean_underlying_operation_stopped():
    tracker = TaskOperationTracker()
    event = asyncio.Event()
    caller = asyncio.create_task(tracker.run(event.wait()))
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    assert not (await tracker.drain(0.01)).drained
    event.set()
    assert (await tracker.drain(1)).drained


@pytest.mark.asyncio
async def test_action_barrier_waits_for_abandoned_call():
    actuator = MockActuator()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow():
        entered.set()
        await release.wait()
        return []

    actuator.get_ui_elements = slow
    actions = await ActionSession(build_action_server(actuator)).start()
    try:
        caller = asyncio.create_task(actions.ui_hierarchy())
        await entered.wait()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        with pytest.raises(TimeoutError):
            await actions.wait_idle(0.01)
        release.set()
        await actions.wait_idle(1)
    finally:
        release.set()
        await actions.aclose()
    assert actions.closed


@pytest.mark.asyncio
async def test_flush_reports_a_completed_failed_write(tmp_path, monkeypatch):
    monkeypatch.setattr(DataEngine, "_connect_ipc_locked", lambda *a, **k: False)
    ctx = ArtemisContext(
        device=DeviceContext(),
        execution_setup=ExecutionSetup(traces_path=tmp_path, trace_name="test"),
    )
    engine = DataEngine(ctx)

    def fail():
        raise OSError("disk full")

    engine._run_in_background(fail)
    with pytest.raises(RuntimeError, match="disk full"):
        await engine.flush(2)
    await engine.shutdown()


def test_session_pause_and_zero_step_scope(tmp_path):
    fallback = tmp_path / "default"
    token = SESSION_PAUSE_PATH.set(tmp_path / "session-a")
    try:
        assert pause_path(fallback) == tmp_path / "session-a"
    finally:
        SESSION_PAUSE_PATH.reset(token)
    assert pause_path(fallback) == fallback
    engine = MagicMock(current_step_number=4)
    ctx = SimpleNamespace(data_engine=engine, active_task=TaskScope("t", "a", "g", 4, 4))
    assert task_steps(ctx) == []
    engine.get_agent_friendly_steps_in_range.assert_not_called()


@pytest.mark.asyncio
async def test_native_session_two_tasks_keep_context_memory_and_data(tmp_path, monkeypatch):
    import artemis.sdk.persistent_session as module

    monkeypatch.setattr(DataEngine, "_connect_ipc_locked", lambda *a, **k: False)
    lease = MagicMock()
    monkeypatch.setattr(module, "DeviceExecutionLock", lambda *a, **k: lease)
    profile = SimpleNamespace(llm_config=None)
    agent = SimpleNamespace(
        _initialized=True,
        _session_id=None,
        _tmp_traces_dir=tmp_path,
        _device_context=DeviceContext(),
        _adb_client=None,
        _ui_adb_client=None,
        _tasks=[],
        _config=SimpleNamespace(
            device_id="fixture-device",
            disable_checker=False,
            disable_final_check=False,
            concurrency_mode="per_device",
            max_concurrency=0,
            video_recording_tools_enabled=False,
            agent_profiles={"pro": profile},
            default_profile=profile,
        ),
    )
    agent.clean = AsyncMock()
    agent._ensure_device_unlocked = AsyncMock()
    agent._prepare_app_installation = AsyncMock()
    agent._prepare_device_environment = AsyncMock()
    agent._prepare_app_lock = AsyncMock()
    agent._prepare_output_files = MagicMock()
    agent._get_graph_state = lambda task: State.initial(task.request.goal)

    def tracing(task, context):
        context.execution_setup = ExecutionSetup(traces_path=tmp_path, trace_name=task.id)
        context.data_engine = DataEngine(context)
        context.data_engine.start_session(goal=task.request.goal, session_id=task.id)

    agent._prepare_tracing = tracing
    contexts = []

    async def run(task, ctx, state, output_config, **kwargs):
        assert pause_path(tmp_path / "global") == ctx.execution_setup.pause_path
        plan = ctx.data_engine.base_dir / "notes" / "task_plan.md"
        assert not plan.exists(), "Previous task's active plan leaked into the next task"
        plan.parent.mkdir(exist_ok=True)
        plan.write_text(state.initial_goal)
        contexts.append(ctx)
        if ctx.transcript_ledger is None:
            ctx.transcript_ledger = TranscriptLedger()
        ctx.transcript_ledger.begin_task(
            ctx.active_task, [SystemMessage(content=state.initial_goal)]
        )
        step = ctx.data_engine.record_step(summary=state.initial_goal)
        ctx.transcript_ledger.stage_turn([HumanMessage(content=state.initial_goal)])
        state.current_step_id = str(step)
        kwargs["on_state"](state)
        ctx.assert_halt = True
        ctx.final_check_attempts = 99
        return {}, state

    agent._execute_pro_task = run
    async with PersistentSession(agent, SessionOptions(traces_path=tmp_path)) as session:
        sid = session.context.data_engine.current_session_id
        for goal in ("login", "create meeting"):
            scope = TaskScope(new_id(), new_id(), goal)
            # Simulate the worker control task, created before session startup,
            # which therefore lacks the startup task's ContextVar values.
            from contextvars import Context

            execution = asyncio.create_task(
                session.execute(TaskRequest(goal=goal, profile="pro"), scope), context=Context()
            )
            result = await execution
            assert result.session_reusable
            assert session.state == "READY"
            assert not (session.context.data_engine.base_dir / "notes" / "task_plan.md").exists()
            archived = (
                session.context.data_engine.base_dir
                / "tasks"
                / scope.task_id
                / scope.attempt_id
                / "native"
                / "notes"
                / "task_plan.md"
            )
            assert archived.read_text() == goal
            assert session.context.data_engine.current_session_id == sid
        assert contexts[0] is contexts[1]
        assert session.context.transcript_ledger.turn_count == 2
        assert len(session.context.data_engine.get_agent_friendly_steps()) == 2
        assert not session.context.transcript_ledger.has_staged_turn
    lease.acquire.assert_called_once()
    lease.release.assert_called_once()
    agent._prepare_device_environment.assert_awaited_once()
