"""Persistent-session boundaries with native storage and controllable device/graph IO."""

import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import artemis.sdk.role_session as sessions
from artemis.context import DeviceContext, DevicePlatform
from artemis.data_engine.trace import DataEngineCallbackHandler
from artemis.drivers.mock.mock_driver import MockDeviceDriver
from artemis.graph.checkpoints import append_ledger_record, read_ledger
from artemis.graph.graph import get_graph
from artemis.graph.state import State
from artemis.runtime.execution_scope import CURRENT_EXECUTION, current_pause_file, execution_scope
from artemis.runtime.session_runtime import StaleTaskError
from artemis.sdk.agent import Agent
from artemis.sdk.builders import Builders
from artemis.sdk.role_session import RoleSession
from artemis.utils.notes import read_note_content, save_note_content


@pytest.mark.asyncio
async def test_cancel_during_storage_flush_retains_write_and_quarantines(role_factory):
    open_role, handlers = role_factory
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def write():
        started.set()
        release.wait(10)
        finished.set()

    async def run(ctx, state):
        ctx.data_engine._run_in_background(write)
        await ctx.data_engine.flush()
        return state

    handlers["write"] = run
    role = await open_role("A", cleanup_timeout=0.1)
    handle = role.submit("write")
    try:
        await eventually(started.is_set)
        result = await role.cancel(handle.task_id, handle.attempt_id)
        assert result.status == "cancelled" and not result.reusable
        assert not finished.is_set()
        assert role.resources.context.data_engine.has_pending_operations()
        assert role._lease._acquired
        with pytest.raises(RuntimeError, match="quarantined"):
            role.submit("write")
    finally:
        release.set()
        # Let the still-owned cleanup finish before retrying close.
        await eventually(finished.is_set)
        role.cleanup_timeout = 2
        await role.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["log", "ipc"])
async def test_slow_role_telemetry_does_not_block_other_role_cleanup(
    role_factory, monkeypatch, block
):
    from third_party.mobile_use.utils.logger import get_logger

    open_role, handlers = role_factory
    started, release = threading.Event(), threading.Event()
    a = await open_role("A", cleanup_timeout=0.1)
    b = await open_role("B", cleanup_timeout=2)
    engine = a.resources.context.data_engine
    await engine.flush()
    method = "record_trace" if block == "log" else "_send_ipc_event"
    original = getattr(engine, method)

    def slow(*args, **kwargs):
        started.set()
        release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, method, slow)
    logger = get_logger("role-telemetry-isolation")

    async def run(ctx, state):
        logger.info(f"Owned by {ctx.device.device_id}")
        return state

    handlers["work"] = run
    handle_a = a.submit("work")
    try:
        await eventually(started.is_set)
        result_b = await asyncio.wait_for(b.submit("work").result(), 3)
        assert result_b.status == "completed" and result_b.reusable, result_b
        assert not release.is_set()
        result_a = await a.cancel(handle_a.task_id, handle_a.attempt_id)
        assert not result_a.reusable and a._lease._acquired
    finally:
        release.set()
        a.cleanup_timeout = 3
        await a.close()


@pytest.mark.asyncio
async def test_cancel_checkpoint_settlement_books_completed_and_cancelled_checks(role_factory):
    from artemis.agents.checker.checker import CheckReport, CheckVerdict
    from artemis.graph.checkpoints import CheckpointRun, PendingCheckpoint, settle_all_checkpoints
    from artemis.utils.plan_grammar import CheckItem

    open_role, handlers = role_factory
    entered = asyncio.Event()
    tasks = []
    context = None

    async def run(ctx, state):
        nonlocal context
        context = ctx
        for name in ("finished", "waiting"):
            checkpoint = PendingCheckpoint(
                name, name, (CheckItem("verify", "on_complete", name, name),), "", 0
            )
            if name == "finished":
                task = asyncio.get_running_loop().create_future()
                task.set_result(
                    CheckReport(
                        verdicts=[
                            CheckVerdict(
                                item_text=name,
                                kind="verify",
                                status="passed",
                                evidence="observed this task",
                            )
                        ]
                    )
                )
            else:
                task = asyncio.create_task(asyncio.Event().wait())
            tasks.append(task)
            ctx.checkpoint_tasks[name] = CheckpointRun(name + "#1", task, checkpoint)
        entered.set()
        await settle_all_checkpoints(ctx, state)
        return state

    async def next_assignment(ctx, state):
        assert not ctx.checkpoint_tasks
        assert read_ledger(ctx.data_engine.base_dir) == []
        return state

    handlers.update(check=run, next=next_assignment)
    role = await open_role("A", cleanup_timeout=3)
    handle = role.submit("check")
    await entered.wait()
    result = await role.cancel(handle.task_id, handle.attempt_id)
    assert result.status == "cancelled" and result.reusable, result
    assert all(task.done() for task in tasks) and not context.checkpoint_tasks
    rows = read_ledger(result.workspace)
    assert {(row["attempt_id"], row["status"]) for row in rows} == {
        ("finished#1", "passed"),
        ("waiting#1", "cancelled"),
    }
    assert len(rows) == 2
    assert (await role.submit("next").result()).reusable


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["operator", "execution_check", "validator"])
async def test_cancel_commits_only_current_turn_evidence(role_factory, point):
    from artemis.runtime.task_lifecycle import scope_graph_nodes

    open_role, handlers = role_factory
    entered = asyncio.Event()
    expected_step = None

    async def run(ctx, state):
        nonlocal expected_step
        nodes = {}
        builder = SimpleNamespace(add_node=lambda name, fn, **kw: nodes.update({name: fn}))
        scope_graph_nodes(builder, ctx)

        async def operator(state):
            ctx.transcript_ledger.stage_turn(
                [HumanMessage(content="CURRENT observation"), AIMessage(content="CURRENT action")]
            )
            return {"structured_decisions": '[{"action":"tap"}]'}

        async def check(state):
            nonlocal expected_step
            expected_step = str(ctx.data_engine.record_step(summary="CURRENT turn"))
            return {"current_step_id": expected_step}

        async def validator(state):
            return {"last_execution_result": {"status": "CURRENT_RESULT"}}

        for name, fn in (
            ("operator", operator),
            ("execution_check", check),
            ("validator", validator),
        ):
            builder.add_node(name, fn)
        state = state.model_copy(
            update={
                "current_step_id": str(uuid4()),
                "last_execution_result": {"status": "PREVIOUS_RESULT"},
            }
        )
        for name in ("operator", "execution_check", "validator"):
            state = state.model_copy(update=await nodes[name](state))
            role._active["state"] = state
            if name == point:
                break
        entered.set()
        await asyncio.Event().wait()

    handlers["work"] = run
    role = await open_role("A", cleanup_timeout=3)
    handle = role.submit("work")
    await entered.wait()
    result = await role.cancel(handle.task_id, handle.attempt_id)
    assert result.status == "cancelled" and result.reusable, result
    ledger = role.resources.ledger
    messages = str(ledger.active_messages)
    assert "CURRENT action" in messages and "PREVIOUS_RESULT" not in messages
    assert ("CURRENT_RESULT" in messages) == (point == "validator")
    assert ledger._turns[-1]["step_key"] == expected_step


@pytest.mark.asyncio
async def test_continuing_assignments_keep_goal_facts_and_progress_without_old_state(role_factory):
    from artemis.agents.operator.operator import OperatorNode

    open_role, handlers = role_factory
    contexts = []

    async def run(ctx, state):
        contexts.append(ctx)
        assert state.current_step_id is None and state.last_execution_result is None
        assert not state.structured_decisions and state.indexed_points is None
        if state.initial_goal == "create meeting":
            save_note_content(
                ctx.data_engine.base_dir,
                "session_context",
                "Account A is signed in. MeetingID=123. B has not joined yet.",
            )
            save_note_content(ctx.data_engine.base_dir, "task_plan", "- [x] meeting 123 created")
            ctx.transcript_ledger.stage_turn([HumanMessage(content="Meeting 123 created")])
            ctx.transcript_ledger.commit_staged()
        else:
            with pytest.raises(FileNotFoundError):
                read_note_content(ctx.data_engine.base_dir, "task_plan")
            # Frozen history does not evict the explicit continuity context.
            ctx.transcript_ledger.freeze_turns(0, [HumanMessage(content="compressed history")])
        messages = await OperatorNode(ctx, tools=[])._build_prompt_transcript(
            state,
            latest_screenshot_b64="",
            minimal_list="",
            current_step_num=1,
            steps=[],
            task_plan="No plan yet",
        )
        text = str(messages)
        assert "Coordinate A and B in the same meeting" in text
        assert "MeetingID=123" in text and "B has not joined yet" in text
        if state.initial_goal == "approve B":
            assert "create meeting" in text and "completed" in text
            assert "meeting 123 created" in text  # previous plan is history, not the new plan
            assert "approve B" in messages[0].content
        return state

    handlers.update({"create meeting": run, "approve B": run})
    role = await open_role("A", session_goal="Coordinate A and B in the same meeting")
    results = [await role.submit(goal).result() for goal in handlers]
    assert all(result.reusable and result.status == "completed" for result in results), results
    assert contexts[0] is not contexts[1]
    assert contexts[0].transcript_ledger is contexts[1].transcript_ledger
    progress = read_note_content(role.resources.context.data_engine.base_dir, "session_progress")
    assert "create meeting" in progress and "approve B" in progress


@pytest_asyncio.fixture
async def role_factory(tmp_path, monkeypatch):
    # Keep tests hermetic even if a local desktop left stale IPC-port files.
    from artemis.data_engine.engine import DataEngine

    monkeypatch.setattr(DataEngine, "_ipc_port_candidates", staticmethod(lambda: []))
    monkeypatch.setenv("ARTEMIS_FAKE_LLM", "1")
    config = Builders.AgentConfig.build(validate_profiles=False)
    config.video_recording_tools_enabled = False
    config.disable_outputter = True
    handlers = {}
    opened = []

    class FakeAgent(Agent):
        def __init__(self, **kwargs):
            kwargs["config"] = config.model_copy(deep=True)
            super().__init__(**kwargs)
            self.init_count = 0
            self.clean_count = 0

        async def init(self):
            self.init_count += 1
            self._device_context = DeviceContext(
                host_platform="WINDOWS",
                mobile_platform=DevicePlatform.ANDROID,
                device_id=self._config.device_id,
                device_width=1080,
                device_height=2400,
            )
            self._adb_client = None
            self._ui_adb_client = None
            self._initialized = True

        _connect_screen_client = AsyncMock()
        _ensure_device_unlocked = AsyncMock()
        _prepare_device_environment = AsyncMock()

        async def clean(self, force=False):
            self.clean_count += 1
            self._initialized = False

        async def _invoke_prepared_graph(self, *, context, task, state, on_state):
            result = await handlers[task.request.goal](context, state)
            on_state(result)
            return result

        async def _extract_output(self, **kwargs):
            return {"goal": kwargs["request"].goal}

    class Lease:
        def __init__(self, *args, **kwargs):
            self._acquired = False

        def acquire(self, **kwargs):
            self._acquired = True

        def release(self):
            self._acquired = False

    monkeypatch.setattr(sessions, "Agent", FakeAgent)
    monkeypatch.setattr(sessions, "DeviceExecutionLock", Lease)
    monkeypatch.setattr(
        sessions, "create_driver", lambda ctx: MockDeviceDriver(device_id=ctx.device.device_id)
    )

    async def open_role(role_id, *, cleanup_timeout=1, **kwargs):
        role = await RoleSession.open(
            role_id,
            f"device-{role_id}",
            traces_path=tmp_path,
            cleanup_timeout=cleanup_timeout,
            **kwargs,
        )
        opened.append(role)
        return role

    yield open_role, handlers
    for role in opened:
        await role.close()


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_serial_role_tasks_parallel_roles_keep_memory_and_reset_state(role_factory):
    open_role, handlers = role_factory
    entered, release, b_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    contexts = []

    async def first(ctx, state):
        contexts.append(ctx)
        assert state.initial_goal == "first"
        ctx.assert_halt = True
        ctx.transcript_ledger.set_static_prefix([SystemMessage(content="old goal")])
        ctx.transcript_ledger.stage_turn(
            [HumanMessage(content="old observation"), AIMessage(content="done")]
        )
        save_note_content(ctx.data_engine.base_dir, "identity", "signed in as A")
        save_note_content(ctx.data_engine.base_dir, "task_plan", "first plan")
        entered.set()
        await release.wait()
        return state.model_copy(update={"indexed_points": {"old": [1, 2]}})

    async def second(ctx, state):
        contexts.append(ctx)
        assert not ctx.assert_halt
        assert not state.indexed_points
        assert ctx.data_engine.current_step_id is None
        assert ctx.data_engine.last_recorded_step_id is None
        assert ctx.data_engine.current_step_dir is None
        with pytest.raises(ValueError, match="No active step"):
            ctx.data_engine.update_step_action({"old": "must not be reused"})
        assert ctx.transcript_ledger.turn_count == 1
        assert not ctx.transcript_ledger.has_static_prefix
        assert read_note_content(ctx.data_engine.base_dir, "identity") == "signed in as A"
        with pytest.raises(FileNotFoundError):
            read_note_content(ctx.data_engine.base_dir, "task_plan")
        assert "old observation" in str(ctx.transcript_ledger.active_messages)
        return state

    async def other(ctx, state):
        b_entered.set()
        assert ctx.device.device_id == "device-B"
        return state

    handlers.update(first=first, second=second, other=other)
    a, b = await open_role("A"), await open_role("B")
    h1, h2 = a.submit("first"), a.submit("second")
    await entered.wait()
    hb = b.submit("other")
    await b_entered.wait()
    assert not h2._future.done()
    release.set()
    r1, r2, rb = await asyncio.gather(h1.result(), h2.result(), hb.result())
    assert [r.status for r in (r1, r2, rb)] == ["completed"] * 3
    assert all(r.reusable for r in (r1, r2, rb)), (r1, r2, rb)
    assert contexts[0] is not contexts[1]
    assert contexts[0].transcript_ledger is contexts[1].transcript_ledger
    assert r1.workspace != r2.workspace
    assert a.resources.agent.init_count == 1
    assert a.resources.agent.clean_count == 0


@pytest.mark.asyncio
async def test_cancel_is_independent_and_stale_attempt_cannot_cancel_successor(role_factory):
    open_role, handlers = role_factory
    started, forever = asyncio.Event(), asyncio.Event()

    async def wait(ctx, state):
        started.set()
        await forever.wait()
        return state

    async def done(ctx, state):
        return state

    handlers.update(wait=wait, done=done)
    a, b = await open_role("A"), await open_role("B")
    cancelled = a.submit("wait")
    await started.wait()
    successor = a.submit("done")
    rb = await b.submit("done").result()
    r = await a.cancel(cancelled.task_id, cancelled.attempt_id)
    assert r.status == "cancelled" and r.reusable, r
    assert rb.status == "completed"
    assert (await successor.result()).status == "completed"
    assert await a.cancel(successor.task_id, cancelled.attempt_id) is None
    assert (await a.cancel(cancelled.task_id, cancelled.attempt_id)).task_id == cancelled.task_id


@pytest.mark.asyncio
async def test_uncancellable_operation_quarantines_and_holds_lease(role_factory):
    open_role, handlers = role_factory
    started, finish = asyncio.Event(), asyncio.Event()

    async def run(ctx, state):
        async def device_operation():
            started.set()
            await finish.wait()

        await ctx.task_control.own(device_operation())
        return state

    handlers["blocked"] = run
    role = await open_role("A", cleanup_timeout=0.02)
    handle = role.submit("blocked")
    await started.wait()
    r = await role.cancel(handle.task_id, handle.attempt_id)
    assert r.status == "cancelled" and not r.reusable
    assert role.status == "quarantined"
    assert role._lease._acquired
    with pytest.raises(RuntimeError, match="quarantined"):
        role.submit("blocked")
    finish.set()
    await eventually(lambda: not role._retained_runs[0]["control"].operations)
    await eventually(
        lambda: all(
            role._retained_runs[0][name].done() for name in ("cleanup_task", "finalize_task")
        )
    )
    # The tiny deadline exercises device-operation quarantine. Final shutdown
    # includes all process-wide log queue barriers and needs a normal budget.
    role.cleanup_timeout = 1
    await role.close()
    assert not role._lease._acquired


@pytest.mark.asyncio
async def test_task_steps_checks_chunks_and_pause_files_are_isolated(role_factory):
    open_role, handlers = role_factory
    scopes, ids = [], []

    async def record(ctx, state):
        scopes.append(CURRENT_EXECUTION.get())
        scope = scopes[-1]
        step_id = ctx.data_engine.record_step(summary=state.initial_goal)
        ids.append(str(step_id))
        await ctx.data_engine.flush()
        steps = ctx.data_engine.get_agent_friendly_steps()
        assert len(steps) == 1 and steps[0]["summary"] == state.initial_goal
        assert read_ledger(ctx.data_engine.base_dir) == []
        append_ledger_record(
            ctx.data_engine.base_dir, {"checkpoint_id": "same", "status": "passed"}
        )
        ctx.data_engine.record_history_chunk(
            start_step_number=steps[0]["step_number"],
            end_step_number=steps[0]["step_number"],
            version=1,
            status="ready",
            source_step_ids=[str(step_id)],
            rendered_text=state.initial_goal,
        )
        await ctx.data_engine.flush()
        chunks = ctx.data_engine.get_history_chunks()
        assert len(chunks) == 1 and chunks[0].rendered_text == state.initial_goal
        current_pause_file().write_text("paused", encoding="utf-8")
        return state

    handlers.update(first=record, second=record)
    role = await open_role("A")
    r1 = await role.submit("first").result()
    r2 = await role.submit("second").result()
    assert r1.status == r2.status == "completed", (r1, r2)
    assert len(role.resources.context.data_engine.get_session_steps()) == 2
    assert len(ids) == len(set(ids)) == 2
    assert scopes[0].workspace != scopes[1].workspace
    assert scopes[0].workspace.joinpath(".paused").exists()
    assert scopes[1].workspace.joinpath(".paused").exists()


@pytest.mark.asyncio
async def test_device_gateway_rejects_missing_or_other_attempt_scope(role_factory):
    from artemis.drivers.factory import get_driver

    open_role, handlers = role_factory
    saved = []

    async def run(ctx, state):
        saved.append((get_driver(ctx), CURRENT_EXECUTION.get()))
        assert (await get_driver(ctx).get_screen_data()).width > 0
        return state

    handlers["observe"] = run
    role = await open_role("A")
    assert (await role.submit("observe").result()).status == "completed"
    driver, scope = saved[0]
    with pytest.raises(StaleTaskError):
        await driver.get_screen_data()
    with execution_scope(replace(scope, attempt_id="different")):
        with pytest.raises(StaleTaskError):
            await driver.get_screen_data()
    with execution_scope(scope):
        with pytest.raises(StaleTaskError):
            await driver.get_screen_data()


@pytest.mark.asyncio
async def test_native_graph_build_accepts_scoping_and_deferred_convergence(role_factory):
    open_role, handlers = role_factory

    async def run(ctx, state):
        graph = await get_graph(ctx)
        assert "planner" in graph.nodes and "operator" in graph.nodes
        assert "convergence" in graph.nodes
        return state

    handlers["build"] = run
    role = await open_role("A")
    r = await role.submit("build").result()
    assert r.status == "completed" and r.reusable, r


@pytest.mark.asyncio
async def test_llm_end_callback_uses_start_scope(role_factory):
    open_role, handlers = role_factory

    async def run(ctx, state):
        engine = ctx.data_engine
        captured = []
        original = engine.record_trace
        engine.record_trace = lambda **kwargs: captured.append((CURRENT_EXECUTION.get(), kwargs))
        try:
            scope = replace(CURRENT_EXECUTION.get(), step_id=uuid4())
            callback = DataEngineCallbackHandler(ctx)
            run_id = uuid4()
            with execution_scope(scope):
                callback.on_llm_start({"name": "model"}, ["prompt"], run_id=run_id)
            with execution_scope(replace(scope, task_id="wrong", step_id=uuid4())):
                callback.on_llm_end(SimpleNamespace(generations=[]), run_id=run_id)
            assert captured[-1][0].task_id == scope.task_id
            assert captured[-1][1]["step_id"] == scope.step_id
        finally:
            engine.record_trace = original
        return state

    handlers["callback"] = run
    role = await open_role("A")
    r = await role.submit("callback").result()
    assert r.status == "completed", r


@pytest.mark.asyncio
async def test_queued_cancel_is_immediate_and_main_waiter_cancel_is_not_task_cancel(role_factory):
    open_role, handlers = role_factory
    started, finish = asyncio.Event(), asyncio.Event()

    async def run(ctx, state):
        started.set()
        await finish.wait()
        return state

    handlers["wait"] = run
    role = await open_role("A")
    current = role.submit("wait")
    await started.wait()
    waiting = role.submit("wait")
    result = await asyncio.wait_for(role.cancel(waiting.task_id, waiting.attempt_id), 1)
    assert result.status == "cancelled"
    waiter = asyncio.create_task(current.result())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not current._future.done()
    finish.set()
    assert (await current.result()).status == "completed"


@pytest.mark.asyncio
async def test_repeated_cancel_during_settlement_does_not_interrupt_it(role_factory):
    open_role, handlers = role_factory
    started, finish = asyncio.Event(), asyncio.Event()

    async def run(ctx, state):
        async def cleanup_sensitive_job():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await finish.wait()

        ctx.background_tasks.append(asyncio.create_task(cleanup_sensitive_job()))
        await started.wait()
        await asyncio.Event().wait()

    handlers["wait"] = run
    role = await open_role("A")
    handle = role.submit("wait")
    await started.wait()
    cancel1 = asyncio.create_task(role.cancel(handle.task_id, handle.attempt_id))
    await eventually(lambda: role.status == "settling")
    cancel2 = asyncio.create_task(role.cancel(handle.task_id, handle.attempt_id))
    finish.set()
    r1, r2 = await asyncio.gather(cancel1, cancel2)
    assert r1 is r2 and r1.reusable, (r1, r2)
    assert role.status == "idle"


@pytest.mark.asyncio
async def test_raw_background_writes_retain_original_scope(role_factory):
    open_role, handlers = role_factory
    observed = []

    async def run(ctx, state):
        original = CURRENT_EXECUTION.get()
        # _run_in_background is entered without an event loop on this thread;
        # its own manually-created storage thread must still inherit provenance.
        await asyncio.to_thread(
            ctx.data_engine._run_in_background, lambda: observed.append(CURRENT_EXECUTION.get())
        )
        await ctx.data_engine.flush()
        assert observed[-1] is original
        return state

    handlers["write"] = run
    role = await open_role("A")
    r = await role.submit("write").result()
    assert r.status == "completed", r


@pytest.mark.asyncio
async def test_chunk_partition_keeps_identical_milestones_in_separate_tasks(role_factory):
    open_role, handlers = role_factory

    async def run(ctx, state):
        manager = ctx.transcript_ledger.chunker
        manager.on_step_stamped("s1", "same-hash")
        manager.on_step_stamped("s2", "same-hash")
        segments = manager._partition(
            [
                {"step_key": "s1", "task_key": ("one", "attempt1")},
                {"step_key": "s2", "task_key": ("two", "attempt2")},
            ]
        )
        assert len(segments) == 2
        return state

    handlers["partition"] = run
    role = await open_role("A")
    r = await role.submit("partition").result()
    assert r.status == "completed", r


@pytest.mark.asyncio
async def test_native_graph_runs_with_state_models_and_advances_step_scope(
    role_factory, monkeypatch
):
    import importlib

    graph_module = importlib.import_module("artemis.graph.graph")
    from third_party.mobile_use.sdk.agent import AgentBase

    open_role, handlers = role_factory
    seen = []

    def node(name):
        def construct(ctx, **kwargs):
            async def invoke(state):
                assert isinstance(state, State)
                scope = CURRENT_EXECUTION.get()
                assert scope.phase == name
                if name == "planner":
                    assert scope.step_id is None and state.current_step_id is None
                elif name == "operator":
                    assert scope.step_id is not None
                seen.append((state.initial_goal, name, scope.step_id))
                return {}

            return invoke

        return construct

    for name in ("planner", "operator", "validator", "summarizer"):
        monkeypatch.setattr(graph_module, name.title() + "Node", node(name))

    async def perception(state, ctx):
        assert isinstance(state, State)
        ctx.data_engine.allocate_step_id()
        # Native perception allocates a cursor without returning it in State.
        return {}

    async def execution_check(state, ctx):
        assert CURRENT_EXECUTION.get().step_id is not None
        step = ctx.data_engine.record_step(summary=state.initial_goal)
        return {"current_step_id": str(step)}

    def convergence(state, ctx):
        return (
            "end"
            if any(goal == state.initial_goal and name == "summarizer" for goal, name, _ in seen)
            else "continue"
        )

    monkeypatch.setattr(graph_module, "perception_node", perception)
    monkeypatch.setattr(graph_module, "execution_check_node", execution_check)
    monkeypatch.setattr(graph_module, "execution_check_edge", lambda state: "execute_decisions")
    monkeypatch.setattr(graph_module, "convergence_gate", convergence)

    async def run(ctx, state):
        run = role._active
        return await AgentBase._invoke_prepared_graph(
            role.resources.agent,
            context=ctx,
            task=run["task"],
            state=state,
            on_state=lambda value: run.update(state=value),
        )

    handlers.update(first=run, second=run)
    role = await open_role("A")
    results = [await role.submit(goal).result() for goal in ("first", "second")]
    assert all(result.status == "completed" and result.reusable for result in results), results
    cursors = {
        goal: [step for g, n, step in seen if g == goal and n == "operator"][0] for goal in handlers
    }
    assert cursors["first"] != cursors["second"]


@pytest.mark.asyncio
async def test_mcp_server_receives_each_request_context_not_owner_context(role_factory):
    from artemis.data_engine.context_vars import CURRENT_TRACE_ID
    from artemis.mcp.action_session import get_action_session
    from artemis.mcp.actuators.adb import AdbActuator

    open_role, handlers = role_factory
    seen = []

    async def run(ctx, state):
        actuator = AdbActuator(ctx)

        async def hierarchy():
            scope = CURRENT_EXECUTION.get()
            seen.append((scope.task_id, scope.step_id, CURRENT_TRACE_ID.get()))
            return []

        actuator.get_ui_elements = hierarchy
        session = await get_action_session(ctx, actuator)
        for _ in range(2):
            scope = replace(CURRENT_EXECUTION.get(), step_id=uuid4())
            trace = uuid4()
            with execution_scope(scope):
                token = CURRENT_TRACE_ID.set(trace)
                try:
                    await session.ui_hierarchy()
                finally:
                    CURRENT_TRACE_ID.reset(token)
            assert seen[-1] == (scope.task_id, scope.step_id, trace)
        return state

    handlers["transport"] = run
    role = await open_role("A")
    result = await role.submit("transport").result()
    assert result.status == "completed" and result.reusable, result
    assert seen[0][1] != seen[1][1]


@pytest.mark.asyncio
async def test_cancel_open_during_storage_construction_closes_owned_engine(
    role_factory, monkeypatch, tmp_path
):
    import threading

    from artemis.data_engine.session_engine import SessionDataEngine

    started, finish = threading.Event(), threading.Event()
    engines, leases = [], []

    class SlowEngine(SessionDataEngine):
        def __init__(self, ctx):
            started.set()
            finish.wait(5)
            super().__init__(ctx)
            engines.append(self)

    native_lease = sessions.DeviceExecutionLock

    def lease(*args, **kwargs):
        value = native_lease(*args, **kwargs)
        leases.append(value)
        return value

    monkeypatch.setattr(sessions, "SessionDataEngine", SlowEngine)
    monkeypatch.setattr(sessions, "DeviceExecutionLock", lease)
    opening = asyncio.create_task(
        RoleSession.open("opening", "device-opening", traces_path=tmp_path)
    )
    try:
        await eventually(started.is_set)
        opening.cancel()
        await asyncio.sleep(0)
        assert leases[0]._acquired and not opening.done()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert engines[0]._ipc_shutdown
    assert not leases[0]._acquired


@pytest.mark.asyncio
async def test_concurrent_native_storage_and_logs_keep_role_task_provenance(role_factory):
    from third_party.mobile_use.utils.logger import get_logger

    open_role, handlers = role_factory
    logger = get_logger("persistent-storage-test")
    entered = set()
    both = asyncio.Event()

    async def run(ctx, state):
        engine = ctx.data_engine
        engine.allocate_step_id()
        step_id = engine.current_step_id
        entered.add(ctx.device.device_id)
        if len(entered) == 2:
            both.set()
        await both.wait()
        for index in range(5):
            logger.info(f"Owned log {index}")
            engine.record_trace(
                type="tool", name="owned-tool", payload={"index": index}, step_id=step_id
            )
            await asyncio.sleep(0)
        engine.record_step(summary=state.initial_goal)
        return state

    handlers["record"] = run
    roles = [await open_role(name) for name in ("A", "B")]
    handles = [role.submit("record") for role in roles]
    results = await asyncio.gather(*(handle.result() for handle in handles))
    for role, result in zip(roles, results):
        assert result.status == "completed" and result.reusable, result
        engine = role.resources.context.data_engine
        steps = engine.get_session_steps()
        assert len(steps) == 1
        traces = engine.storage.get_traces_for_step(steps[0]["step_id"])
        owned = [
            trace for trace in traces if trace.name in ("owned-tool", "persistent-storage-test")
        ]
        assert len(owned) == 10
        for trace in owned:
            assert str(trace.session_id) == role.session_id
            assert trace.payload["execution"]["role_id"] == role.role_id
            assert trace.payload["execution"]["task_id"] == result.task_id
            assert trace.payload["execution"]["attempt_id"] == result.attempt_id


@pytest.mark.asyncio
async def test_failed_write_quarantines_but_can_still_close_resources(role_factory):
    open_role, handlers = role_factory

    async def run(ctx, state):
        def fail():
            raise OSError("injected storage failure")

        ctx.data_engine._run_in_background(fail)
        return state

    handlers["failure"] = run
    role = await open_role("A")
    result = await role.submit("failure").result()
    assert result.status == "failed" and not result.reusable, result
    assert role.status == "quarantined" and role._lease._acquired
    await role.close()
    assert not role._lease._acquired


@pytest.mark.asyncio
async def test_native_operator_rebuilds_current_goal_while_retaining_committed_history(
    role_factory,
):
    from artemis.agents.operator.operator import OperatorNode

    open_role, handlers = role_factory

    async def run(ctx, state):
        messages = await OperatorNode(ctx, tools=[])._build_prompt_transcript(
            state,
            latest_screenshot_b64="",
            minimal_list="",
            current_step_num=1,
            steps=[],
            task_plan="No plan yet",
        )
        assert state.initial_goal in messages[0].content
        if state.initial_goal == "NEW_GOAL_CREATE_MEETING":
            assert "OLD_GOAL_LOGIN" not in messages[0].content
            assert "A signed in successfully" in str(messages[1:])
        else:
            ctx.transcript_ledger.stage_turn(
                [
                    HumanMessage(content="A signed in successfully"),
                    AIMessage(content="done"),
                ]
            )
        return state

    handlers.update(OLD_GOAL_LOGIN=run, NEW_GOAL_CREATE_MEETING=run)
    role = await open_role("A")
    results = [await role.submit(goal).result() for goal in handlers]
    assert all(result.status == "completed" and result.reusable for result in results), results


@pytest.mark.asyncio
async def test_close_timeout_retains_resource_owner_and_retry_waits_same_task(role_factory):
    open_role, _ = role_factory
    role = await open_role("A")
    entered, finish = asyncio.Event(), asyncio.Event()
    original = role.resources.agent.clean

    async def slow_clean(force=False):
        entered.set()
        await finish.wait()
        await original(force=force)

    role.resources.agent.clean = slow_clean
    role.cleanup_timeout = 0.02
    try:
        with pytest.raises(TimeoutError, match="lease retained"):
            await role.close()
        await entered.wait()
        owner = role._resource_close_task
        assert role.status == "quarantined" and role._lease._acquired and not owner.done()
    finally:
        finish.set()
        role.cleanup_timeout = 1
    await role.close()
    assert role._resource_close_task is owner
    assert role.status == "closed" and not role._lease._acquired
