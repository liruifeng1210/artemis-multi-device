"""Track the actual work when a caller abandons a device operation."""

from __future__ import annotations
import asyncio
import inspect
from dataclasses import dataclass, field
from functools import wraps
from typing import Any


@dataclass
class OperationDrainReport:
    drained: bool
    pending: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


class TaskOperationTracker:
    def __init__(self):
        self.tasks: set[asyncio.Task] = set()
        self.failures: list[str] = []
        self.accepting = True
        self.processes = set()

    def register_process(self, process):
        """Register synchronously immediately after spawn, before reading output."""
        self.processes.add(process)

    async def stop_processes(self, timeout_seconds):
        live = [p for p in self.processes if p.returncode is None]
        for process in live:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        if live:
            waiters = [asyncio.create_task(p.wait()) for p in live]
            _, pending = await asyncio.wait(waiters, timeout=max(0, timeout_seconds / 2))
            if pending:
                for process in live:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                _, pending = await asyncio.wait(waiters, timeout=max(0, timeout_seconds / 2))
            for waiter in pending:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        self.processes = {p for p in self.processes if p.returncode is None}
        if self.processes:
            raise RuntimeError("Command subprocess exit could not be verified")

    async def run(self, operation, *, name="device-operation"):
        if not self.accepting:
            if inspect.iscoroutine(operation):
                operation.close()
            raise RuntimeError("Task operations are settling")
        task = asyncio.create_task(operation, name=name)
        self.tasks.add(task)

        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                self.failures.append(repr(done.exception()))

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def drain(self, timeout_seconds: float) -> OperationDrainReport:
        self.accepting = False
        if self.tasks:
            await asyncio.wait(list(self.tasks), timeout=max(0, timeout_seconds))
        return OperationDrainReport(
            not self.tasks, [t.get_name() for t in self.tasks], list(self.failures)
        )


def tracked_device_operation(func):
    signature = inspect.signature(func)

    @wraps(func)
    async def tracked(*args, **kwargs):
        bound = signature.bind_partial(*args, **kwargs)
        ctx = bound.arguments.get("ctx")
        tracker = getattr(ctx, "operation_tracker", None)
        if tracker is None:
            return await func(*args, **kwargs)
        return await tracker.run(func(*args, **kwargs), name=func.__qualname__)

    return tracked
