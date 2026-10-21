"""Main sees the same limited output-schema language that the validator accepts."""

import json

import pytest

from artemis.runtime.session_orchestration.contract import output_model, validated_outputs
from artemis.runtime.session_orchestration.models import OUTPUT_SCHEMA_DESCRIPTION, PlanProposal


def test_advertised_example_is_a_valid_strict_output_contract():
    example = json.loads(OUTPUT_SCHEMA_DESCRIPTION.split("Example: ", 1)[1])
    assert validated_outputs(example, {"result": "001234"}) == {"result": "001234"}
    for invalid in ({"result": 1234}, {"result": "8", "unexpected": True}, {}):
        with pytest.raises(ValueError):
            validated_outputs(example, invalid)
    advertised = PlanProposal.model_json_schema()["$defs"]["TaskSpec"]["properties"]["outputs_schema"]
    assert advertised["description"] == OUTPUT_SCHEMA_DESCRIPTION


@pytest.mark.parametrize("schema,message", [
    ({"type": "object", "description": "SENSITIVE_SCHEMA_VALUE"}, "Unsupported output object schema keys: description"),
    ({"type": "object", "title": "SENSITIVE_SCHEMA_VALUE", "$defs": {"secret": "SENSITIVE_SCHEMA_VALUE"}}, "Unsupported output object schema keys: $defs, title"),
    ({"type": "SENSITIVE_SCHEMA_VALUE"}, "Output schema root type must be 'object'"),
    ({"type": "object", "additionalProperties": {"description": "SENSITIVE_SCHEMA_VALUE"}}, "Output schema additionalProperties must be false"),
])
def test_schema_rejection_names_the_unsupported_rule_without_echoing_values(schema, message):
    with pytest.raises(ValueError) as error:
        output_model(schema)
    assert str(error.value) == message
    assert "SENSITIVE_SCHEMA_VALUE" not in str(error.value)


def test_documented_scalar_nullable_and_array_subset_preserves_validation():
    schema = {"type": "object", "properties": {
        "text": {"type": "string", "enum": ["08", "09"], "description": "identifier"},
        "count": {"type": ["integer", "null"]},
        "ratio": {"type": "number", "nullable": True},
        "enabled": {"type": "boolean"},
        "labels": {"type": "array", "items": {"type": "string"}},
    }, "required": ["text", "count", "ratio", "enabled", "labels"], "additionalProperties": False}
    valid = {"text": "08", "count": None, "ratio": None, "enabled": True, "labels": ["a"]}
    assert validated_outputs(schema, valid) == valid
    with pytest.raises(ValueError):
        validated_outputs(schema, {**valid, "enabled": "true"})
    # Omitted additionalProperties remains the existing strict, closed policy.
    with pytest.raises(ValueError):
        validated_outputs({"type": "object"}, {"unexpected": "value"})


@pytest.mark.asyncio
async def test_main_prompt_includes_the_restricted_output_contract(monkeypatch):
    from types import SimpleNamespace
    from artemis.llm.router import ModelFactory
    from artemis.runtime.session_orchestration.planner import MainPlanner
    from artemis.services import llm
    captured = []
    async def complete(model, messages, *, schema):
        captured.extend(messages)
        return schema(base_plan_revision=1, missing_input="test only")
    monkeypatch.setattr(ModelFactory, "get_model", lambda endpoint: object())
    monkeypatch.setattr(llm, "acomplete_structured", complete)
    workflow = SimpleNamespace(goal="calculate", constraints=[], expected_cancellations=[], roles=[])
    scheduler = SimpleNamespace(revision=1, tasks={}, results={}, invalidated=set(), recoveries={})
    facts = SimpleNamespace(versions=[], sources={})
    await MainPlanner().propose(workflow, scheduler, facts)
    assert OUTPUT_SCHEMA_DESCRIPTION in captured[0].content
