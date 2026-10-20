"""Independent acceptance of the original task contract."""

from __future__ import annotations
import json
from typing import Annotated, Literal
from pydantic import (
    BeforeValidator,
    ConfigDict,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    TypeAdapter,
    create_model,
)


def output_model(schema):
    allowed = {"type", "properties", "required", "additionalProperties"}
    if (
        set(schema) - allowed
        or schema.get("type") != "object"
        or schema.get("additionalProperties", False) is not False
    ):
        raise ValueError("Unsupported output object schema")
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not isinstance(props, dict) or not required <= props.keys():
        raise ValueError("Invalid properties/required")
    fields = {}
    scalar = {
        "string": StrictStr,
        "integer": StrictInt,
        "number": StrictFloat,
        "boolean": StrictBool,
    }

    def field_type(field):
        if set(field) - {"type", "enum", "items", "description", "nullable"}:
            raise ValueError("Unsupported output field")
        kind = field.get("type")
        nullable = field.get("nullable", False)
        if isinstance(kind, list):
            if len(kind) != 2 or "null" not in kind:
                raise ValueError("Only nullable unions supported")
            nullable, kind = True, next(k for k in kind if k != "null")
        if kind == "array":
            item = field.get("items", {})
            if item.get("type") not in scalar:
                raise ValueError("Only scalar arrays supported")
            typ = list[field_type(item)]
        elif kind in scalar:
            typ = scalar[kind]
        else:
            raise ValueError("Unsupported output type")
        if "enum" in field:
            values = field["enum"]
            if not values or kind == "array":
                raise ValueError("Invalid enum")
            checker = create_model("EnumValue", value=(typ, ...))
            for value in values:
                checker(value=value)
            typ = Annotated[
                Literal[tuple(values)], BeforeValidator(TypeAdapter(typ).validate_python)
            ]
        return typ | None if nullable else typ

    for name, value in props.items():
        if not isinstance(name, str) or not name.isidentifier() or name.startswith("_"):
            raise ValueError("Invalid output field name")
        typ = field_type(value)
        fields[name] = (typ, ...) if name in required else (typ, None)
    return create_model(
        "ContractOutput", __config__=ConfigDict(extra="forbid", strict=True), **fields
    )


def validated_outputs(schema, output):
    model = output_model(schema)
    if hasattr(output, "model_dump"):
        output = output.model_dump(exclude_unset=True)
    if isinstance(output, str):
        output = json.loads(output)
    # Persistent execution returns the raw structured output; native test
    # summaries stay in run_outcome and cannot weaken this schema.
    return model.model_validate(output if output is not None else {}).model_dump(exclude_unset=True)


def render_goal(spec):
    return "\n".join(
        [
            spec.goal,
            "Inputs: " + json.dumps(spec.inputs, ensure_ascii=False),
            "Required contract:",
            *[f"[contract:{c.id}] {c.description}" for c in spec.completion],
            "Constraints:",
            *spec.constraints,
            "Effect policy: " + spec.effect_policy,
            *(
                ["Read-only task: observe and verify; do not modify device or business state."]
                if spec.effect_policy == "read_only"
                else []
            ),
            "Finish only this task, then wait for Main.",
        ]
    )


def accept_result(spec, result):
    validated_outputs(spec.outputs_schema, result.outputs)
    expected = {c.id for c in spec.completion if c.required}
    by_id = {}
    for check in result.checks:
        if check.id in by_id:
            raise ValueError("Duplicate result check")
        by_id[check.id] = check
    if result.status == "succeeded" and any(
        key not in by_id or by_id[key].verdict != "passed" or not by_id[key].evidence_refs
        for key in expected
    ):
        raise ValueError("Successful result lacks verified contract")


class ContractVerifier:
    def __init__(self, spec, store):
        self.spec, self.store = spec, store
        self.result = None

    async def verify(self, ctx, native):
        import asyncio
        from artemis.agents.checker.checker import run_final_check
        from artemis.utils.plan_grammar import CheckItem
        from artemis.runtime.session_orchestration.artifacts import atomic_json

        items = [
            CheckItem(
                kind="verify",
                when="at_end",
                text=f"[contract:{c.id}] {c.description}",
                parent_key=None,
            )
            for c in self.spec.completion
        ]
        outputs = validated_outputs(self.spec.outputs_schema, native.output)
        plan = ctx.data_engine.base_dir / "notes" / "task_plan.md"
        text = plan.read_text(encoding="utf-8") if plan.exists() else ""
        report = await asyncio.wait_for(
            run_final_check(
                ctx,
                render_goal(self.spec) + "\nExtracted output: " + json.dumps(outputs),
                text,
                [],
                items,
                attempt_id=ctx.active_task.attempt_id,
                review_id="contract",
            ),
            self.spec.verification_timeout_seconds,
        )
        atomic_json(self.store.path / "contract_report.json", report.model_dump())
        return {
            "outputs": outputs,
            "report": report.model_dump(),
            "evidence_refs": getattr(ctx, "contract_evidence", []),
        }

    async def persist(self, ctx, native):
        from artemis.runtime.session_orchestration.models import CheckResult, TaskResult

        bundle = native.completion_data or {}
        verdicts = bundle.get("report", {}).get("verdicts", [])
        checks = []
        reference = str((self.store.path / "contract_report.json").relative_to(self.store.root))
        for check in self.spec.completion:
            text = f"[contract:{check.id}] {check.description}"
            matches = [v for v in verdicts if v["item_text"] == text]
            verdict = (
                matches[0]["status"]
                if len(matches) == 1
                else ("unchecked" if not matches else "inconclusive")
            )
            checks.append(
                CheckResult(
                    id=check.id,
                    verdict=verdict,
                    evidence=matches[0]["evidence"] if len(matches) == 1 else "",
                    evidence_refs=[reference, *bundle.get("evidence_refs", [])] if matches else [],
                )
            )
        required = {c.id for c in self.spec.completion if c.required}
        passed = all(c.verdict == "passed" for c in checks if c.id in required)
        status = (
            "cancelled"
            if native.cancelled
            else "failed"
            if native.execution_error
            else "succeeded"
            if passed
            else "failed"
            if any(c.verdict == "failed" for c in checks if c.id in required)
            else "blocked"
        )
        scope = ctx.active_task
        self.result = TaskResult(
            status=status,
            outputs=bundle.get("outputs", {}),
            checks=checks,
            session_reusable=native.session_reusable,
            side_effect_state="uncertain"
            if native.cancelled or native.execution_error
            else "confirmed",
            step_range=(
                {"first": scope.start_step_exclusive + 1, "last": scope.end_step_inclusive}
                if scope.end_step_inclusive > scope.start_step_exclusive
                else None
            ),
            artifact_refs=[reference, *bundle.get("evidence_refs", [])] if verdicts else [],
            error={"code": "execution_failed", "description": native.execution_error}
            if native.execution_error
            else None,
        )
        return self.store.finish(
            self.result.model_dump(mode="json"), getattr(ctx, "task_artifact_paths", set())
        )
