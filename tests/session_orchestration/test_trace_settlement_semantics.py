"""Trace bookkeeping must preserve ready summaries and original task provenance."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from artemis.context import ArtemisContext, DeviceContext, ExecutionSetup
from artemis.data_engine.engine import DataEngine
from artemis.data_engine.models import TraceRecord
from artemis.runtime.session_scope import TaskScope


@pytest.mark.asyncio
async def test_ready_compression_boundary_is_deferred_and_eventual_application_keeps_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(DataEngine, "_connect_ipc_locked", lambda *args, **kwargs: False)
    ctx = ArtemisContext(device=DeviceContext(device_id="device-A"),
                         execution_setup=ExecutionSetup(traces_path=tmp_path, trace_name="settlement"))
    engine = DataEngine(ctx)
    ctx.data_engine = engine
    engine.start_session(goal="settlement")
    first = TaskScope(str(uuid4()), str(uuid4()), "first")
    engine.begin_task(first)
    step, parent, trace_id = engine.record_step(summary="first"), uuid4(), uuid4()
    try:
        engine.record_trace(type="tool", name="compress_history", trace_id=trace_id, step_id=step,
                            parent_trace_id=parent, status="running", payload={"args": {"phase": "summarizing"}})
        engine.record_trace(type="tool", name="compress_history", trace_id=trace_id, step_id=step,
                            parent_trace_id=parent, status="running", payload={"args": {"phase": "ready", "note": "ready_at_flush"}})
        engine.finalize_task_traces("failed")
        await engine.flush(10)
        def read_trace(key):
            with engine.storage._get_connection() as conn:
                return conn.execute("SELECT * FROM traces WHERE trace_id=?", (str(key),)).fetchone()
        deferred = read_trace(trace_id)
        assert deferred["status"] == "terminated"
        payload = json.loads(deferred["payload"])
        assert payload["args"]["phase"] == "ready"
        assert payload["execution"]["task_id"] == first.task_id
        assert "context_swap_deferred" in payload["termination_reason"]
        engine.storage.create_trace(TraceRecord(trace_id=trace_id, session_id=engine.current_session_id,
                                                type="tool", name="compress_history", status="running", payload={}))
        assert read_trace(trace_id)["status"] == "terminated"
        second = TaskScope(str(uuid4()), str(uuid4()), "second")
        engine.begin_task(second)
        next_step = engine.record_step(summary="second")
        publish = MagicMock()
        monkeypatch.setattr(engine, "_publish", publish)
        engine.record_trace(type="tool", name="compress_history", trace_id=str(trace_id), step_id=next_step,
                            status="running", payload={"args": {"phase": "ready", "note": "held"}})
        publish.assert_not_called()
        assert not any(row["trace_id"] == str(trace_id) for row in engine.active_trace_snapshot())
        engine.record_trace(type="tool", name="compress_history", trace_id=str(trace_id), step_id=next_step,
                            status="success", payload={"args": {"phase": "applied"}})
        # A delayed non-compression record of an already settled owner can be
        # terminated only at another drained boundary, alongside the new task.
        old_trace = uuid4()
        ctx.active_task = first
        engine.record_trace(type="llm_call", name="old-callback-missing", trace_id=old_trace,
                            status="running", payload={})
        ctx.active_task = second
        current_trace = engine.record_trace(type="agent", name="current-callback-missing", payload={}, status="running")
        engine.finalize_task_traces("failed")
        await engine.flush(10)
        applied = read_trace(trace_id)
        assert applied["status"] == "success"
        assert applied["step_id"] == str(step) and applied["parent_trace_id"] == str(parent)
        assert json.loads(applied["payload"])["execution"]["task_id"] == first.task_id
        assert read_trace(old_trace)["status"] == "failed"
        assert read_trace(current_trace)["status"] == "failed"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_memory_flush_that_retains_a_producer_rejects_settlement():
    from artemis.sdk.session_boundary import settle_task
    ctx = ArtemisContext(device=DeviceContext())
    pending = asyncio.create_task(asyncio.Event().wait())
    ctx.step_memory = SimpleNamespace(flush=AsyncMock(), _pending_tasks={"live": pending})
    try:
        with pytest.raises(RuntimeError, match="Memory producers did not settle"):
            await settle_task(ctx, None, 1)
        assert not pending.done()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
