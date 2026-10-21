"""Reject accidental null value gates without weakening intentional dependencies."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from artemis.runtime.session_orchestration.models import (
    CheckResult, Completion, Dependency, PlanProposal, Recovery, RoleSpec,
    TaskResult, TaskSpec, WorkflowSpec, WorkflowTask, new_id,
)
from artemis.runtime.session_orchestration.scheduler import Scheduler


def task(role, name, dependencies=(), nullable=False):
    return WorkflowTask(role_id=role, display_name=name, depends_on=list(dependencies), spec=TaskSpec(
        goal=name, completion=[Completion(id="result", description="Observed original result")],
        outputs_schema={"type": "object", "properties": {"first_result": {"type": "string", "nullable": nullable}},
                        "required": ["first_result"], "additionalProperties": False},
        execution_timeout_seconds=10,
    ))


def workflow(tmp_path, tasks):
    return WorkflowSpec(goal="Independent roles and explicit recovery", root=tmp_path,
        roles=[RoleSpec(role_id=role, device_id="synthetic-" + role) for role in ("A", "B")],
        tasks=tasks, max_workflow_seconds=5, max_artifact_bytes=1_000_000, fake_worker=True)


@pytest.mark.parametrize("equals", [None, 200, True])
def test_nonnullable_source_rejects_null_or_wrongly_typed_equals(tmp_path, equals):
    original = task("A", "initial")
    continuation = task("A", "continue", [Dependency(task_id=original.task_id, output_key="first_result", equals=equals)])
    with pytest.raises(ValueError, match="For status-only dependencies"):
        Scheduler(workflow(tmp_path, [original, continuation]))


@pytest.mark.parametrize("kwargs,message", [
    ({"output_key": "undeclared", "equals": "200"}, "declared source output"),
    ({"equals": "200"}, "equals requires output_key"),
])
def test_invalid_value_filter_cannot_be_silently_ignored(tmp_path, kwargs, message):
    original = task("A", "initial")
    continuation = task("A", "continue", [Dependency(task_id=original.task_id, **kwargs)])
    with pytest.raises(ValueError, match=message):
        Scheduler(workflow(tmp_path, [original, continuation]))


@pytest.mark.parametrize("filtered", [False, True])
def test_status_dependency_and_actual_value_filter_are_distinct(tmp_path, filtered):
    original = task("A", "initial")
    dep = Dependency(task_id=original.task_id, output_key="first_result" if filtered else None,
                     equals="200" if filtered else None)
    continuation = task("A", "continue", [dep])
    scheduler = Scheduler(workflow(tmp_path, [original, continuation]))
    scheduler.results[original.task_id] = TaskResult(status="succeeded", outputs={"first_result": "201"})
    assert scheduler.ready(continuation) is (not filtered)
    scheduler.results[original.task_id].outputs["first_result"] = "200"
    assert scheduler.ready(continuation)


@pytest.mark.parametrize("kind", ["nullable_flag", "nullable_union"])
def test_explicit_nullable_output_can_compare_null_but_missing_output_cannot(tmp_path, kind):
    original = task("A", "initial", nullable=True)
    if kind == "nullable_union":
        original.spec.outputs_schema["properties"]["first_result"] = {"type": ["string", "null"]}
    continuation = task("A", "continue", [Dependency(task_id=original.task_id, output_key="first_result")])
    scheduler = Scheduler(workflow(tmp_path, [original, continuation]))
    scheduler.results[original.task_id] = TaskResult(status="succeeded", outputs={})
    assert not scheduler.ready(continuation)
    scheduler.results[original.task_id].outputs["first_result"] = None
    assert scheduler.ready(continuation)


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
async def test_actual_main_dispatches_corrected_recovery_and_other_ready_role_or_stops(tmp_path, monkeypatch, recover):
    from artemis.runtime.session_orchestration import main_session as module
    a = task("A", "initial-A")
    b = task("B", "initial-B")
    a_next = task("A", "continue-A", [Dependency(task_id=a.task_id, output_key="first_result", equals="200")])
    recovery = task("B", "recover-B", [Dependency(task_id=b.task_id, statuses=["failed"])])
    recovery.spec.effect_policy = "read_only"
    dispatched, proposals = [], []

    class Worker:
        def __init__(self, config, role, events):
            self.role, self.session_id = role, new_id()
            self.ready, self.state, self.quarantined = asyncio.Event(), "READY", False
            self.process = SimpleNamespace(returncode=None)
            self.last_heartbeat = time.monotonic()

        async def start(self, *args):
            self.ready.set()

        async def execute(self, dispatched_task, attempt):
            dispatched.append(dispatched_task.task_id)
            await asyncio.sleep(0.001)
            failed = dispatched_task.task_id == b.task_id
            result = TaskResult(status="failed" if failed else "succeeded",
                outputs={"first_result": "200" if self.role.role_id == "A" else "8"},
                checks=[CheckResult(id="result", verdict="failed" if failed else "passed", evidence_refs=["synthetic-proof"])],
                side_effect_state="confirmed", session_reusable=True)
            return SimpleNamespace(payload=result.model_dump(mode="json"))

        async def request(self, *args, **kwargs):
            return SimpleNamespace(payload={"state": "READY"})

    class Planner:
        async def propose(self, config, scheduler, facts, diagnostics):
            proposals.append(list(diagnostics))
            if not recover:
                return PlanProposal(base_plan_revision=scheduler.revision, missing_input="Explicit test stop")
            if len(proposals) == 1:
                # Reproduce the live proposal: status failed plus a stray null output filter.
                invalid = recovery.model_copy(deep=True)
                invalid.depends_on[0].output_key = "first_result"
                return PlanProposal(base_plan_revision=scheduler.revision, tasks=[a_next, invalid],
                    recoveries=[Recovery(failed_task_id=b.task_id, replacement_task_ids=[recovery.task_id])])
            assert len(proposals) == 2, "Must dispatch the accepted graph instead of repeatedly replanning"
            assert any(e.get("kind") == "plan_rejected" and "status-only" in e.get("error", "") for e in diagnostics)
            return PlanProposal(base_plan_revision=scheduler.revision, tasks=[a_next, recovery],
                recoveries=[Recovery(failed_task_id=b.task_id, replacement_task_ids=[recovery.task_id])])

    monkeypatch.setattr(module, "WorkerHandle", Worker)
    monkeypatch.setattr(module, "validate_evidence", lambda *args: None)  # Synthetic worker has no artifact store.
    main = module.MainSession(planner=Planner())

    async def cleanup(reason):
        main._closing = True
        for background in (main._event_task, main._monitor_task):
            if background:
                background.cancel()
        await asyncio.gather(main._event_task, main._monitor_task, return_exceptions=True)
        main.server.close()
        await main.server.wait_closed()
        return {}

    main.close = cleanup
    result = await asyncio.wait_for(main.run(workflow(tmp_path, [a, b] if recover else [a, b, a_next])), 2)
    assert a_next.task_id in dispatched  # Other Role remains runnable despite B's failure.
    assert len(dispatched) == len(set(dispatched))
    if recover:
        assert recovery.task_id in dispatched
        assert set(dispatched[2:]) == {a_next.task_id, recovery.task_id}
        assert result["business_status"] == "completed"
        assert result["tasks"][b.task_id]["status"] == "failed"  # No rewritten history.
        assert len(proposals) == 2
    else:
        assert result["business_status"] == "blocked"
        assert len(proposals) == 1  # Empty graph change terminates rather than busy-looping.
