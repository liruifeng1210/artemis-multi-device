"""Task attribution shared by native sessions and optional orchestration."""

from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SESSION_PAUSE_PATH: ContextVar[Path | None] = ContextVar("session_pause_path", default=None)


@dataclass
class TaskScope:
    task_id: str
    attempt_id: str
    goal: str
    start_step_exclusive: int = 0
    end_step_inclusive: int | None = None
    scope_revision: int = 1

    def metadata(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "scope_revision": self.scope_revision,
        }


def execution_metadata(ctx: Any) -> dict[str, Any]:
    """Capture attribution synchronously, before a background write can outlive a task."""
    owner = getattr(ctx, "execution_owner", None)
    result = dict(owner) if isinstance(owner, dict) else {}
    device = getattr(getattr(ctx, "device", None), "device_id", None)
    if isinstance(device, str):
        result["device_id"] = device
    scope = getattr(ctx, "active_task", None)
    if isinstance(scope, TaskScope):
        result.update(scope.metadata())
    return result


def pause_path(default: Path, ctx: Any = None) -> Path:
    setup = getattr(ctx, "execution_setup", None)
    return getattr(setup, "pause_path", None) or SESSION_PAUSE_PATH.get() or default


def task_steps(ctx: Any) -> list[dict]:
    engine = getattr(ctx, "data_engine", None)
    if engine is None:
        return []
    scope = getattr(ctx, "active_task", None)
    if scope is None:
        return engine.get_agent_friendly_steps()
    end = scope.end_step_inclusive
    if end is None:
        end = getattr(engine, "current_step_number", 0)
    start = scope.start_step_exclusive + 1
    if end < start:
        return []
    steps = engine.get_agent_friendly_steps_in_range(start, end)
    expected = scope.metadata()
    for step in steps:
        metadata = step.get("extra_metadata") or {}
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Task step attribution does not match the active attempt")
    return steps
