"""Exercise actual processes and TCP, without using a model or physical device."""

import asyncio
import json

import pytest

from artemis.runtime.session_orchestration.main_session import MainSession
from artemis.runtime.session_orchestration.models import (
    Completion,
    PlanProposal,
    RoleSpec,
    TaskSpec,
    WorkflowSpec,
    WorkflowTask,
    new_id,
)


class StopPlanner:
    async def propose(self, workflow, scheduler, facts, diagnostics=None):
        return PlanProposal(base_plan_revision=scheduler.revision, missing_input="test stop")


@pytest.mark.asyncio
async def test_main_cancellation_closes_workers_and_persists_outcome(tmp_path):
    workflow = workflow_at(tmp_path, delay=30)
    main = MainSession(planner=StopPlanner())
    running = asyncio.create_task(main.run(workflow))
    try:
        await wait_until(lambda: len(main.running) == 2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        outcome = json.loads((workflow.root / workflow.workflow_id / "result.json").read_text())
        assert outcome["business_status"] == "cancelled"
        assert len(outcome["cleanup_status"]) == 2
        assert all(worker.process.returncode is not None for worker in main.workers.values())
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


def workflow_at(path, delay=0.8):
    tasks = [
        WorkflowTask(
            role_id=role,
            spec=TaskSpec(
                goal="work-" + role,
                inputs={"_test_delay": delay},
                completion=[Completion(id="done", description="work is complete")],
                execution_timeout_seconds=10,
            ),
        )
        for role in ("a", "b")
    ]
    return WorkflowSpec(
        goal="isolation",
        roles=[
            RoleSpec(role_id="a", device_id="fake-a"),
            RoleSpec(role_id="b", device_id="fake-b"),
        ],
        tasks=tasks,
        root=path,
        max_workflow_seconds=30,
        max_artifact_bytes=10_000_000,
        close_timeout_seconds=3,
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=3,
        reconnect_grace_seconds=3,
        fake_worker=True,
    )


async def wait_until(predicate, timeout=15):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_reconnect_replays_same_attempt_without_reexecuting(tmp_path):
    workflow = workflow_at(tmp_path)
    main = MainSession(planner=StopPlanner())
    running = asyncio.create_task(main.run(workflow))
    try:
        await wait_until(lambda: len(main.running) == 2)
        worker = main.workers["a"]
        pid, session_id = worker.process.pid, worker.session_id
        await worker.conn.close()
        result = await asyncio.wait_for(running, 30)
        assert result["business_status"] == "completed", result
        assert worker.process.pid == pid and worker.session_id == session_id
        evidence = list(
            (workflow.root / workflow.workflow_id / "traces" / session_id).glob(
                "tasks/*/*/test_evidence.json"
            )
        )
        assert len(evidence) == 1
        assert json.loads(evidence[0].read_text())["history"] == ["work-a"]
        assert any(e["kind"] == "connection_lost" for e in main.journal.read())
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_is_exact_and_other_role_finishes(tmp_path):
    workflow = workflow_at(tmp_path, delay=1)
    main = MainSession(planner=StopPlanner())
    running = asyncio.create_task(main.run(workflow))
    try:
        await wait_until(lambda: len(main.running) == 2)
        a = workflow.tasks[0]
        attempt = main.running[a.task_id][0]
        # A dispatch coroutine must reach the worker before cancellation.
        await wait_until(
            lambda: main.workers["a"].state == "EXECUTING" and bool(main.workers["a"].inflight)
        )
        await asyncio.sleep(0.1)
        stale = await main.workers["a"].request("cancel_task", {}, a.task_id, new_id(), timeout=3)
        assert not stale.payload["accepted"]
        assert stale.payload["code"] == "stale_attempt"
        receipt = await main.cancel(a.task_id)
        assert receipt["accepted"]
        assert main.running[a.task_id][0] == attempt
        result = await asyncio.wait_for(running, 30)
        assert result["tasks"][a.task_id]["status"] == "cancelled"
        assert result["tasks"][workflow.tasks[1].task_id]["status"] == "succeeded"
        assert main.attempt_counts[a.task_id] == 1
        assert all(w.process.returncode == 0 for w in main.workers.values())
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_crashed_worker_becomes_unknown_without_replaying_side_effect(tmp_path):
    workflow = workflow_at(tmp_path, delay=1)
    main = MainSession(planner=StopPlanner())
    running = asyncio.create_task(main.run(workflow))
    try:
        await wait_until(lambda: len(main.running) == 2)
        await main.workers["a"].terminate()
        result = await asyncio.wait_for(running, 30)
        assert result["tasks"][workflow.tasks[0].task_id]["status"] == "unknown"
        assert result["tasks"][workflow.tasks[1].task_id]["status"] == "succeeded"
        assert main.attempt_counts[workflow.tasks[0].task_id] == 1
        assert main.archived_inspection("a")["consistency"] == "committed_artifacts"
        assert all(w.process.returncode is not None for w in main.workers.values())
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_takeover_confirms_exit_and_uses_new_identity(tmp_path):
    workflow = workflow_at(tmp_path, delay=1)
    main = MainSession(planner=StopPlanner())
    running = asyncio.create_task(main.run(workflow))
    try:
        await wait_until(lambda: len(main.running) == 2)
        old = main.workers["a"]
        replacement = await main.takeover("a", "test recovery")
        assert old.process.returncode is not None
        assert replacement["session_id"] != old.session_id
        assert replacement["epoch"] != old.epoch
        assert main.workers["a"].process.pid != old.process.pid
        await asyncio.wait_for(running, 30)
        assert all(w.process.returncode is not None for w in main.workers.values())
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
