import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from artemis.runtime.session_orchestration.artifacts import AttemptStore, validate_evidence
from artemis.runtime.session_orchestration.diagnostics import Diagnostics
from artemis.runtime.session_orchestration.facts import FactStore
from artemis.runtime.session_orchestration.journal import Journal
from artemis.runtime.session_orchestration.models import (
    Completion,
    Recovery,
    RoleSpec,
    TaskSpec,
    TaskResult,
    WorkflowSpec,
    WorkflowTask,
    new_id,
)
from artemis.runtime.session_orchestration.scheduler import Scheduler
from artemis.runtime.session_operations import TaskOperationTracker
from artemis.sdk.persistent_session import PersistentSession, SessionOptions


def test_recovery_keeps_failed_history_and_original_acceptance(tmp_path):
    original = WorkflowTask(
        role_id="host",
        spec=TaskSpec(
            goal="send once",
            completion=[Completion(id="sent", description="message is visible")],
            execution_timeout_seconds=2,
        ),
    )
    workflow = WorkflowSpec(
        goal="message",
        roles=[RoleSpec(role_id="host", device_id="a")],
        tasks=[original],
        root=tmp_path,
        max_workflow_seconds=20,
        max_artifact_bytes=100000,
    )
    scheduler = Scheduler(workflow)
    scheduler.results[original.task_id] = TaskResult(status="unknown")
    reconciliation = original.model_copy(deep=True)
    reconciliation.task_id = new_id()
    reconciliation.spec.goal = "Read the message history and verify whether the message was sent"
    recovery = Recovery(
        failed_task_id=original.task_id, replacement_task_ids=[reconciliation.task_id]
    )
    with pytest.raises(ValueError, match="read-only"):
        scheduler.apply([reconciliation], scheduler.revision, [recovery])
    reconciliation.spec.effect_policy = "read_only"
    weakened = reconciliation.model_copy(deep=True)
    weakened.spec.completion = [Completion(id="anything", description="app is open")]
    with pytest.raises(ValueError, match="acceptance"):
        scheduler.apply([weakened], scheduler.revision, [recovery])
    scheduler.apply([reconciliation], scheduler.revision, [recovery])
    assert not scheduler.completed
    scheduler.results[reconciliation.task_id] = TaskResult(status="succeeded")
    assert scheduler.completed
    assert scheduler.results[original.task_id].status == "unknown"


def test_committed_evidence_cannot_be_substituted(tmp_path):
    store = AttemptStore(tmp_path, new_id(), new_id())
    evidence = store.path / "proof.txt"
    evidence.write_text("observed")
    reference = str(evidence.relative_to(store.root))
    result = {"status": "succeeded"}
    store.finish(result)
    validate_evidence(store.root, store.task_id, store.attempt_id, [reference], result)
    evidence.write_text("substituted")
    with pytest.raises(ValueError, match="changed"):
        validate_evidence(store.root, store.task_id, store.attempt_id, [reference], result)
    with pytest.raises(ValueError, match="manifest"):
        validate_evidence(store.root, store.task_id, store.attempt_id, [], {"status": "failed"})


def test_fact_binding_carries_provenance_and_invalidates_after_takeover(tmp_path):
    store = FactStore(Journal(tmp_path / "facts.jsonl"))
    task, attempt, session_id = new_id(), new_id(), new_id()
    store.accept(
        task,
        attempt,
        "host",
        TaskResult(status="succeeded", outputs={"meeting_id": "001234"}),
        session_id,
    )
    binding = {"meeting": {"from_task": task, "key": "meeting_id"}}
    assert store.resolve(binding) == {"meeting": "001234"}
    assert store.provenance(binding)["meeting"]["child_session_id"] == session_id
    store.invalidate_role("host")
    with pytest.raises(KeyError):
        store.resolve(binding)


@pytest.mark.asyncio
async def test_diagnostics_snapshot_pages_include_beyond_first_hundred():
    messages = [{"content": str(i), "password": "hidden"} for i in range(205)]

    class Session:
        state = "READY"

        async def inspect(self, query):
            offset = query["offset"]
            return {
                "transcript": {
                    "messages": messages[offset : offset + 100],
                    "total": len(messages),
                    "next_offset": offset + 100 if offset + 100 < len(messages) else None,
                }
            }

    diagnostics = Diagnostics()
    first = await diagnostics.inspect(Session(), {"limit": 100})
    messages[0]["content"] = "mutated after capture"
    page = await diagnostics.inspect(
        Session(), {"snapshot_id": first["snapshot_id"], "offset": 200, "limit": 10}
    )
    assert len(page["sections"]["transcript"]["messages"]) == 5
    assert page["sections"]["transcript"]["messages"][0]["content"] == "200"
    assert page["sections"]["transcript"]["messages"][0]["password"] == "[REDACTED]"
    assert page["next_cursor"] is None
    assert first["sections"]["transcript"]["messages"][0]["content"] == "0"


@pytest.mark.asyncio
async def test_failed_action_owner_close_does_not_release_lease(monkeypatch):
    import artemis.sdk.persistent_session as module

    monkeypatch.setattr(module, "settle_task", AsyncMock(return_value={"drained": True}))
    actions = SimpleNamespace(closed=False)
    ctx = SimpleNamespace(action_session=actions, data_engine=None)

    async def exit_context(*args):
        ctx.action_session = None

    ctx.__aexit__ = exit_context
    agent = SimpleNamespace(clean=AsyncMock())
    session = PersistentSession(agent, SessionOptions())
    session.context = ctx
    session._lease = MagicMock()
    report = await session.close()
    assert not report["lease_released"]
    assert session.state == "FAULTED"
    session._lease.release.assert_not_called()
    agent.clean.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_is_registered_before_foreground_completes():
    import sys

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    tracker = TaskOperationTracker()
    tracker.register_process(process)
    try:
        await tracker.stop_processes(3)
        assert process.returncode is not None
        assert not tracker.processes
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
