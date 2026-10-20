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


@pytest.mark.asyncio
async def test_startup_cancellation_retains_and_closes_spawned_processes(tmp_path, monkeypatch):
    from artemis.runtime.session_orchestration.worker_handle import WorkerHandle

    entered, release = asyncio.Event(), asyncio.Event()
    original = WorkerHandle._start

    async def delayed_start(worker, host, port):
        entered.set()
        await release.wait()
        await original(worker, host, port)

    monkeypatch.setattr(WorkerHandle, "_start", delayed_start)
    main = MainSession(planner=StopPlanner())
    running = asyncio.create_task(main.run(workflow_at(tmp_path)))
    try:
        await entered.wait()
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 20)
        assert all(
            w.process is not None and w.process.returncode is not None
            for w in main.workers.values()
        )
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_success_from_retired_worker_is_not_accepted(tmp_path):
    workflow = workflow_at(tmp_path, delay=0.3)
    main = MainSession(planner=StopPlanner())
    running = asyncio.create_task(main.run(workflow))
    try:
        await wait_until(lambda: len(main.running) == 2)
        old = main.workers["a"]
        close = old.close

        async def delayed_close(reason):
            # Old execution finishes after invalidation but before termination.
            await asyncio.sleep(0.6)
            return await close(reason)

        old.close = delayed_close
        await main.takeover("a")
        result = await asyncio.wait_for(running, 20)
        assert result["tasks"][workflow.tasks[0].task_id]["status"] == "unknown"
        assert workflow.tasks[0].task_id not in main.facts.results
        assert main.attempt_counts[workflow.tasks[0].task_id] == 1
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_waits_for_takeover_and_closes_replacement(tmp_path):
    main = MainSession(planner=StopPlanner())
    running = asyncio.create_task(main.run(workflow_at(tmp_path, delay=2)))
    recovery = closing = None
    try:
        await wait_until(lambda: len(main.running) == 2)
        old = main.workers["a"]
        recovery = asyncio.create_task(main.takeover("a"))
        await wait_until(lambda: "a" in main.recovering)
        closing = asyncio.create_task(main.close("test close"))
        await asyncio.wait_for(asyncio.gather(recovery, closing, running), 25)
        assert old.process.returncode is not None
        assert main.workers["a"] is not old
        assert all(w.process.returncode is not None for w in main.workers.values())
    finally:
        for task in (recovery, closing, running):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in (recovery, closing, running) if t is not None), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_orphan_emergency_shutdown_kills_descendant_before_owner():
    import os
    import subprocess
    import sys
    import psutil

    script = """
import asyncio, subprocess, sys, os
from artemis.runtime.session_orchestration.worker import Worker
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
print(child.pid, flush=True)
asyncio.run(Worker.terminate_self(None))
"""
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    child = None
    try:
        child_pid = int(await asyncio.wait_for(owner.stdout.readline(), 15))
        try:
            child = psutil.Process(child_pid)
        except psutil.NoSuchProcess:
            pass
        await asyncio.wait_for(owner.wait(), 10)
        assert owner.returncode != 0
        assert child is None or not child.is_running()
    finally:
        if owner.returncode is None:
            owner.kill()
        await owner.wait()
        if child is not None and child.is_running():
            child.kill()
