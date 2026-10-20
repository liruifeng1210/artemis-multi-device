"""Device recovery requires the previous process to have exited."""

import asyncio
from artemis.runtime.session_orchestration.worker_handle import WorkerHandle


class DeviceManager:
    def __init__(self):
        self.quarantined = set()

    async def takeover(self, old, host, port):
        role_id = old.role.role_id
        try:
            await old.close("takeover")
            if old.process.returncode is None:
                raise RuntimeError("Old device owner still alive")
            # The new worker acquires the real DeviceExecutionLock normally.
            # A stale/live external owner is handled by the startup deadline.
            replacement = WorkerHandle(old.workflow, old.role, old.events)
            return replacement
        except BaseException:
            self.quarantined.add(role_id)
            raise
