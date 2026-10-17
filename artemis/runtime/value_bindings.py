"""Case-local data provenance. Runtime values never belong to an export model."""

from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from third_party.mobile_use.utils.logger import get_logger

ScalarType = Literal["string", "integer", "number", "boolean"]
logger = get_logger(__name__)


class BindingError(ValueError):
    """A safe, value-free error suitable for execution feedback."""


class ValueRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ref: str = Field(min_length=1)


class CaseSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["case"] = "case"
    start: int
    end: int


class FixtureSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["fixture"] = "fixture"
    resource_id: str
    field: str


class ActionOutputSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["action"] = "action"
    action_id: str
    output_name: str


class ValueDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    value_type: ScalarType
    source: Annotated[CaseSource | FixtureSource | ActionOutputSource, Field(discriminator="kind")]
    persistence: Literal["literal", "reference"]
    allowed_roles: tuple[str, ...] = ()
    description: str = ""
    dependencies: tuple[str, ...] = ()


@dataclass(repr=False)
class _RuntimeValue:
    value: Any = field(repr=False)
    valid: bool = True
    confirmed: bool = True


def value_type(value: Any) -> ScalarType:
    # bool is an int subclass; do not accept it as an integer.
    scalar_types: dict[type, ScalarType] = {
        bool: "boolean",
        str: "string",
        int: "integer",
        float: "number",
    }
    name = scalar_types.get(type(value))
    if name is not None:
        return name
    raise BindingError("Only scalar business values can be bound")


def as_ref(value: Any) -> ValueRef | None:
    if isinstance(value, ValueRef):
        return value
    if isinstance(value, dict) and "ref" in value:
        try:
            return ValueRef.model_validate(value)
        except ValueError:
            raise BindingError("Malformed value reference") from None
    return None


class ValueBindings:
    """One instance per run, shared by its device contexts. No model_dump API."""

    def __init__(self, case: str = ""):
        self._case = case
        self._definitions: dict[str, ValueDefinition] = {}
        self._values: dict[str, _RuntimeValue] = {}
        self._resources: dict[str, dict] = {}
        self._operations: dict[str, dict] = {}
        self._lock = RLock()

    def register_operation(
        self, name: str, *, inputs: dict | None = None, literals: dict | None = None
    ):
        """Trusted adapter contract, never supplied as model-call arguments."""
        contract = {"name": name, "inputs": dict(inputs or {}), "literals": dict(literals or {})}
        kinds = {"string", "integer", "number", "boolean"}
        if set(contract["inputs"]) & set(contract["literals"]) or any(
            kind not in kinds
            for group in (contract["inputs"], contract["literals"])
            for kind in group.values()
        ):
            raise BindingError("Invalid operation input contract")
        with self._lock:
            previous = self._operations.get(name)
            if previous is not None and previous != contract:
                raise BindingError("Operation contract cannot change during a case")
            self._operations[name] = contract
            if previous is None:
                logger.debug(
                    "[bindings] operation.registered inputs={} literals={}".format(
                        len(contract["inputs"]),
                        len(contract["literals"]),
                    )
                )

    def operation(self, name: str) -> dict | None:
        with self._lock:
            return deepcopy(self._operations.get(name))

    def satisfies_requirements(self, role_id: str, requirements: dict) -> bool:
        """Registration attests that a trusted provider fulfilled this resource contract."""
        with self._lock:
            return any(
                resource["role_id"] == role_id
                and all(
                    key in resource["requirements"] and resource["requirements"][key] == expected
                    for key, expected in requirements.items()
                )
                for resource in self._resources.values()
            )

    def _register(self, definition: ValueDefinition, value: Any) -> ValueRef:
        if definition.id in self._definitions:
            raise BindingError("Value binding already exists; bindings are immutable")
        if value_type(value) != definition.value_type:
            raise BindingError("Value does not match its declared type")
        self._definitions[definition.id] = definition
        self._values[definition.id] = _RuntimeValue(deepcopy(value))
        logger.debug(
            f"[bindings] value.registered source={definition.source.kind} policy={definition.persistence} type={definition.value_type} roles={len(definition.allowed_roles)}"
        )
        return ValueRef(ref=definition.id)

    def register_case_literal(
        self,
        value_id: str,
        *,
        start: int,
        end: int,
        allowed_roles: tuple[str, ...] = (),
        description: str = "",
    ) -> ValueRef:
        if not 0 <= start < end <= len(self._case):
            raise BindingError("Invalid case source span")
        with self._lock:
            return self._register(
                ValueDefinition(
                    id=value_id,
                    value_type="string",
                    source=CaseSource(start=start, end=end),
                    persistence="literal",
                    allowed_roles=allowed_roles,
                    description=description,
                ),
                self._case[start:end],
            )

    def register_fixture(
        self,
        *,
        resource_id: str,
        role_id: str,
        provider: str,
        requirements: dict,
        values: dict[str, Any],
        field_roles: dict[str, tuple[str, ...]] | None = None,
    ) -> dict[str, ValueRef]:
        # Only the requirements/locator contract is exportable; values stay in memory.
        with self._lock:
            resource_key = f"{role_id}.{resource_id}"
            if resource_key in self._resources or not values:
                raise BindingError("Fixture must be nonempty and registered once")
            definitions = [
                ValueDefinition(
                    id=f"roles.{role_id}.{resource_id}.{name}",
                    value_type=value_type(value),
                    source=FixtureSource(resource_id=resource_id, field=name),
                    persistence="reference",
                    allowed_roles=(field_roles or {}).get(name, (role_id,)),
                    description=f"{role_id} {resource_id} {name}",
                )
                for name, value in values.items()
            ]
            if any(d.id in self._definitions for d in definitions):
                raise BindingError("Fixture binding already exists")
            refs = {
                name: self._register(definition, value)
                for definition, (name, value) in zip(definitions, values.items(), strict=True)
            }
            self._resources[resource_key] = deepcopy(
                {
                    "id": resource_id,
                    "role_id": role_id,
                    "provider": provider,
                    "requirements": requirements,
                }
            )
            logger.info(
                f"[bindings] fixture.registered role_id={role_id} fields={len(refs)} requirements={len(requirements)}"
            )
            return refs

    def register_case_text(self, name: str, *, text: str, occurrence: int = 0) -> ValueRef:
        if not text or occurrence < 0:
            raise BindingError("A nonempty exact case input is required")
        start = -1
        for _ in range(occurrence + 1):
            start = self._case.find(text, start + 1)
            if start < 0:
                raise BindingError("Input is not present in the original case")
        return self.register_case_literal(f"case.{name}", start=start, end=start + len(text))

    def publish_output(
        self,
        *,
        action_id: str,
        output_name: str,
        value: Any,
        allowed_roles: tuple[str, ...],
        dependencies: tuple[str, ...] = (),
        description: str = "",
        confirmed: bool = True,
    ) -> ValueRef:
        with self._lock:
            for dep in dependencies:
                self._ready(dep)
            ref = self._register(
                ValueDefinition(
                    id=f"actions.{action_id}.outputs.{output_name}",
                    value_type=value_type(value),
                    source=ActionOutputSource(action_id=action_id, output_name=output_name),
                    persistence="reference",
                    allowed_roles=allowed_roles,
                    dependencies=dependencies,
                    description=description,
                ),
                value,
            )
            self._values[ref.ref].confirmed = confirmed
            logger.debug(
                f"[bindings] output.published action_id={action_id} confirmed={confirmed} dependencies={len(dependencies)}"
            )
            return ref

    def _ready(self, value_id: str) -> ValueDefinition:
        definition = self._definitions.get(value_id)
        if definition is None:
            raise BindingError("Unknown value reference")
        if not self._values[value_id].valid:
            raise BindingError("Value reference is invalid")
        if not self._values[value_id].confirmed:
            raise BindingError("Value output is not confirmed")
        return definition

    def confirm_outputs(self, action_id: str):
        with self._lock:
            outputs = self.outputs_for(action_id)
            for value_id in outputs.values():
                if not self._values[value_id].valid:
                    raise BindingError("Action output is invalid")
                for dependency in self._definitions[value_id].dependencies:
                    self._ready(dependency)
            for value_id in outputs.values():
                self._values[value_id].confirmed = True
            if outputs:
                logger.debug(
                    f"[bindings] outputs.confirmed action_id={action_id} count={len(outputs)}"
                )

    def validate_ref(self, ref: ValueRef, *, expected_type: str, role_id: str | None):
        with self._lock:
            definition = self._ready(ref.ref)
            if definition.allowed_roles and role_id not in definition.allowed_roles:
                raise BindingError("Value reference is not accessible to this role")
            if definition.value_type != expected_type:
                raise BindingError("Value reference type mismatch")
            return definition

    def resolve(self, ref: ValueRef, *, expected_type: str, role_id: str | None) -> Any:
        with self._lock:
            self.validate_ref(ref, expected_type=expected_type, role_id=role_id)
            logger.debug(f"[bindings] value.resolved role_id={role_id} type={expected_type}")
            return deepcopy(self._values[ref.ref].value)

    def invalidate(self, value_ids: list[str]):
        with self._lock:
            invalid = set(value_ids)
            while True:
                expanded = invalid | {
                    d.id for d in self._definitions.values() if invalid.intersection(d.dependencies)
                }
                if expanded == invalid:
                    break
                invalid = expanded
            for value_id in invalid:
                if value_id in self._values:
                    self._values[value_id].valid = False
            logger.info(
                f"[bindings] values.invalidated requested={len(value_ids)} affected={len(invalid.intersection(self._values))}"
            )

    def outputs_for(self, action_id: str) -> dict[str, str]:
        with self._lock:
            return {
                d.source.output_name: d.id
                for d in self._definitions.values()
                if isinstance(d.source, ActionOutputSource) and d.source.action_id == action_id
            }

    def describe_for_role(self, role_id: str | None) -> list[dict]:
        with self._lock:
            return [
                {"ref": d.id, "type": d.value_type, "description": d.description}
                for d in self._definitions.values()
                if self._values[d.id].valid
                and self._values[d.id].confirmed
                and (not d.allowed_roles or role_id in d.allowed_roles)
            ]

    def export_definitions(self) -> dict:
        with self._lock:
            values = []
            for d in self._definitions.values():
                item = d.model_dump(mode="json")
                if d.persistence == "literal":
                    item["value"] = self._values[d.id].value
                values.append(item)
            return deepcopy(
                {
                    "resources": list(self._resources.values()),
                    "values": values,
                    "operations": list(self._operations.values()),
                }
            )

    def clear(self):
        with self._lock:
            count = len(self._values)
            self._values.clear()
            self._definitions.clear()
            self._resources.clear()
            self._operations.clear()
            self._case = ""
            logger.debug(f"[bindings] cleared values={count}")
