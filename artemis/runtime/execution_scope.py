"""Task-local provenance for device execution and asynchronous evidence."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class ExecutionScope:
    engine: Any = None
    runtime: Any = None
    role_id: str | None = None
    device_id: str | None = None
    subgoal_hash: str | None = None
    turn_id: str | None = None
    step_id: Any = None
    phase: str = ""

    def metadata(self) -> dict:
        return {
            k: v
            for k, v in {
                "role_id": self.role_id,
                "device_id": self.device_id,
                "subgoal_hash": self.subgoal_hash,
                "turn_id": self.turn_id,
            }.items()
            if v is not None
        }


CURRENT_EXECUTION: ContextVar[ExecutionScope | None] = ContextVar("artemis_execution", default=None)


@contextmanager
def execution_scope(scope: ExecutionScope):
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

    module = sys.modules.get("artemis.data_engine.engine")
    return getattr(module, "_CURRENT_DATA_ENGINE", None)
