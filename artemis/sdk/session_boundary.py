"""Native task boundaries; no orchestration or transport dependencies."""

from __future__ import annotations
import asyncio
from artemis.agents.operator.operator import commit_terminal_turn


async def settle_task(ctx, state, timeout_seconds=30.0) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    def remaining():
        return max(0.001, deadline - loop.time())

    producers = set()
    producers.update(ctx.background_tasks)
    producers.update(
        job["task"]
        for job in ctx.background_jobs.values()
        if isinstance(job, dict) and isinstance(job.get("task"), asyncio.Task)
    )
    if ctx.planner_task is not None:
        producers.add(ctx.planner_task)
    for item in ctx.checkpoint_tasks.values():
        task = getattr(item, "task", None)
        if task is None and isinstance(item, tuple):
            task = item[-1]
        if isinstance(task, asyncio.Task):
            producers.add(task)
    # Summary/chunk services are flushed by their owners, rather than cancelled.
    for task in producers:
        if not task.done():
            task.cancel()
    if producers:
        _, pending = await asyncio.wait(producers, timeout=remaining())
        if pending:
            raise RuntimeError("Native producers did not stop")
    tracker = getattr(ctx, "operation_tracker", None)
    if tracker is not None:
        tracker.accepting = False
        await tracker.stop_processes(remaining())
        report = await tracker.drain(remaining())
        if not report.drained:
            raise RuntimeError(f"Device operations still active: {report.pending}")
        if report.failures:
            raise RuntimeError(f"Device operations failed: {report.failures}")
        # A spawn already in flight can register after the first process sweep.
        await tracker.stop_processes(remaining())
    from artemis.tools.command_tool import shutdown_adb_background_tasks

    await asyncio.wait_for(shutdown_adb_background_tasks(ctx), remaining())
    registry = getattr(ctx, "adb_task_registry", None)
    if registry is not None and any(
        t.process.returncode is None for t in registry.background.values()
    ):
        raise RuntimeError("ADB processes still active")
    if ctx.action_session is not None:
        await ctx.action_session.wait_idle(remaining())
    commit_terminal_turn(ctx, state)
    warnings = []
    for service in (ctx.step_memory, getattr(ctx.transcript_ledger, "chunker", None)):
        if service is not None:
            try:
                await asyncio.wait_for(service.flush(timeout_seconds=remaining() / 2), remaining())
                runtime = getattr(service, "capsule_service", service)
                pending = getattr(runtime, "_pending_tasks", {})
                if any(not task.done() for task in pending.values()):
                    raise RuntimeError("Memory flush returned with an active producer")
            except Exception as exc:
                # A failed flush cannot prove that its private producers stopped.
                raise RuntimeError("Memory producers did not settle") from exc
    # Flush services may have scheduled writes. Their flush must be harvested first.
    if ctx.data_engine is not None:
        await ctx.data_engine.flush(remaining())
    other_tasks = set(ctx.background_tasks)
    other_tasks.update(
        job["task"]
        for job in ctx.background_jobs.values()
        if isinstance(job, dict) and isinstance(job.get("task"), asyncio.Task)
    )
    for task in other_tasks:
        if not task.done():
            task.cancel()
    if other_tasks:
        _, pending = await asyncio.wait(other_tasks, timeout=remaining())
        if pending:
            raise RuntimeError("Background task producers did not stop")
    if ctx.data_engine is not None:
        await ctx.data_engine.flush(remaining())
    return {"drained": True, "optional_warnings": warnings}
