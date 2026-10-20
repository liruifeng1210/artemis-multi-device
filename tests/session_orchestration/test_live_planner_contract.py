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
    workflow = SimpleNamespace(goal="calculate", constraints=[], roles=[RoleSpec(role_id="A",device_id="test")])
    scheduler = SimpleNamespace(revision=1,tasks={},results={},invalidated=set(),recoveries={})
    facts = SimpleNamespace(versions=[], sources={})
    await MainPlanner().propose(workflow, scheduler, facts)
    prompt = captured[0].content
    advertised = json.loads(prompt.split("The exact required JSON Schema follows; use its field names and nesting:\n")[1])
    assert advertised == PlanProposal.model_json_schema()
    assert "base_plan_revision" in advertised["required"]
    assert "spec" in advertised["$defs"]["WorkflowTask"]["required"]
