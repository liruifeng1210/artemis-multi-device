from datetime import datetime, UTC, timedelta
import json
from pathlib import Path
import pytest
from artemis.runtime.session_orchestration.artifacts import (
    AttemptStore,
    require_clean_boundary,
    contained,
)
from artemis.runtime.session_orchestration.contract import (
    accept_result,
    output_model,
    validated_outputs,
)
from artemis.runtime.session_orchestration.journal import Journal
from artemis.runtime.session_orchestration.models import (
    Completion,
    TaskSpec,
    TaskResult,
    CheckResult,
    WorkflowSpec,
    RoleSpec,
    WorkflowTask,
    Dependency,
    new_id,
)
from artemis.runtime.session_orchestration.scheduler import Scheduler


def spec(**kwargs):
    return TaskSpec(
        goal="inspect",
        completion=[Completion(id="done", description="done")],
        execution_timeout_seconds=2,
        **kwargs,
    )


def test_output_schema_strict_and_preserves_identifier():
    schema = {
        "type": "object",
        "required": ["meeting_id"],
        "additionalProperties": False,
        "properties": {"meeting_id": {"type": "string"}},
    }
    assert validated_outputs(schema, {"meeting_id": "001234"}) == {"meeting_id": "001234"}
    for value in ({"meeting_id": 1234}, {"meeting_id": "123", "extra": True}, {}):
        with pytest.raises(ValueError):
            validated_outputs(schema, value)
    with pytest.raises(ValueError):
        output_model({"type": "object", "$ref": "remote"})


def test_success_requires_every_original_check():
    request = spec()
    for verdict in ("inconclusive", "unchecked", "failed"):
        result = TaskResult(
            status="succeeded",
            checks=[CheckResult(id="done", verdict=verdict, evidence_refs=["proof"])],
        )
        with pytest.raises(ValueError):
            accept_result(request, result)
    result = TaskResult(
        status="succeeded",
        checks=[CheckResult(id="done", verdict="passed", evidence_refs=["proof"])],
    )
    accept_result(request, result)
    result.checks.append(result.checks[0])
    with pytest.raises(ValueError):
        accept_result(request, result)


def test_attempt_dedup_and_archive_preserves_business_notes(tmp_path):
    store = AttemptStore(tmp_path, new_id(), new_id())
    assert store.accept({"goal": "one"})
    assert not store.accept({"goal": "one"})
    with pytest.raises(ValueError):
        store.accept({"goal": "different"})
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes/task_plan.md").write_text("plan")
    (tmp_path / "notes/customer.md").write_text("keep")
    store.finish({"status": "succeeded"})
    assert not (tmp_path / "notes/task_plan.md").exists()
    assert (tmp_path / "notes/customer.md").read_text() == "keep"
    assert (store.path / "native/notes/task_plan.md").read_text() == "plan"
    assert store.saved_result()["status"] == "succeeded"
    require_clean_boundary(tmp_path)
    with pytest.raises(ValueError):
        contained(tmp_path, "../escape")


def test_incomplete_archive_rejects_next_task(tmp_path):
    store = AttemptStore(tmp_path, new_id(), new_id())
    (store.path / "manifest.json").write_text(json.dumps({"archive_state": "COMMITTED"}))
    with pytest.raises(RuntimeError, match="Incomplete"):
        require_clean_boundary(tmp_path)


def test_torn_journal_tail_is_not_a_committed_event(tmp_path):
    journal = Journal(tmp_path / "events.jsonl")
    journal.append({"kind": "accepted"})
    with journal.path.open("a") as handle:
        handle.write('{"kind":"res')
    assert journal.read() == [{"kind": "accepted"}]


def test_dag_and_binding_admission(tmp_path):
    first = WorkflowTask(role_id="host", spec=spec())
    second = WorkflowTask(
        role_id="guest", spec=spec(), depends_on=[Dependency(task_id=first.task_id)]
    )
    workflow = WorkflowSpec(
        goal="meeting",
        roles=[RoleSpec(role_id="host", device_id="a"), RoleSpec(role_id="guest", device_id="b")],
        tasks=[first, second],
        root=tmp_path,
        max_workflow_seconds=20,
        max_artifact_bytes=1000000,
    )
    scheduler = Scheduler(workflow)
    assert scheduler.ready(first)
    assert not scheduler.ready(second)
    scheduler.results[first.task_id] = TaskResult(status="blocked")
    assert not scheduler.ready(second)
    scheduler.results[first.task_id] = TaskResult(status="succeeded")
    assert scheduler.ready(second)
    changed = first.model_copy(deep=True)
    changed.spec.goal = "changed"
    with pytest.raises(ValueError):
        scheduler.apply([changed], scheduler.revision)
    cycle_first = first.model_copy(deep=True)
    cycle_first.depends_on = [Dependency(task_id=second.task_id)]
    with pytest.raises(ValueError, match="Cyclic"):
        Scheduler(workflow.model_copy(update={"tasks": [cycle_first, second]}))
