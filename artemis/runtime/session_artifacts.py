"""Crash-detectable per-attempt artifacts; all paths stay inside a session root."""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID
from datetime import datetime, UTC
from artemis.runtime.session_paths import absolute_path


def now():
    return datetime.now(UTC).isoformat()


ACTIVE_FILES = (
    "notes/task_plan.md",
    "notes/subgoal_hash_chain.json",
    "notes/output.md",
    "check_ledger.jsonl",
    "check_streams.jsonl",
    "run_outcome.json",
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def contained(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Artifact escapes root")
    return path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class AttemptStore:
    def __init__(self, root, task_id, attempt_id):
        UUID(task_id)
        UUID(attempt_id)
        self.root = absolute_path(root).resolve()
        self.path = contained(self.root, f"tasks/{task_id}/{attempt_id}")
        self.path.mkdir(parents=True, exist_ok=True)
        self.task_id, self.attempt_id = task_id, attempt_id

    def accept(self, request):
        path = self.path / "accepted.json"
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved["request_hash"] != digest(request):
                raise ValueError("attempt_payload_conflict")
            return False
        atomic_json(self.path / "request.json", request)
        atomic_json(path, {"request_hash": digest(request), "accepted_at": now()})
        return True

    def saved_result(self):
        path = self.path / "result.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def archive(self, extra=()):
        manifest_path = self.path / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest["archive_state"] == "ACTIVE_CLEARED":
                return manifest
        files = list(dict.fromkeys((*ACTIVE_FILES, *sorted(extra))))
        manifest = {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "archive_state": "PREPARED",
            "files": {},
        }
        atomic_json(manifest_path, manifest)
        for relative in files:
            source = contained(self.root, relative)
            if not source.exists():
                continue
            target = contained(self.path / "native", relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = source.read_bytes()
            with target.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            manifest["files"][relative] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        manifest["archive_state"] = "SNAPSHOT_WRITTEN"
        atomic_json(manifest_path, manifest)
        for relative, info in manifest["files"].items():
            if (
                hashlib.sha256(contained(self.path / "native", relative).read_bytes()).hexdigest()
                != info["sha256"]
            ):
                raise RuntimeError("Archive verification failed")
        manifest["archive_state"] = "COMMITTED"
        atomic_json(manifest_path, manifest)
        for relative in manifest["files"]:
            contained(self.root, relative).unlink(missing_ok=True)
        manifest["archive_state"] = "ACTIVE_CLEARED"
        atomic_json(manifest_path, manifest)
        return manifest

    def finish(self, result, extra=()):
        manifest = self.archive(extra)
        manifest["artifacts"] = {}
        for artifact in self.path.rglob("*"):
            if artifact.is_file() and artifact.name not in ("manifest.json", "result.json"):
                content = artifact.read_bytes()
                manifest["artifacts"][str(artifact.relative_to(self.root))] = {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
        manifest["result_hash"] = digest(result)
        atomic_json(self.path / "manifest.json", manifest)
        atomic_json(self.path / "result.json", result)
        return str(self.path / "result.json")


def validate_evidence(root, task_id, attempt_id, references, result):
    store = AttemptStore(root, task_id, attempt_id)
    manifest = json.loads((store.path / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("result_hash") != digest(result):
        raise ValueError("Result does not match committed manifest")
    for relative in references:
        metadata = manifest.get("artifacts", {}).get(relative)
        path = contained(root, relative)
        if metadata is None or not path.is_relative_to(store.path) or not path.is_file():
            raise ValueError("Evidence is outside the committed attempt")
        data = path.read_bytes()
        if (
            len(data) != metadata["size_bytes"]
            or hashlib.sha256(data).hexdigest() != metadata["sha256"]
        ):
            raise ValueError("Evidence content changed")


def require_clean_boundary(root):
    for path in Path(root).glob("tasks/*/*/manifest.json"):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["archive_state"] != "ACTIVE_CLEARED":
            raise RuntimeError(f"Incomplete task archive: {path}")
