"""Execution provenance, captured at asynchronous hand-off boundaries.

The legacy global Engine remains available to one-shot callers. Managed Role
Sessions never publish themselves there: an unowned log cannot select a Role.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExecutionScope:
    engine: Any = None
    role_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    device_id: str | None = None
    step_id: Any = None
    phase: str = ""
    workspace: Path | None = None
    control: Any = None

    def metadata(self) -> dict:
        return {
            name: value
            for name in ("role_id", "session_id", "task_id", "attempt_id", "device_id")
            if (value := getattr(self, name)) is not None
        }


CURRENT_EXECUTION: ContextVar[ExecutionScope | None] = ContextVar("artemis_execution", default=None)


@contextmanager
def execution_scope(scope: ExecutionScope | None):
    token = CURRENT_EXECUTION.set(scope)
    try:
        yield scope
    finally:
        CURRENT_EXECUTION.reset(token)


def set_scoped_step(step_id):
    scope = CURRENT_EXECUTION.get()
    if scope is not None:
        CURRENT_EXECUTION.set(replace(scope, step_id=step_id))


def get_current_data_engine():
    scope = CURRENT_EXECUTION.get()
    if scope is not None:
        return scope.engine
    import sys

    return getattr(sys.modules.get("artemis.data_engine.engine"), "_CURRENT_DATA_ENGINE", None)


def current_pause_file():
    scope = CURRENT_EXECUTION.get()
    if scope is not None and scope.workspace is not None:
        return scope.workspace / ".paused"
    from artemis.config import PAUSE_FILE

    return PAUSE_FILE


def validate_device_scope(ctx):
    control = getattr(ctx, "task_control", None)
    if control is not None:
        control.validate(CURRENT_EXECUTION.get(), ctx.device.device_id)
