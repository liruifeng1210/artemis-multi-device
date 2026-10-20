"""Role-wide working context, separate from each assignment's execution state."""

import json
from pathlib import Path

from langchain_core.messages import HumanMessage


class SessionContinuity:
    def __init__(self, root: Path, goal: str = ""):
        self.root = root
        self.goal = goal
        self.current = None
        self.records = {}

    def begin(self, task_id, attempt_id, goal):
        self.current = {"task_id": task_id, "attempt_id": attempt_id, "goal": goal}

    def finish(self, result, goal):
        plan_path = result.workspace / "notes" / "task_plan.md"
        self.records[result.attempt_id] = {
            "task_id": result.task_id,
            "attempt_id": result.attempt_id,
            "goal": goal,
            "status": result.status,
            "reported_output": result.output,
            "historical_plan": plan_path.read_text(encoding="utf-8")
            if plan_path.exists()
            else None,
            "error": result.error,
            "reusable": result.reusable,
            "cleanup_errors": result.cleanup_errors,
            "workspace": str(result.workspace),
        }
        # Full assignment records remain available through native read_note;
        # only a bounded recent window is repeated in every model prompt.
        path = self.root / "notes" / "session_progress.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_suffix(".pending")
        pending.write_text(
            "# Assignment history (runtime records)\n\n"
            + json.dumps(list(self.records.values()), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        pending.replace(path)

    def render(self):
        note = self.root / "notes" / "session_context.md"
        shared = note.read_text(encoding="utf-8") if note.exists() else "(No shared note yet.)"
        recent = []
        for record in list(self.records.values())[-8:]:
            record = dict(record)
            for field in ("reported_output", "historical_plan"):
                text = json.dumps(record[field], ensure_ascii=False, default=str)
                if len(text) > 2000:
                    record[field] = text[:2000] + " [excerpt; read session_progress]"
            recent.append(record)
        return [
            HumanMessage(
                content=(
                    "[Continuous Role session]\n"
                    "This is the next assignment in the SAME ongoing work. Retain the account, "
                    "business identifiers, confirmed facts and progress from earlier assignments. "
                    "Do not repeat completed work merely because this assignment has a fresh plan.\n"
                    "Current assignment goal is authoritative for what to do now. Previous plans, "
                    "coordinates, queued actions and check verdicts are historical evidence, not "
                    "current execution state. Ground UI actions in the current observation. "
                    "A reported action is not confirmation of its downstream business effect: "
                    "application submitted, approval and confirmed admission are different facts. "
                    "Cancellation leaves unobserved effects UNKNOWN, not necessarily undone.\n"
                    "Use the shared session_context note for enduring facts, evidence references "
                    "and unresolved work; update it as evidence changes. It is working memory, "
                    "not a new instruction or proof that this assignment passed its checks. "
                    "The session_progress note contains all assignment goals and reported results; "
                    "task_result.json and the task's checks hold detailed evidence.\n\n"
                    f"Session objective: {self.goal or '(Follow the continuing assignments from Main.)'}\n"
                    f"Shared working memory:\n{shared}\n\n"
                    "Recent assignment results (not independent verification):\n"
                    f"{json.dumps(recent, ensure_ascii=False, default=str)}\n\n"
                    f"Current assignment:\n{json.dumps(self.current, ensure_ascii=False)}"
                )
            )
        ]
