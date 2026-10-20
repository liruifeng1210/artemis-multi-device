"""Resource ownership and task capabilities for persistent coroutine Sessions."""

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any

from artemis.runtime.execution_scope import CURRENT_EXECUTION, validate_device_scope


class StaleTaskError(RuntimeError):
    """A task no longer owns permission to dispatch device operations."""


@dataclass
class TaskControl:
    task_id: str
    attempt_id: str
    device_id: str
    accepting_actions: bool = True
    step_id: Any = None
    last_recorded_step_id: Any = None
    current_step_dir: Any = None
    operations: set = field(default_factory=set)
    processes: set = field(default_factory=set)

    def validate(self, scope, device_id):
        if (
            scope is None
            or scope.control is not self
            or (scope.task_id, scope.attempt_id) != (self.task_id, self.attempt_id)
            or device_id != self.device_id
            or scope.device_id != self.device_id
            or (not self.accepting_actions and scope.phase != "cleanup")
        ):
            raise StaleTaskError("No active task capability for this device")

    async def own(self, awaitable):
        """Cancellation abandons the waiter, never the underlying device call.

        The Session must drain operations before reusing or releasing its device.
        This also covers to_thread calls which cannot be stopped by Task.cancel.
        """
        task = asyncio.create_task(awaitable)
        self.operations.add(task)

        def done(completed):
            self.operations.discard(completed)
            if not completed.cancelled():
                completed.exception()  # consume errors even when the waiter was cancelled

        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def drain(self, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self.operations:
            done, pending = await asyncio.wait(
                list(self.operations),
                timeout=max(0, deadline - asyncio.get_running_loop().time()),
            )
            self.operations.difference_update(done)
            if pending:
                raise TimeoutError("Device operation still running; Role remains quarantined")

    async def start_process(self, awaitable):
        try:
            self.validate(CURRENT_EXECUTION.get(), self.device_id)
        except Exception:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise

        async def spawn():
            process = await awaitable
            self.processes.add(process)
            return process

        return await self.own(spawn())

    async def stop_processes(self, timeout):
        """Reap even a subprocess whose cancelled caller never received it."""
        for process in tuple(self.processes):
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(process.wait(), timeout=timeout)
            if process.returncode is None:
                raise RuntimeError("Subprocess did not exit; device lease retained")
            self.processes.discard(process)


class TaskDriver:
    """A task-bound gateway around a Role-owned native driver."""

    def __init__(self, driver, ctx):
        self._driver = driver
        self._ctx = ctx

    def __getattr__(self, name):
        value = getattr(self._driver, name)
        if not inspect.iscoroutinefunction(value):
            return value

        async def invoke(*args, **kwargs):
            validate_device_scope(self._ctx)
            return await self._ctx.task_control.own(value(*args, **kwargs))

        return invoke


@dataclass
class SessionResources:
    context: Any
    driver: Any
    agent: Any
    ledger: Any = None

    def ensure_ledger(self, state=None):
        if self.ledger is None:
            # Reuse the native composition root on a stable memory-only context.
            from artemis.agents.operator.operator import OperatorNode

            node = OperatorNode(self.context, tools=[])
            self.ledger = node._ensure_transcript_ledger(state)
        return self.ledger
