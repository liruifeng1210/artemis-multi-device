"""Immutable accepted fact versions; stale observations do not become current."""

from datetime import datetime, UTC
from artemis.runtime.session_orchestration.models import new_id, now


class FactStore:
    def __init__(self, journal):
        self.journal = journal
        self.versions = []
        self.results = {}

    def accept(self, task_id, attempt_id, role, result, session_id=None):
        self.results[task_id] = (attempt_id, result)
        for key, value in result.outputs.items():
            fact = {
                "fact_id": new_id(),
                "key": key,
                "value": value,
                "source_role": role,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "child_session_id": session_id,
                "observed_at": result.observed_at,
                "evidence_refs": result.artifact_refs,
                "validity": "reported",
                "result_revision": len(self.versions) + 1,
            }
            self.journal.append({"kind": "fact", **fact})
            self.versions.append(fact)

    def resolve(self, inputs):
        result = {}
        for name, value in inputs.items():
            if not isinstance(value, dict) or "from_task" not in value:
                result[name] = value
                continue
            if set(value) - {"from_task", "key", "max_age_seconds"}:
                raise ValueError("Invalid input binding")
            _, source = self.results[value["from_task"]]
            if source.status != "succeeded":
                raise ValueError("Unaccepted input")
            max_age = value.get("max_age_seconds")
            if (
                max_age is not None
                and (datetime.now(UTC) - datetime.fromisoformat(source.observed_at)).total_seconds()
                > max_age
            ):
                raise ValueError("Input fact expired")
            result[name] = source.outputs[value["key"]]
        return result

    def provenance(self, inputs):
        sources = {}
        for name, binding in inputs.items():
            if not isinstance(binding, dict) or "from_task" not in binding:
                continue
            attempt_id, _ = self.results[binding["from_task"]]
            fact = next(
                item
                for item in reversed(self.versions)
                if item["task_id"] == binding["from_task"]
                and item["attempt_id"] == attempt_id
                and item["key"] == binding["key"]
            )
            sources[name] = {
                key: fact[key]
                for key in (
                    "fact_id",
                    "task_id",
                    "attempt_id",
                    "child_session_id",
                    "observed_at",
                    "key",
                )
            }
        return sources

    def invalidate_role(self, role):
        for item in self.versions:
            if item["source_role"] == role:
                self.journal.append(
                    {"kind": "fact_invalidated", "fact_id": item["fact_id"], "observed_at": now()}
                )
        # Conservatively invalidate outputs from a lost session.
        for task_id in {x["task_id"] for x in self.versions if x["source_role"] == role}:
            self.results.pop(task_id, None)
