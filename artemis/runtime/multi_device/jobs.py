"""Process-isolated case jobs shared by HTTP and MCP entrypoints."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import UUID, uuid4

from artemis.config import settings
from artemis.runtime.cancel_requests import request_cancel
from artemis.runtime.multi_device.models import MultiDeviceRequest

TERMINAL = frozenset({"completed", "failed", "cancelled", "inconclusive"})


def case_directory(run_id: str) -> Path:
    # Public IDs cannot address arbitrary files below or outside the trace store.
    return Path(settings.TRACES_PATH) / str(UUID(run_id))


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    pending.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def submit_case(request: MultiDeviceRequest | dict) -> dict:
    request = MultiDeviceRequest.model_validate(request)
    if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
        raise ValueError("Multi-device cases require directly addressable ADB devices")
    run_id = str(uuid4())
    directory = case_directory(run_id)
    directory.mkdir(parents=True)
    request_path = directory / "case_request.json"
    request_path.write_text(request.model_dump_json(indent=2), encoding="utf-8")
    job = {"run_id": run_id, "profile": "pro", "status": "starting", "created_at": time.time()}
    write_json(directory / "case_job.json", job)
    try:
        with (directory / "case_worker.log").open("ab") as log:
            worker = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "artemis",
                    "run-case",
                    str(request_path),
                    "--session-id",
                    run_id,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env={**os.environ, "PYTHONUNBUFFERED": "1", "ARTEMIS_TASK_INGRESS": "multi_device"},
            )
        import psutil

        try:
            created_at = psutil.Process(worker.pid).create_time()
        except psutil.Error:
            created_at = None
        job.update(pid=worker.pid, process_created_at=created_at)
        write_json(directory / "case_job.json", job)
    except Exception as exc:
        job.update(status="failed", error=f"Worker launch failed: {exc}")
        write_json(directory / "case_result.json", job)
        return job

    def reap():
        code = worker.wait()
        # The SDK persists its result even on cancellation. Cover import/startup failures too.
        if not read_json(directory / "case_result.json"):
            write_json(
                directory / "case_result.json",
                {
                    **job,
                    "status": "failed",
                    "error": f"Case worker exited without a result (exit {code})",
                },
            )

    threading.Thread(target=reap, name=f"case-{run_id}", daemon=True).start()
    return job


def get_case(run_id: str) -> dict:
    directory = case_directory(run_id)
    job = read_json(directory / "case_job.json")
    result = read_json(directory / "case_result.json")
    progress = read_json(directory / "case_progress.json")
    if not any((job, result, progress)):
        raise FileNotFoundError(f"Unknown case {run_id}")
    data = {**job, **progress, **result, "run_id": str(UUID(run_id))}
    manifest = read_json(directory / "multi_device.json")
    data.update({k: v for k, v in manifest.items() if k not in data})
    plan_path = directory / "notes" / "task_plan.md"
    if plan_path.exists():
        data["task_plan"] = plan_path.read_text(encoding="utf-8")
    if data.get("status") not in TERMINAL and job.get("pid") and job.get("process_created_at"):
        import psutil

        try:
            process = psutil.Process(job["pid"])
            alive = (
                abs(process.create_time() - job["process_created_at"]) < 1 and process.is_running()
            )
        except psutil.Error:
            alive = False
        if not alive:
            data.update(status="failed", error="Case worker is no longer running")
    if (directory / "case_cancel.json").exists() and data.get("status") not in TERMINAL:
        data["status"] = "cancelling"
    return data


def cancel_case(run_id: str) -> dict:
    data = get_case(run_id)
    if data.get("status") in TERMINAL:
        return data
    markers = request_cancel(session_id=run_id, reason="Multi-device case cancelled")
    if not markers:
        raise OSError("Could not deliver the case cancellation request")
    write_json(case_directory(run_id) / "case_cancel.json", {"requested_at": time.time()})
    return {**data, "status": "cancelling"}


def case_video(run_id: str, role_id: str) -> Path:
    directory = case_directory(run_id).resolve()
    value = get_case(run_id).get("videos", {}).get(role_id)
    if not value:
        raise FileNotFoundError(f"No recording for role {role_id}")
    path = Path(value).resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise FileNotFoundError("Recording is not available in this case")
    return path
