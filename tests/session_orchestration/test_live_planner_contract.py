"""Regression for the missing wire schema discovered with the real CLI backend."""
import json
from types import SimpleNamespace
import pytest
from artemis.runtime.session_orchestration.models import PlanProposal
from artemis.runtime.session_orchestration.planner import MainPlanner


@pytest.mark.asyncio
async def test_main_exposes_exact_nested_wire_contract(monkeypatch):
    from artemis.services import llm
    from artemis.llm.router import ModelFactory
    from artemis.runtime.session_orchestration.models import RoleSpec
    captured = []
    async def complete(model, messages, *, schema):
        captured.extend(messages)
        return schema(base_plan_revision=1, missing_input="test only")
    monkeypatch.setattr(ModelFactory, "get_model", lambda endpoint: object())
    monkeypatch.setattr(llm, "acomplete_structured", complete)
    workflow = SimpleNamespace(goal="calculate", constraints=[], expected_cancellations=[], roles=[RoleSpec(role_id="A",device_id="test")])
    scheduler = SimpleNamespace(revision=1,tasks={},results={},invalidated=set(),recoveries={})
    facts = SimpleNamespace(versions=[], sources={})
    await MainPlanner().propose(workflow, scheduler, facts)
    prompt = captured[0].content
    advertised = json.loads(prompt.split("The exact required JSON Schema follows; use its field names and nesting:\n")[1])
    assert advertised == PlanProposal.model_json_schema()
    assert "base_plan_revision" in advertised["required"]
    assert "spec" in advertised["$defs"]["WorkflowTask"]["required"]


@pytest.mark.asyncio
async def test_rejected_proposal_feeds_validation_error_to_next_model_call(tmp_path):
    import copy
    import time
    from artemis.runtime.session_orchestration.main_session import MainSession
    from artemis.runtime.session_orchestration.models import Completion, RoleSpec, TaskSpec, WorkflowSpec, WorkflowTask
    from artemis.runtime.session_orchestration.scheduler import Scheduler

    task = WorkflowTask(role_id="missing", spec=TaskSpec(goal="calculate", execution_timeout_seconds=10, completion=[Completion(id="r", description="result visible")]))
    calls, events = [], []
    class Planner:
        async def propose(self, workflow, scheduler, facts, diagnostics):
            calls.append(copy.deepcopy(diagnostics))
            if len(calls) == 1:
                return PlanProposal(base_plan_revision=scheduler.revision, tasks=[task])
            corrected = task.model_copy(update={"role_id": "A"})
            return PlanProposal(base_plan_revision=scheduler.revision, tasks=[corrected])
    workflow = WorkflowSpec(goal="calculate", root=tmp_path, max_workflow_seconds=30, max_artifact_bytes=100000, roles=[RoleSpec(role_id="A", device_id="test")])
    session = MainSession(planner=Planner())
    session.workflow = workflow
    session.scheduler = Scheduler(workflow)
    session.facts = object()
    session.journal = SimpleNamespace(append=events.append)
    session.deadline = time.monotonic() + 10
    await session._replan()
    rejection = next(e for e in events if e["kind"] == "plan_rejected")
    assert "role" in rejection["error"].lower()
    assert rejection in calls[1]
    assert session.scheduler.tasks[task.task_id].role_id == "A"
    assert len(calls) == 2
