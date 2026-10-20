"""Provenance must survive queues and threads, independent of mutable globals."""

import asyncio
import logging
import threading
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace

import pytest

from artemis.runtime.execution_scope import CURRENT_EXECUTION, ExecutionScope, execution_scope
from artemis.runtime.session_runtime import TaskControl
from third_party.mobile_use.utils.logger import DataEngineHandler


@pytest.mark.asyncio
async def test_log_queue_snapshots_engine_task_step_before_late_consumption(monkeypatch):
    import artemis.data_engine.engine as engine_module

    release = threading.Event()
    records = []

    class Engine:
        def __init__(self, session_id):
            self.current_session_id = session_id

        @property
        def current_step_id(self):
            return CURRENT_EXECUTION.get().step_id

        def record_trace(self, **kwargs):
            records.append((self.current_session_id, CURRENT_EXECUTION.get(), kwargs))

    class DelayedHandler(DataEngineHandler):
        def _drain_queue(self):
            release.wait()
            super()._drain_queue()

    handler = DelayedHandler()
    a, b = Engine("session-A"), Engine("session-B")
    scope = ExecutionScope(engine=a, role_id="A", task_id="A1", attempt_id="one", step_id="step-A")
    try:
        with execution_scope(scope):
            handler.emit(logging.LogRecord("role-test", logging.INFO, "", 0, "A message", (), None))
        with execution_scope(replace(scope, engine=b, role_id="B", task_id="B1", step_id="step-B")):
            handler.emit(logging.LogRecord("role-test", logging.INFO, "", 0, "B message", (), None))
        monkeypatch.setattr(engine_module, "_CURRENT_DATA_ENGINE", object())
        release.set()
        await asyncio.to_thread(handler._log_queue.join)
        assert [(row[0], row[1].task_id, row[2]["step_id"]) for row in records] == [
            ("session-A", "A1", "step-A"),
            ("session-B", "B1", "step-B"),
        ]
    finally:
        release.set()
        handler._log_queue.put(None)
        await asyncio.to_thread(handler._worker_thread.join)


@pytest.mark.asyncio
async def test_explicit_context_capture_for_manual_thread_pool_boundary():
    scope = ExecutionScope(role_id="A", task_id="task")
    with execution_scope(scope):
        assert await asyncio.to_thread(CURRENT_EXECUTION.get) is scope
        context = copy_context()
    observed = []
    thread = threading.Thread(target=lambda: observed.append(context.run(CURRENT_EXECUTION.get)))
    thread.start()
    await asyncio.to_thread(thread.join)
    assert observed == [scope]
    assert CURRENT_EXECUTION.get() is None


@pytest.mark.asyncio
async def test_action_consumer_revalidates_stale_queued_request():
    from artemis.mcp.action_session import ActionSession, _SHUTDOWN

    control = TaskControl("task", "attempt", "device")
    scope = ExecutionScope(
        task_id="task", attempt_id="attempt", device_id="device", control=control
    )
    calls = []

    async def call_tool(name, args):
        calls.append(name)
        return "should never execute"

    session = ActionSession(None)
    session.device_context = SimpleNamespace(
        task_control=control, device=SimpleNamespace(device_id="device")
    )
    future = asyncio.get_running_loop().create_future()
    session._requests.put_nowait((future, "click", {}, scope, copy_context()))
    session._requests.put_nowait(_SHUTDOWN)
    control.accepting_actions = False
    await session._serve(SimpleNamespace(call_tool=call_tool))
    with pytest.raises(RuntimeError, match="capability"):
        await future
    assert calls == []


@pytest.mark.asyncio
async def test_timed_out_shell_keeps_underlying_thread_owned():
    from artemis.tools.command_tool import _owned_shell

    started, release = threading.Event(), threading.Event()
    control = TaskControl("task", "attempt", "device")
    scope = ExecutionScope(
        task_id="task", attempt_id="attempt", device_id="device", control=control
    )
    ctx = SimpleNamespace(task_control=control, device=SimpleNamespace(device_id="device"))

    def shell(command):
        started.set()
        release.wait(3)
        return "finished"

    try:
        with execution_scope(scope):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    _owned_shell(ctx, SimpleNamespace(shell=shell), "test"), 0.05
                )
        assert started.is_set() and control.operations
        with pytest.raises(TimeoutError, match="quarantined"):
            await control.drain(0.01)
    finally:
        release.set()
        await control.drain(1)
    assert not control.operations


def test_trace_span_end_keeps_original_scope_and_step():
    from artemis.data_engine.trace import TraceSpan

    records = []
    engine = SimpleNamespace(
        current_step_id="original",
        record_trace=lambda **kw: records.append((CURRENT_EXECUTION.get(), kw)),
    )
    scope = ExecutionScope(engine=engine, task_id="original", step_id="original")
    with execution_scope(scope):
        with TraceSpan("span", ctx=SimpleNamespace(data_engine=engine)):
            engine.current_step_id = "later"
            CURRENT_EXECUTION.set(replace(scope, task_id="later", step_id="later"))
    assert all(owner is scope and record["step_id"] == "original" for owner, record in records)


@pytest.mark.asyncio
async def test_cancelled_subprocess_creation_still_registers_and_reaps_child():
    started, finish = asyncio.Event(), asyncio.Event()
    control = TaskControl("task", "attempt", "device")
    scope = ExecutionScope(
        task_id="task", attempt_id="attempt", device_id="device", control=control
    )

    class Process:
        returncode = None

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    process = Process()

    async def spawn():
        started.set()
        await finish.wait()
        return process

    with execution_scope(scope):
        waiter = asyncio.create_task(control.start_process(spawn()))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    finish.set()
    await control.drain(1)
    assert process in control.processes
    await control.stop_processes(1)
    assert process.returncode == 0 and not control.processes
