"""Validated wire contracts. No SDK/device imports."""

from __future__ import annotations
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def new_id():
    return str(uuid4())


def now():
    return datetime.now(UTC).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Completion(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str = Field(min_length=1)
    required: bool = True


class TaskSpec(Model):
    goal: str = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    input_sources: dict[str, dict] = Field(default_factory=dict)
    completion: list[Completion] = Field(min_length=1)
    outputs_schema: dict = Field(
        default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False}
    )
    constraints: list[str] = Field(default_factory=list)
    effect_policy: Literal["read_only", "retry_safe", "verify_before_retry"] = "verify_before_retry"
    execution_timeout_seconds: float = Field(gt=0, le=86400)
    verification_timeout_seconds: float = Field(default=60, gt=0, le=600)
    settlement_timeout_seconds: float = Field(default=30, gt=0, le=600)
    max_steps: int = Field(default=30, gt=0, le=10000)

    @model_validator(mode="after")
    def valid_checks(self):
        ids = [c.id for c in self.completion]
        if len(ids) != len(set(ids)) or not any(c.required for c in self.completion):
            raise ValueError("Unique check IDs and a required check are mandatory")
        from artemis.runtime.session_orchestration.contract import output_model

        output_model(self.outputs_schema)
        return self


class CheckResult(Model):
    id: str
    verdict: Literal["passed", "failed", "inconclusive", "unchecked"]
    evidence_refs: list[str] = Field(default_factory=list)
    evidence: str = ""


class TaskResult(Model):
    status: Literal["succeeded", "failed", "blocked", "cancelled", "unknown"]
    outputs: dict = Field(default_factory=dict)
    checks: list[CheckResult] = Field(default_factory=list)
    observations: list[dict] = Field(default_factory=list)
    blocker: dict | None = None
    suggested_next_actions: list[dict] = Field(default_factory=list)
    observed_at: str = Field(default_factory=now)
    step_range: dict | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    side_effect_state: Literal["none", "confirmed", "uncertain"] = "uncertain"
    session_reusable: bool = False
    error: dict | None = None

    @field_validator("observed_at")
    @classmethod
    def valid_timestamp(cls, value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or (parsed - datetime.now(UTC)).total_seconds() > 10:
            raise ValueError("Observation requires a timezone and cannot be in the future")
        return value


class Envelope(Model):
    protocol_version: Literal[1] = 1
    kind: Literal[
        "hello",
        "ready",
        "execute",
        "accepted",
        "rejected",
        "started",
        "progress",
        "needs_input",
        "result",
        "ack",
        "cancel_task",
        "cancel_received",
        "observe_session",
        "inspect_session",
        "observation",
        "inspection",
        "close_session",
        "heartbeat",
        "session_status",
        "closed",
        "error",
    ]
    message_id: str = Field(default_factory=new_id)
    in_reply_to: str | None = None
    sequence: int = Field(ge=0)
    workflow_id: str
    role_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    child_session_id: str
    worker_epoch: str
    task_id: str | None = None
    attempt_id: str | None = None
    payload: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def identity(self):
        for value in (
            self.message_id,
            self.workflow_id,
            self.child_session_id,
            self.worker_epoch,
            self.task_id,
            self.attempt_id,
            self.in_reply_to,
        ):
            if value is not None:
                UUID(value)
        if (self.task_id is None) != (self.attempt_id is None):
            raise ValueError("Task and attempt IDs must appear together")
        if (
            self.kind in ("execute", "accepted", "started", "result", "cancel_task")
            and self.task_id is None
        ):
            raise ValueError("Task identity required")
        return self


class RoleSpec(Model):
    role_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    device_id: str = Field(min_length=1)
    description: str = ""
    profile: Literal["pro", "default"] = "pro"
    app_path: Path | None = None
    locked_app_package: str | None = None


class Dependency(Model):
    task_id: str
    statuses: list[Literal["succeeded", "failed", "blocked", "cancelled", "unknown"]] = Field(
        default_factory=lambda: ["succeeded"], min_length=1
    )
    check_id: str | None = None
    verdict: Literal["passed", "failed", "inconclusive", "unchecked"] = "passed"
    output_key: str | None = None
    equals: Any = None
    max_age_seconds: float | None = Field(default=None, gt=0)


class WorkflowTask(Model):
    task_id: str = Field(default_factory=new_id)
    display_name: str = ""
    role_id: str
    spec: TaskSpec
    depends_on: list[Dependency] = Field(default_factory=list)
    dependency_mode: Literal["all", "any"] = "all"
    expected_terminal_statuses: list[Literal["succeeded", "cancelled"]] = Field(
        default_factory=lambda: ["succeeded"], min_length=1
    )

    @model_validator(mode="after")
    def valid_id(self):
        UUID(self.task_id)
        return self


class ExpectedCancellation(Model):
    role_id: str
    display_name: str = Field(min_length=1)


class WorkflowSpec(Model):
    workflow_id: str = Field(default_factory=new_id)
    goal: str = Field(min_length=1)
    roles: list[RoleSpec] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    tasks: list[WorkflowTask] = Field(default_factory=list)
    expected_cancellations: list[ExpectedCancellation] = Field(default_factory=list)
    root: Path
    max_workflow_seconds: float = Field(gt=0)
    max_artifact_bytes: int = Field(gt=0)
    max_attempts_per_task: int = Field(default=2, ge=1, le=10)
    startup_timeout_seconds: float = Field(default=120, gt=0)
    heartbeat_interval_seconds: float = Field(default=2, gt=0)
    heartbeat_timeout_seconds: float = Field(default=10, gt=0)
    reconnect_grace_seconds: float = Field(default=15, gt=0)
    close_timeout_seconds: float = Field(default=45, gt=0)
    fake_worker: bool = False

    @field_validator("root")
    @classmethod
    def normalize_root(cls, value):
        from artemis.runtime.session_orchestration.paths import absolute_path

        return absolute_path(value)

    @model_validator(mode="after")
    def valid_roles(self):
        UUID(self.workflow_id)
        if len({r.role_id for r in self.roles}) != len(self.roles):
            raise ValueError("Duplicate role")
        if len({r.device_id for r in self.roles}) != len(self.roles):
            raise ValueError("Duplicate device")
        return self


class DiagnosticRequest(Model):
    role_id: str
    kind: Literal["inspect", "observe"] = "inspect"
    query: dict = Field(default_factory=dict)


class Recovery(Model):
    failed_task_id: str
    replacement_task_ids: list[str] = Field(min_length=1)


class PlanProposal(Model):
    base_plan_revision: int = Field(
        ge=0, description="Copy the current plan revision exactly; this is not the next revision."
    )
    tasks: list[WorkflowTask] = Field(default_factory=list)
    finish: bool = False
    missing_input: str | None = None
    diagnostics: list[DiagnosticRequest] = Field(default_factory=list, max_length=4)
    recoveries: list[Recovery] = Field(default_factory=list)
    restart_roles: list[str] = Field(default_factory=list, max_length=4)
