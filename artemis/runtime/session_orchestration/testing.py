"""Explicit test backend for process/protocol tests; never claims device validation."""

import asyncio
import os
from artemis.runtime.session_orchestration.artifacts import atomic_json
from artemis.runtime.session_orchestration.models import TaskResult, CheckResult, now


class FakeSession:
    def __init__(self, root):
        self.root = root
        self.state = "READY"
        self.history = []
        self.current = None
        self.identity = None

    async def execute_spec(self, spec, store, task_id, attempt_id):
        self.state = "EXECUTING"
        self.identity = (task_id, attempt_id)
        self.current = asyncio.create_task(asyncio.sleep(spec.inputs.get("_test_delay", 0.01)))
        try:
            await self.current
            self.history.append(spec.goal)
            atomic_json(
                store.path / "test_evidence.json",
                {"pid": os.getpid(), "history": self.history, "observed_at": now()},
            )
            evidence = str((store.path / "test_evidence.json").relative_to(self.root))
            outputs = dict(spec.inputs.get("_test_outputs", {}))
            result = TaskResult(
                status="succeeded",
                outputs=outputs,
                checks=[
                    CheckResult(id=c.id, verdict="passed", evidence_refs=[evidence])
                    for c in spec.completion
                ],
                artifact_refs=[evidence],
                side_effect_state="confirmed",
                session_reusable=True,
            )
        except asyncio.CancelledError:
            result = TaskResult(
                status="cancelled", side_effect_state="uncertain", session_reusable=True
            )
        store.finish(result.model_dump(mode="json"))
        self.state = "READY"
        return result

    def request_cancel(self, task, attempt):
        if self.identity != (task, attempt):
            return {"accepted": False, "code": "stale_attempt"}
        if self.current:
            self.current.cancel()
        return {"accepted": True}

    async def inspect(self, query):
        return {"state": self.state, "history": list(self.history), "pid": os.getpid()}

    async def observe(self, query):
        return {
            "consistency": "settled" if self.state == "READY" else "stream_checkpoint",
            "test_backend": True,
        }

    async def close(self, reason):
        self.state = "CLOSED"
        return {"reason": reason, "lease_released": True, "errors": []}
