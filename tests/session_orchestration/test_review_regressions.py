"""Regression cases for lifecycle races, separate from happy-path process tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from artemis.context import DeviceContext
from artemis.graph.state import State
from artemis.runtime.session_scope import TaskScope
from artemis.runtime.session_orchestration.artifacts import AttemptStore
from artemis.runtime.session_orchestration.contract import validated_outputs
from artemis.runtime.session_orchestration.facts import FactStore
from artemis.runtime.session_orchestration.journal import Journal
from artemis.runtime.session_orchestration.main_session import MainSession
from artemis.runtime.session_orchestration.models import (
    Completion,
    Dependency,
    PlanProposal,
    Recovery,
    RoleSpec,
    TaskResult,
    TaskSpec,
    WorkflowSpec,
    WorkflowTask,
    new_id,
)
from artemis.runtime.session_orchestration.scheduler import Scheduler
from artemis.runtime.session_orchestration.worker import Worker
from artemis.runtime.session_orchestration.worker_handle import WorkerHandle
from artemis.sdk.persistent_session import PersistentSession, SessionOptions
from third_party.mobile_use.sdk.types.task import TaskRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [1, 2])
@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_cancel_during_settlement_is_latched_and_cleanup_not_abandoned(
    tmp_path, monkeypatch, phase, cancel_caller
):
    import artemis.sdk.persistent_session as module

    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def settle(*args):
        nonlocal calls
        calls += 1
        if calls == phase:
            entered.set()
            await release.wait()
        return {"drained": True}

    monkeypatch.setattr(module, "settle_task", settle)
    state = State.initial("meeting")
    agent = SimpleNamespace(
        _tasks=[],
        _prepare_output_files=MagicMock(),
        _get_graph_state=lambda task: state,
        _execute_pro_task=AsyncMock(return_value=({}, state)),
    )
    session = PersistentSession(agent, SessionOptions())
    engine = MagicMock(base_dir=tmp_path, current_step_number=0)
    engine.flush = AsyncMock(return_value={"pending": [], "failures": {}})
    ctx = SimpleNamespace(
        data_engine=engine,
        device=DeviceContext(),
        reset_task_state=MagicMock(),
        active_task=None,
        execution_setup=SimpleNamespace(pause_path=tmp_path / "pause"),
    )
    engine.begin_task.side_effect = lambda scope: setattr(ctx, "active_task", scope)
    session.context, session.state = ctx, "READY"
    completion = SimpleNamespace(verify=AsyncMock(), persist=AsyncMock())
    scope = TaskScope(new_id(), new_id(), "meeting")
    execution = asyncio.create_task(session.execute(TaskRequest(goal="meeting"), scope, completion))
    await entered.wait()
    try:
        if cancel_caller:
            execution.cancel()
            await asyncio.sleep(0)
            execution.cancel()  # Repeated cancellation must not interrupt settlement.
            await asyncio.sleep(0)
        else:
            assert session.request_cancel(scope.task_id, scope.attempt_id)["accepted"]
        assert session._busy and not execution.done()
        release.set()
        if cancel_caller:
            with pytest.raises(asyncio.CancelledError):
                await execution
        else:
            assert (await execution).cancelled
        persisted = completion.persist.call_args.args[1]
        assert persisted.cancelled and persisted.session_reusable
        assert completion.verify.await_count == int(phase == 2)
        assert calls == 2 and session.state == "READY" and ctx.active_task is None
    finally:
        release.set()
        await asyncio.gather(execution, return_exceptions=True)


def workflow_at(tmp_path):
    task = WorkflowTask(
        role_id="a",
        spec=TaskSpec(
            goal="login",
            completion=[Completion(id="ready", description="logged in")],
            execution_timeout_seconds=2,
        ),
    )
    dependent = WorkflowTask(
        role_id="a",
        spec=task.spec.model_copy(deep=True),
        depends_on=[Dependency(task_id=task.task_id)],
    )
    return WorkflowSpec(
        goal="meeting",
        roles=[RoleSpec(role_id="a", device_id="fake")],
        tasks=[task, dependent],
        root=tmp_path,
        fake_worker=True,
        max_workflow_seconds=30,
        max_artifact_bytes=1000000,
    )


def test_loss_invalidates_success_without_outputs_and_requires_reconciliation(tmp_path):
    workflow = workflow_at(tmp_path)
    task, dependent = workflow.tasks
    scheduler = Scheduler(workflow)
    facts = FactStore(Journal(tmp_path / "facts.jsonl"))
    result = TaskResult(status="succeeded", side_effect_state="confirmed")
    scheduler.results[task.task_id] = result
    facts.accept(task.task_id, new_id(), "a", result, "old")
    assert scheduler.ready(dependent)
    scheduler.invalidated.update(facts.invalidate_role("a", "old"))
    assert task.task_id not in facts.results
    assert not scheduler.ready(dependent)
    assert scheduler.results[task.task_id].status == "succeeded"  # History is immutable.
    replacement = task.model_copy(deep=True)
    replacement.task_id = new_id()
    recovery = Recovery(failed_task_id=task.task_id, replacement_task_ids=[replacement.task_id])
    with pytest.raises(ValueError, match="read-only"):
        scheduler.apply([replacement], scheduler.revision, [recovery])
    replacement.spec.effect_policy = "read_only"
    scheduler.apply([replacement], scheduler.revision, [recovery])
    # Invalidating a retired session cannot erase newly accepted observations.
    facts.accept(task.task_id, new_id(), "a", result, "new")
    assert not facts.invalidate_role("a", "old")
    assert task.task_id in facts.results


def test_invalidated_inputs_propagate_to_executed_descendants(tmp_path):
    workflow = workflow_at(tmp_path)
    task, dependent = workflow.tasks
    scheduler = Scheduler(workflow)
    scheduler.results[task.task_id] = TaskResult(status="succeeded")
    scheduler.executed.add(dependent.task_id)
    scheduler.running.add(dependent.task_id)
    assert scheduler.invalidate({task.task_id}) == {task.task_id, dependent.task_id}
    scheduler.results[dependent.task_id] = TaskResult(status="succeeded")
    assert not scheduler.completed


@pytest.mark.asyncio
async def test_lost_dependency_cancels_exact_dispatched_attempt(tmp_path):
    workflow = workflow_at(tmp_path)
    task, dependent = workflow.tasks
    main = MainSession()
    main.scheduler = Scheduler(workflow)
    main.facts = FactStore(Journal(tmp_path / "facts.jsonl"))
    main.journal = Journal(tmp_path / "events.jsonl")
    success = TaskResult(status="succeeded")
    main.scheduler.results[task.task_id] = success
    main.scheduler.executed.add(dependent.task_id)
    main.facts.accept(task.task_id, new_id(), "a", success, "old")
    worker = SimpleNamespace(session_id="old", role=workflow.roles[0], binding={})
    owner = SimpleNamespace(request=AsyncMock())
    attempt = new_id()
    pending = asyncio.get_running_loop().create_future()
    main.running[dependent.task_id] = (attempt, pending)
    main.dispatched_workers[(dependent.task_id, attempt)] = owner
    main.invalidate_worker(worker)
    await asyncio.gather(*main._dependency_cancellations)
    owner.request.assert_awaited_once_with(
        "cancel_task", {"reason": "dependency_invalidated"}, dependent.task_id, attempt, timeout=10
    )
    pending.cancel()


@pytest.mark.asyncio
async def test_repeated_cancel_does_not_abandon_main_close():
    entered, release = asyncio.Event(), asyncio.Event()

    async def close(reason):
        entered.set()
        await release.wait()
        return {"exited": True}

    main = MainSession()
    main.workers = {"a": SimpleNamespace(close=close)}
    closing = asyncio.create_task(main.close())
    await entered.wait()
    try:
        for _ in range(2):
            closing.cancel()
            await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert await main.close() == [{"exited": True}]
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.asyncio
async def test_process_spawn_completing_during_settlement_is_also_reaped():
    import os
    import subprocess
    import sys
    from artemis.runtime.session_operations import TaskOperationTracker
    from artemis.sdk.session_boundary import settle_task

    tracker = TaskOperationTracker()
    release = asyncio.Event()
    processes = []
    stop = tracker.stop_processes

    async def sweep(timeout):
        await stop(timeout)
        release.set()

    tracker.stop_processes = sweep

    async def spawn():
        await release.wait()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(process)
        tracker.register_process(process)

    caller = asyncio.create_task(tracker.run(spawn()))
    await asyncio.sleep(0)
    ctx = SimpleNamespace(
        background_tasks=set(),
        background_jobs={},
        planner_task=None,
        checkpoint_tasks={},
        operation_tracker=tracker,
        adb_task_registry=None,
        action_session=None,
        transcript_ledger=None,
        step_memory=None,
        data_engine=None,
    )
    try:
        await settle_task(ctx, None, 5)
        await caller
        assert len(processes) == 1 and processes[0].returncode is not None
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_cancel_between_acceptance_and_native_execution(tmp_path):
    workflow = workflow_at(tmp_path)
    task = workflow.tasks[0]
    worker = Worker(
        {
            "workflow_id": new_id(),
            "role_id": "a",
            "child_session_id": new_id(),
            "worker_epoch": new_id(),
            "token": "test",
            "fake_worker": True,
        }
    )
    worker.root = tmp_path
    worker.session = SimpleNamespace(state="READY", execute_spec=AsyncMock())
    worker.send = AsyncMock()
    msg = worker.message(
        "execute", task.spec.model_dump(mode="json"), task=task.task_id, attempt=new_id()
    )
    await worker.dispatch(msg)
    assert worker.cancel(msg.task_id, msg.attempt_id)["accepted"]
    await worker.active
    worker.session.execute_spec.assert_not_awaited()
    result = AttemptStore(tmp_path, msg.task_id, msg.attempt_id).saved_result()
    assert result["status"] == "cancelled" and result["side_effect_state"] == "none"


@pytest.mark.asyncio
async def test_execution_timeout_starts_after_transport_acceptance(tmp_path):
    from artemis.runtime.session_orchestration.testing import FakeSession

    worker = Worker(
        {
            "workflow_id": new_id(),
            "role_id": "a",
            "child_session_id": new_id(),
            "worker_epoch": new_id(),
            "token": "test",
            "fake_worker": True,
        }
    )
    worker.root, worker.session = tmp_path, FakeSession(tmp_path)
    spec = workflow_at(tmp_path).tasks[0].spec
    spec.execution_timeout_seconds = 0.01
    spec.inputs = {"_test_delay": 1}

    async def send(kind, *args):
        if kind == "accepted":
            await asyncio.sleep(0.03)

    worker.send = send
    msg = worker.message("execute", spec.model_dump(mode="json"), task=new_id(), attempt=new_id())
    await worker.dispatch(msg)
    await worker.active
    result = AttemptStore(tmp_path, msg.task_id, msg.attempt_id).saved_result()
    assert result["status"] == "cancelled"


@pytest.mark.asyncio
async def test_timed_out_dispatch_is_never_replayed(tmp_path):
    workflow = workflow_at(tmp_path)
    worker = WorkerHandle(workflow, workflow.roles[0], asyncio.Queue())
    worker.conn = SimpleNamespace(send=AsyncMock())
    worker.connected.set()
    attempt = new_id()
    with pytest.raises(TimeoutError):
        await worker.request("execute", {}, workflow.tasks[0].task_id, attempt, timeout=0.01)
    assert not worker.inflight and attempt in worker.abandoned and worker.quarantined
    with pytest.raises(RuntimeError, match="quarantined"):
        await worker.execute(workflow.tasks[0], new_id())


@pytest.mark.asyncio
async def test_stale_recovery_proposal_cannot_restart_devices(tmp_path):
    import time

    workflow = workflow_at(tmp_path)
    main = MainSession(
        planner=SimpleNamespace(
            propose=AsyncMock(return_value=PlanProposal(base_plan_revision=0, restart_roles=["a"]))
        )
    )
    main.workflow, main.scheduler = workflow, Scheduler(workflow)
    main.facts = FactStore(Journal(tmp_path / "facts.jsonl"))
    main.journal = Journal(tmp_path / "events.jsonl")
    main.workers = {"a": SimpleNamespace(state="FAULTED", session_id="old", quarantined=True)}
    main.deadline = time.monotonic() + 10
    main.takeover = AsyncMock()
    await main.replan()
    main.takeover.assert_not_awaited()


def test_output_contract_distinguishes_omission_null_and_strict_enum():
    schema = {"type": "object", "properties": {"count": {"type": "integer", "enum": [1, 2]}}}
    assert validated_outputs(schema, {}) == {}
    assert validated_outputs(schema, {"count": 1}) == {"count": 1}
    for value in (None, True, "1", 1.0):
        with pytest.raises(ValidationError):
            validated_outputs(schema, {"count": value})
