"""Task files live below a stable native Session storage directory."""

import json
import re
from pathlib import Path

from pydantic import BaseModel

from artemis.runtime.execution_scope import CURRENT_EXECUTION


def safe_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Identifiers must contain 1-128 letters, digits, underscores or hyphens")
    return value


def task_base_dir(base_dir: str | Path) -> Path:
    """Only redirect the active Engine root; explicit archive paths stay stable."""
    scope = CURRENT_EXECUTION.get()
    if scope is not None and scope.workspace is not None and scope.engine is not None:
        if Path(base_dir) == Path(scope.engine.base_dir):
            return scope.workspace
    return Path(base_dir)


def write_manifest(path: Path, data: dict) -> None:
    def serialize(value):
        return value.model_dump(mode="json") if isinstance(value, BaseModel) else str(value)

    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".pending")
    pending.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=serialize), encoding="utf-8"
    )
    pending.replace(path)
