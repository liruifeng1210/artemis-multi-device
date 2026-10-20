"""Read-only ownership audit of a calculator_roles evidence directory."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3


def audit(output, database):
    sessions = json.loads((output / "sessions.json").read_text(encoding="utf-8"))
    results_file = output / "results.json"
    results = json.loads(results_file.read_text(encoding="utf-8")) if results_file.exists() else []
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    errors, report = [], {}
    for role, info in sessions.items():
        sid = info["session_id"]
        expected = {"role_id": role, "session_id": sid, "device_id": info["device"]}
        steps = {
            row[0]: json.loads(row[1] or "{}")
            for row in connection.execute(
                "SELECT step_id, extra_metadata FROM steps WHERE session_id=?", (sid,)
            )
        }
        traces = list(
            connection.execute(
                "SELECT trace_id, step_id, parent_trace_id, type, status, payload "
                "FROM traces WHERE session_id=?",
                (sid,),
            )
        )
        counts = Counter()
        running = []
        for trace_id, step, parent, kind, status, raw in traces:
            counts[kind] += 1
            meta = json.loads(raw or "{}").get("execution", {})
            if any(meta.get(key) != value for key, value in expected.items()):
                errors.append([role, trace_id, "missing or incorrect trace owner"])
            if meta.get("task_id") and not meta.get("attempt_id"):
                errors.append([role, trace_id, "missing attempt"])
            if step:
                linked = connection.execute(
                    "SELECT session_id FROM steps WHERE step_id=?", (step,)
                ).fetchone()
                if linked and linked[0] != sid:
                    errors.append([role, trace_id, "foreign step"])
                if (
                    step in steps
                    and meta.get("task_id")
                    and (meta.get("task_id"), meta.get("attempt_id"))
                    != (steps[step].get("task_id"), steps[step].get("attempt_id"))
                ):
                    errors.append([role, trace_id, "foreign task step"])
            if parent:
                linked = connection.execute(
                    "SELECT session_id FROM traces WHERE trace_id=?", (parent,)
                ).fetchone()
                if linked and linked[0] != sid:
                    errors.append([role, trace_id, "foreign parent trace"])
            if status == "running":
                running.append(trace_id)
        for step, meta in steps.items():
            if any(meta.get(key) != value for key, value in expected.items()) or not meta.get(
                "task_id"
            ):
                errors.append([role, step, "incorrect step owner"])
        assignments = [r for r in results if r["role_id"] == role]
        for result in assignments:
            if result["session_id"] != sid:
                errors.append([role, result["task_id"], "session changed"])
            folder = Path(result["workspace"])
            manifest = json.loads((folder / "task_manifest.json").read_text(encoding="utf-8"))
            for key in ("role_id", "session_id", "task_id", "attempt_id"):
                if manifest.get(key) != result[key]:
                    errors.append([role, result["task_id"], "manifest owner", key])
        report[role] = {
            **info,
            "steps": len(steps),
            "trace_counts": dict(counts),
            "running_traces": running,
            "tasks": [
                {
                    k: r[k]
                    for k in (
                        "round",
                        "task_id",
                        "attempt_id",
                        "status",
                        "reusable",
                        "cleanup_errors",
                    )
                }
                for r in assignments
            ],
        }
    connection.close()
    return {"roles": report, "ownership_errors": errors}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--database", type=Path, default=Path("traces/data_engine.db"))
    args = parser.parse_args()
    result = audit(args.output, args.database)
    (args.output / "ownership-audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result["ownership_errors"]))
