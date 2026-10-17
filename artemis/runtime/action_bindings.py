"""Symbolic action arguments and replay validation over existing trace records."""

from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, get_args
from uuid import uuid4

from artemis.runtime.value_bindings import BindingError, ValueBindings, ValueRef, as_ref, value_type
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ActionIdentity:
    action_id: str
    attempt: int = 1
    input_refs: tuple[str, ...] = ()


@dataclass(repr=False)
class BoundInvocation:
    action_id: str
    arguments: dict = field(repr=False)


CURRENT_ACTION: ContextVar[ActionIdentity | None] = ContextVar("artemis_action", default=None)


def new_action_id() -> str:
    return str(uuid4())


def record_local_action(ctx, action_item: dict, status: str, step_id=None):
    """Client-side wait, intercepted and skipped actions do not reach ActionSession."""
    from artemis.mcp.action_names import to_canonical_call

    recorder = getattr(getattr(ctx, "data_engine", None), "record_action_binding", None)
    if not callable(recorder):
        return
    action_id = action_item.setdefault("action_id", new_action_id())
    try:
        name, args = to_canonical_call(action_item)
    except ValueError:
        name, args = action_item.get("action", "unknown"), {}
    recorder(
        action_id=action_id,
        attempt=action_item.get("attempt", 1),
        name=name,
        arguments=args,
        status=status,
        step_id=step_id,
    )


def resolve_arguments(
    name: str, args: dict, bindings: ValueBindings, role_id: str | None
) -> tuple[dict, bool]:
    from artemis.mcp.action_specs import ACTION_SPECS

    spec = ACTION_SPECS.get(name)
    contract = bindings.operation(name) or {}
    bindable = dict(spec.bindable) if spec else contract.get("inputs", {})
    result = deepcopy(args)
    protected = False
    for key, value in args.items():
        ref = as_ref(value)
        if ref is not None:
            if key not in bindable:
                raise BindingError("This action parameter does not accept references")
            definition = bindings.validate_ref(ref, expected_type=bindable[key], role_id=role_id)
            result[key] = bindings.resolve(ref, expected_type=bindable[key], role_id=role_id)
            protected |= definition.persistence == "reference"
        elif isinstance(value, (dict, list)) and _contains_ref(value):
            raise BindingError("Nested references are not supported by this parameter")
    return result, protected


def tool_binding_contract(tool) -> tuple[dict, dict]:
    """Only trusted tool declarations decide parameter types and literal policy."""
    metadata = tool.metadata or {}
    inputs = dict(metadata.get("artemis_bindings", {}))
    literals = dict(metadata.get("artemis_literals", {}))
    schema = getattr(tool, "args_schema", None)
    scalar_types = {str: "string", int: "integer", float: "number", bool: "boolean"}
    for name, model_field in getattr(schema, "model_fields", {}).items():
        members = get_args(model_field.annotation)
        kinds = [scalar_types[t] for t in members if isinstance(t, type) and t in scalar_types]
        if ValueRef in members and len(kinds) == 1:
            if name in inputs and inputs[name] != kinds[0]:
                raise BindingError("Tool input declarations disagree")
            inputs[name] = kinds[0]
    return inputs, literals


def input_references(arguments: dict) -> tuple[str, ...]:
    return tuple(ref.ref for value in arguments.values() if (ref := as_ref(value)) is not None)


def _safe_literal(spec, name: str, value: Any, custom: dict) -> bool:
    if spec is None:
        if name not in custom:
            return False
        try:
            return value_type(value) == custom[name]
        except BindingError:
            return False
    if name not in spec.replay_literals:
        return False
    if name == "key":
        from artemis.drivers.base import KeyCode

        key = str(value).removeprefix("KEYCODE_").upper()
        return (
            key in KeyCode.__members__
            or key in {"TAB", "SEARCH", "MENU"}
            or (key.isdecimal() and 0 <= int(key) <= 400)
        )
    if name == "action":
        return value in ("launch", "stop")
    if name == "wait_state":
        return value in ("appear", "disappear")
    from pydantic import TypeAdapter, ValidationError

    param = next((p for p in spec.wire.params if p.name == name), None) if spec.wire else None
    if param is None:
        return False
    try:
        TypeAdapter(param.annotation).validate_python(value, strict=True)
    except ValidationError:
        return False
    return True


def _contains_ref(value: Any) -> bool:
    if as_ref(value) is not None:
        return True
    if isinstance(value, dict):
        return any(_contains_ref(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_ref(v) for v in value)
    return False


def symbolic_arguments(args: dict) -> dict:
    result = {}
    for key, value in args.items():
        ref = as_ref(value)
        result[key] = ref.model_dump() if ref is not None else deepcopy(value)
    return result


def render_bindings_prompt(ctx) -> str:
    import json

    bindings = getattr(ctx, "value_bindings", None)
    if not isinstance(bindings, ValueBindings):
        return ""
    catalog = bindings.describe_for_role(getattr(ctx, "role_id", None))
    return (
        "Available business data (current turn only):\n"
        + json.dumps(catalog, ensure_ascii=False)
        + '\nUse {"ref":"<listed ref>"} for declared business inputs (including input_text.text '
        "and manage_app.app_name). "
        "Do not resolve references, copy runtime values into notes, or invent references. "
        "Binding policy is enforced by code. Invalid references require a new action."
        " For a fixed input explicitly specified in the case, call bind_case_input "
        "with its exact text first, then use the returned reference."
    )


def case_input_tool(ctx):
    from langchain_core.tools import tool

    @tool
    def bind_case_input(name: str, text: str, occurrence: int = 0) -> dict:
        """Bind an exact, fixed case input. Never use for values observed during execution.

        occurrence is the zero-based occurrence of text in the original case.
        Returns a reference whose fixed case value may be saved in the replay artifact.
        """
        return ctx.value_bindings.register_case_text(
            name,
            text=text,
            occurrence=occurrence,
        ).model_dump()

    return bind_case_input


def build_replay_definition(records: list[dict], definitions: dict) -> dict:
    """Project only declared action fields; validate the complete data flow."""
    from artemis.mcp.action_specs import ACTION_SPECS

    values = {d["id"]: d for d in definitions["values"]}
    operations = {d["name"]: d for d in definitions.get("operations", [])}
    available = {key for key, d in values.items() if d["source"]["kind"] != "action"}
    issues, actions = [], []
    seen = set()
    if not records:
        issues.append({"reason": "no_recorded_actions"})
    for record in records:
        action = deepcopy(record)
        action_id = action["action_id"]
        action_issues = []

        def issue(reason, parameter=None):
            item = {"action_id": action_id, "reason": reason}
            if parameter is not None:
                item["parameter"] = parameter
            action_issues.append(item)

        identity = (action_id, action["attempt"])
        if identity in seen:
            issue("duplicate_attempt")
        seen.add(identity)
        if action["status"] != "success":
            issue("unsuccessful_attempt")
        spec = ACTION_SPECS.get(action["name"])
        contract = operations.get(action["name"])
        supported = spec is not None or contract is not None
        bindable = dict(spec.bindable) if spec else (contract or {}).get("inputs", {})
        literals = (contract or {}).get("literals", {})
        if not supported:
            issue("unsupported_operation")
        args = {}
        declared = set(bindable) | set(spec.replay_literals if spec else literals)
        role = action.get("execution", {}).get("role_id")
        for key, value in action["arguments"].items():
            # Unknown field names are untrusted too; never copy them into artifacts/issues.
            if not supported:
                continue
            if key not in declared:
                issue("undeclared_parameter")
                continue
            ref = as_ref(value)
            reason = None
            if ref is not None:
                definition = values.get(ref.ref)
                if definition is None or ref.ref not in available:
                    reason = "unavailable_reference"
                elif key not in bindable or definition["value_type"] != bindable[key]:
                    reason = "reference_type_or_parameter_mismatch"
                elif definition["allowed_roles"] and role not in definition["allowed_roles"]:
                    reason = "reference_role_mismatch"
                elif definition["persistence"] == "literal":
                    if definition["source"]["kind"] != "case":
                        reason = "invalid_literal_source"
                    else:
                        args[key] = definition["value"]
                else:
                    args[key] = ref.model_dump()
            elif _contains_ref(value):
                reason = "unsupported_nested_reference"
            elif key in bindable:
                reason = "unproven_literal"
            elif _safe_literal(spec, key, value, literals):
                args[key] = deepcopy(value)
            else:
                reason = "undeclared_or_unsafe_literal"
            if reason:
                issue(reason, key)
                args[key] = {"unresolved": True}
        action["arguments"] = args
        pending = []
        for output, value_id in action.get("outputs", {}).items():
            definition = values.get(value_id)
            expected = {"kind": "action", "action_id": action_id, "output_name": output}
            if definition is None or definition["source"] != expected:
                issue("invalid_output")
                continue
            dependencies = definition.get("dependencies", [])
            if any(dep not in available for dep in dependencies):
                issue("unavailable_output_dependency")
            elif any(
                values[dep]["allowed_roles"] and role not in values[dep]["allowed_roles"]
                for dep in dependencies
            ):
                issue("output_dependency_role_mismatch")
            else:
                pending.append(value_id)
        if not action_issues:
            available.update(pending)
        issues.extend(action_issues)
        actions.append(action)
    safe_definitions = deepcopy(definitions)
    # Offline callers cannot upgrade fixture/output values into literal fields.
    for definition in safe_definitions["values"]:
        if definition["source"]["kind"] != "case" or definition["persistence"] != "literal":
            definition.pop("value", None)
    reasons = sorted({item["reason"] for item in issues})
    logger.info(
        "[bindings] replay.checked actions={} values={} ready={} issues={} reasons={}".format(
            len(actions),
            len(values),
            not issues,
            len(issues),
            ",".join(reasons),
        )
    )
    return {
        "schema_version": 1,
        "replay_ready": not issues,
        "issues": issues,
        "bindings": safe_definitions,
        "actions": actions,
    }


def build_replay_from_envelopes(envelopes: list[dict], definitions: dict | None = None) -> dict:
    """Also accepts persisted action_binding trace payloads after a run has ended."""
    envelopes = sorted(envelopes, key=lambda item: item.get("sequence", 0))
    if definitions is None:
        definitions = (
            envelopes[-1]["value_definitions"] if envelopes else {"resources": [], "values": []}
        )
    records = []
    for envelope in envelopes:
        record = deepcopy(envelope["action_binding"])
        record["execution"] = deepcopy(envelope.get("execution", {}))
        record["step_id"] = envelope.get("step_id")
        records.append(record)
    return build_replay_definition(records, definitions)
