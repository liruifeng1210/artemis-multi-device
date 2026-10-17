"""Public configuration for an internal App's multi-device test case."""

from typing import Any
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RoleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    case_actor: str = Field(min_length=1)
    description: str = ""
    feature_requirements: dict[str, Any] | None = None


class RolePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[RoleSpec] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_roles(self):
        ids = [r.id for r in self.roles]
        if len(set(ids)) != len(ids):
            raise ValueError("Role IDs must be unique")
        return self


class DeviceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    serial: str = Field(min_length=1)
    account_ref: str | None = None
    # Verified by the caller's fixture; model output never asserts these as applied.
    prepared_features: dict[str, Any] = Field(default_factory=dict)


class AppProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
    environment: str = ""
    instructions: str = ""
    apk_path: str | None = None


class MultiDeviceRequest(BaseModel):
    """Serializable request shared by SDK, CLI and HTTP/MCP entrypoints."""

    model_config = ConfigDict(extra="forbid")
    case: str = Field(min_length=1)
    role_bindings: dict[str, DeviceSpec] = Field(default_factory=dict)
    preferred_device: str | None = None
    output_description: str | None = None
    app_profile: AppProfile | None = None
    role_plan: RolePlan | None = None
    feature_catalog: dict[str, Any] = Field(default_factory=dict)
    max_turns: int = Field(default=200, ge=1)
    timeout_seconds: float = Field(default=1800, gt=0)
    lease_timeout_seconds: float = Field(default=60, ge=0)
    record_video: bool = True

    @model_validator(mode="after")
    def devices_are_distinct(self):
        if not self.case.strip():
            raise ValueError("The case cannot be blank")
        if any(not re.fullmatch(r"[a-z][a-z0-9_]*", role) for role in self.role_bindings):
            raise ValueError("Role binding IDs must use lowercase letters, numbers and underscores")
        serials = [d.serial.strip() for d in self.role_bindings.values()]
        if any(not s for s in serials) or len(set(serials)) != len(serials):
            raise ValueError("Every role must bind a distinct, nonempty device serial")
        return self
