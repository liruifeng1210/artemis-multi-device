"""Atomic files written by the in-process multi-device runner."""

import json
from pathlib import Path
from uuid import uuid4


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    pending.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)
