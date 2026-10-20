"""Persistent, device-bound Pro Sessions sharing one asyncio event loop.

Main owns orchestration. A Role accepts continuing assignments serially, retaining
its native conversation, memory and device connection between tasks.
"""

import asyncio
import os
import threading
from contextvars import Context
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from artemis.config import OutputConfig, settings
from artemis.context import ArtemisContext, ExecutionSetup
from artemis.data_engine.session_engine import SessionDataEngine
from artemis.drivers.factory import create_driver
from artemis.graph.state import State
from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.runtime.execution_scope import ExecutionScope, execution_scope
from artemis.runtime.session_runtime import SessionResources, TaskControl
from artemis.runtime.session_continuity import SessionContinuity
from artemis.runtime.task_workspace import safe_identifier, write_manifest
from artemis.sdk.agent import Agent
from artemis.sdk.run_outcome import attach_test_summary
from third_party.mobile_use.sdk.types.task import Task, TaskRequest
from third_party.mobile_use.utils.logger import flush_data_engine_logs


@dataclass(frozen=True)
class RoleTaskResult:
    role_id: str
    session_id: str
    task_id: str
    attempt_id: str
    status: str
    output: Any = None
    error: str | None = None
    state: dict | None = None
    workspace: Path | None = None
    cleanup_errors: tuple[str, ...] = ()
    reusable: bool = True


@dataclass
class TaskHandle:
    task_id: str
    attempt_id: str
    _future: asyncio.Future = field(repr=False)

    async def result(self) -> RoleTaskResult:
        # Cancelling a Main waiter never implicitly cancels this Role's task.
        return await asyncio.shield(self._future)


class RoleSessionOpenError(RuntimeError):
    """Startup failed and cleanup needs a later retry via ``session.close()``."""

    def __init__(self, session, cause, cleanup_error):
        super().__init__(f"Role startup failed: {cause}; cleanup incomplete: {cleanup_error}")
        self.session = session


class RoleSession:
    def __init__(self):
        raise TypeError("Use await RoleSession.open(...)")

    @classmethod
    async def open(
        cls,
        role_id: str,
        device_serial: str,
        *,
        config=None,
        traces_path: str | Path | None = None,
        cleanup_timeout: float = 30.0,
        lease_timeout: float = 60.0,
        event_queue_size: int = 256,
        session_goal: str = "",
    ) -> "RoleSession":
        safe_identifier(role_id)
        device_serial = device_serial.strip()
        if not device_serial or device_serial.lower() in (
            "any",
            "pending",
            "default",
            "default-device",
        ):
            raise ValueError("A Role needs an explicit device serial")
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
            raise ValueError(
                "Persistent Role Sessions require explicitly addressable local devices"
            )
        if cleanup_timeout <= 0 or lease_timeout <= 0:
            raise ValueError("Cleanup and lease timeouts must be positive")
        if event_queue_size <= 0:
            raise ValueError("event_queue_size must be positive")
        self = object.__new__(cls)
        self.role_id, self.session_id = role_id, str(uuid4())
        self._loop = asyncio.get_running_loop()
        self.status = "opening"
        self.cleanup_timeout = cleanup_timeout
        self._queue = asyncio.Queue()
        self._handles = {}
        self._cancelled = set()
        self._active = None
        self._run_task = None
        self._worker = None
        self._close_task = None
        self._resource_close_task = None
        self._retained_runs = []
        # A bounded, best-effort live feed. Results and persisted traces are
        # authoritative; an absent Main consumer must not grow memory forever.
        self.events = asyncio.Queue(maxsize=event_queue_size)
        self.dropped_events = 0
        self.resources = None
        self._session_goal = session_goal
        self._continuity = None
        self._agent = Agent(
            config=config.model_copy(deep=True) if config is not None else None,
            device_serial=device_serial,
            session_id=self.session_id,
            concurrency_mode="per_device",
        )
        cfg = self._agent._config
        self._lease = DeviceExecutionLock(
            device_serial,
            session_id=self.session_id,
            ingress="role-session",
            description=f"Persistent Role {role_id}",
            concurrency_mode="per_device",
            max_concurrency=cfg.max_concurrency,
            consume_queue_ticket=False,
        )
        self._lease_held = False
        # Startup must not fall back to another Session's legacy global Engine.
        startup = ExecutionScope(
            role_id=role_id, session_id=self.session_id, device_id=device_serial
        )
        try:
            with execution_scope(startup):
                stop = threading.Event()
                acquiring = asyncio.create_task(
                    asyncio.to_thread(self._lease.acquire, timeout=lease_timeout, cancel_event=stop)
                )
                try:
                    await asyncio.shield(acquiring)
                except BaseException:
                    stop.set()
                    await self._await_owned(acquiring, propagate=False)
                    self._lease_held = getattr(self._lease, "_acquired", False)
                    raise
                self._lease_held = True
                await self._await_owned(asyncio.create_task(self._agent.init()))
                if self._agent._device_context.device_id != device_serial:
                    raise RuntimeError("Initialized device does not match the Role's lease")
                setup = self._execution_setup(cfg, Path(traces_path or settings.TRACES_PATH))
                context = ArtemisContext(
                    device=self._agent._device_context,
                    adb_client=self._agent._adb_client,
                    ui_adb_client=self._agent._ui_adb_client,
                    agent_config=cfg,
                    llm_config=(cfg.agent_profiles.get("pro") or cfg.default_profile).llm_config,
                    execution_setup=setup,
                    owns_data_engine=False,
                )
                engine, scope = await self._await_owned(
                    asyncio.create_task(self._initialize_storage(context, startup))
                )
                with execution_scope(scope):
                    self.resources.driver = create_driver(context)
                    await self._await_owned(
                        asyncio.create_task(
                            self._agent._connect_screen_client(context, self.session_id)
                        )
                    )
                    await self._await_owned(
                        asyncio.create_task(self._agent._ensure_device_unlocked())
                    )
                    await self._await_owned(
                        asyncio.create_task(self._agent._prepare_device_environment(context))
                    )
                self.status = "idle"
                self._worker = asyncio.create_task(
                    self._serve(), name=f"role:{role_id}", context=Context()
                )
                return self
        except BaseException as cause:
            # Retain lease ownership until startup's device work has actually exited.
            cleanup = asyncio.create_task(self._settle_resources())
            await self._await_owned(cleanup, propagate=False)
            if cleanup.exception() is not None:
                self.status = "quarantined"
                raise RoleSessionOpenError(self, cause, cleanup.exception()) from cause
            self.status = "closed"
            raise

    async def _initialize_storage(self, context, startup):
        # This entire coroutine is owned during startup. Even if open() is
        # cancelled during construction, it assigns the Engine before cleanup.
        engine = await asyncio.to_thread(SessionDataEngine, context)
        context.data_engine = engine
        self.resources = SessionResources(context, None, self._agent)
        scope = replace(startup, engine=engine, workspace=engine.global_base_dir / self.session_id)
        context.execution_scope = scope
        engine.subscribe(
            lambda name, data: self._loop.call_soon_threadsafe(
                self._offer_event, {"event": name, "data": data}
            )
        )
        with execution_scope(scope):
            await asyncio.to_thread(
                engine.start_session,
                f"Role {self.role_id}",
                {**context.device.model_dump(), "role_id": self.role_id},
                self.session_id,
                publish_global=False,
            )
        self._continuity = SessionContinuity(engine.base_dir, self._session_goal)
        return engine, replace(scope, workspace=engine.base_dir)

    def _offer_event(self, event):
        if self.events.full():
            self.events.get_nowait()
            self.events.task_done()
            self.dropped_events += 1
        self.events.put_nowait(event)

    @staticmethod
    def _execution_setup(config, traces_path):
        fields = {
            key: getattr(config, key) for key in ExecutionSetup.model_fields if hasattr(config, key)
        }
        fields.update(traces_path=traces_path)
        return ExecutionSetup(**fields)

    def _check_loop(self):
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("A RoleSession belongs to the event loop that opened it")

    def submit(self, request: TaskRequest | str, *, task_id: str | None = None) -> TaskHandle:
        self._check_loop()
        if self.status not in ("idle", "running", "settling") or self._close_task:
            raise RuntimeError(f"Role cannot accept tasks while {self.status}")
        request = (
            TaskRequest(goal=request, profile="pro")
            if isinstance(request, str)
            else request.model_copy(deep=True)
        )
        if request.profile not in (None, "pro"):
            raise ValueError("Persistent Role Sessions currently use the native Pro graph")
        request.profile = "pro"
        task_id = safe_identifier(task_id or str(uuid4()))
        if task_id in self._handles:
            raise ValueError("task_id already submitted to this Role")
        handle = TaskHandle(task_id, str(uuid4()), self._loop.create_future())
        self._handles[task_id] = handle
        self._queue.put_nowait((handle, request))
        return handle

    async def cancel(self, task_id: str, attempt_id: str) -> RoleTaskResult | None:
        self._check_loop()
        handle = self._handles.get(task_id)
        if handle is None or handle.attempt_id != attempt_id:
            return None
        if not handle._future.done():
            self._cancelled.add((task_id, attempt_id))
            if self._active and self._active["handle"] is handle:
                self._active["control"].accepting_actions = False
                # Once settling begins repeated cancellation cannot interrupt cleanup.
                if self._run_task and not self._run_task.done():
                    self._run_task.cancel()
            else:
                self._resolve(handle, "cancelled")
        return await handle.result()

    def resume(self, task_id: str, attempt_id: str) -> bool:
        """Resume a retry-exhausted LLM pause for this exact active attempt."""
        self._check_loop()
        run = self._active
        if run is None or (run["handle"].task_id, run["handle"].attempt_id) != (
            task_id,
            attempt_id,
        ):
            return False
        (run["scope"].workspace / ".paused").unlink(missing_ok=True)
        return True

    async def _serve(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            handle, request = item
            if handle._future.done():
                pass
            elif self.status == "quarantined":
                self._resolve(
                    handle, "failed", error="Role quarantined; task was not started", reusable=False
                )
            elif (handle.task_id, handle.attempt_id) in self._cancelled:
                self._resolve(handle, "cancelled")
            else:
                try:
                    await self._execute(handle, request)
                except Exception as exc:
                    # Even setup/persistence failures must resolve queued work.
                    self.status = "quarantined"
                    if self._active:
                        self._active["control"].accepting_actions = False
                        self._retained_runs.append(self._active)
                        self._active = None
                    self._resolve(handle, "failed", error=str(exc), reusable=False)
            self._cancelled.discard((handle.task_id, handle.attempt_id))
            self._queue.task_done()

    def _resolve(self, handle, status, **kwargs):
        result = RoleTaskResult(
            self.role_id, self.session_id, handle.task_id, handle.attempt_id, status, **kwargs
        )
        if not handle._future.done():
            handle._future.set_result(result)
        return result

    async def _execute(self, handle, request):
        resources = self.resources
        engine = resources.context.data_engine
        workspace = engine.base_dir / "tasks" / handle.task_id / handle.attempt_id
        workspace.mkdir(parents=True)
        (workspace / "notes").mkdir()
        control = TaskControl(handle.task_id, handle.attempt_id, resources.context.device.device_id)
        scope = ExecutionScope(
            engine=engine,
            role_id=self.role_id,
            session_id=self.session_id,
            task_id=handle.task_id,
            attempt_id=handle.attempt_id,
            device_id=control.device_id,
            workspace=workspace,
            control=control,
        )
        engine.task_scopes[(handle.task_id, handle.attempt_id)] = scope
        ctx = ArtemisContext(
            device=resources.context.device,
            adb_client=resources.context.adb_client,
            ui_adb_client=resources.context.ui_adb_client,
            llm_config=resources.context.llm_config,
            agent_config=resources.agent._config,
            execution_setup=resources.context.execution_setup.model_copy(deep=True),
            data_engine=engine,
            session_resources=resources,
            task_control=control,
            task_workspace=workspace,
            owns_data_engine=False,
            execution_scope=scope,
        )
        task = Task(
            id=handle.task_id,
            device=ctx.device,
            status="pending",
            request=request,
            created_at=datetime.now(),
        )
        run = dict(
            handle=handle,
            control=control,
            scope=scope,
            ctx=ctx,
            task=task,
            state=State.initial(request.goal),
            recording=False,
        )
        self._active = run
        self.status = "running"
        status, output, error = "completed", None, None
        cleanup_errors = []
        with execution_scope(scope):
            write_manifest(
                workspace / "task_manifest.json",
                {**scope.metadata(), "goal": request.goal, "status": "running"},
            )
            try:
                ledger = resources.ensure_ledger(run["state"])
                ledger.begin_task(handle.task_id, handle.attempt_id)
                self._continuity.begin(handle.task_id, handle.attempt_id, request.goal)
                ledger.set_context_provider(self._continuity.render)
                ctx.transcript_ledger = ledger
                ctx.step_memory = resources.context.step_memory
                engine._publish("task_started", {"goal": request.goal})
                self._run_task = asyncio.create_task(self._execute_native(run))
                state, output = await self._run_task
                if state is None:
                    raise RuntimeError("Native graph returned no final state")
                outcome = state.run_outcome or {}
                if outcome.get("task_status") == "blocked" or (outcome.get("tests") or {}).get(
                    "failed", 0
                ):
                    status = "failed"
                    error = "Native task outcome contains blocked criteria or failed checks"
            except asyncio.CancelledError:
                status, error = "cancelled", "Task cancelled"
            except Exception as exc:
                status, error = "failed", f"{type(exc).__name__}: {exc}"
            finally:
                self._run_task = None
                self.status = "settling"
                control.accepting_actions = False
                await task.finalize(
                    content=output,
                    state=run["state"].model_dump(),
                    error=error,
                    cancelled=status == "cancelled",
                )
                cleanup = asyncio.create_task(self._finish_task(run))
                run["cleanup_task"] = cleanup
                try:
                    _, pending = await asyncio.wait([cleanup], timeout=self.cleanup_timeout)
                    if pending:
                        cleanup.add_done_callback(
                            lambda done: done.exception() if not done.cancelled() else None
                        )
                        raise TimeoutError(
                            "Task cleanup is still running; Role remains quarantined"
                        )
                    cleanup.result()
                except Exception as exc:
                    cleanup_errors.append(f"{type(exc).__name__}: {exc}")
                    self._retained_runs.append(run)
                if cleanup_errors:
                    self.status = "quarantined"
                else:
                    self.status = "idle"
                result = RoleTaskResult(
                    self.role_id,
                    self.session_id,
                    handle.task_id,
                    handle.attempt_id,
                    status,
                    output,
                    error,
                    run["state"].model_dump(),
                    workspace,
                    tuple(cleanup_errors),
                    not cleanup_errors,
                )
                try:
                    finalizing = asyncio.create_task(
                        self._persist_task_result(result, scope, request)
                    )
                    run["finalize_task"] = finalizing
                    _, pending = await asyncio.wait([finalizing], timeout=self.cleanup_timeout)
                    if pending:
                        finalizing.add_done_callback(
                            lambda done: done.exception() if not done.cancelled() else None
                        )
                        raise TimeoutError(
                            "Task persistence is still running; Role remains quarantined"
                        )
                    finalizing.result()
                except Exception as exc:
                    self.status = "quarantined"
                    result = replace(
                        result, reusable=False, cleanup_errors=(*result.cleanup_errors, str(exc))
                    )
                    if run not in self._retained_runs:
                        self._retained_runs.append(run)
                    try:
                        self._write_task_result(result, scope, request)
                    except OSError:
                        pass
                handle._future.set_result(result)
                self._active = None

    async def _persist_task_result(self, result, scope, request):
        self._write_task_result(result, scope, request)
        scope.engine._publish(
            "task_finished", {"status": result.status, "reusable": result.reusable}
        )
        await flush_data_engine_logs(scope.engine)
        await scope.engine.flush()

    def _write_task_result(self, result, scope, request):
        write_manifest(scope.workspace / "task_result.json", result.__dict__)
        write_manifest(
            scope.workspace / "task_manifest.json",
            {
                **scope.metadata(),
                "goal": request.goal,
                "status": result.status,
                "reusable": result.reusable,
                "cleanup_errors": result.cleanup_errors,
            },
        )
        self._continuity.finish(result, request.goal)

    async def _execute_native(self, run):
        ctx, task = run["ctx"], run["task"]
        agent = self.resources.agent
        if task.request.app_path:
            await run["control"].own(agent._prepare_app_installation(task))
        if task.request.locked_app_package:
            await run["control"].own(agent._prepare_app_lock(task, ctx))
        if ctx.execution_setup.video_recording_tools_enabled:
            from artemis.controllers.controller_factory import get_controller

            run["recording"] = True  # cleanup also owns partially started recorders
            await run["control"].own(
                get_controller(ctx).start_video_recording(output_dir=ctx.task_workspace)
            )
        await task.set_status(status="running", message="Invoking native Pro graph")
        state = await agent._invoke_prepared_graph(
            context=ctx,
            task=task,
            state=run["state"],
            on_state=lambda state: run.update(state=state),
        )
        if state is None:
            return None, None
        await ctx.data_engine.flush()
        output_config = (
            OutputConfig(
                output_description=task.request.output_description,
                structured_output=task.request.output_format,
            )
            if task.request.output_description or task.request.output_format
            else None
        )
        output = await agent._extract_output(
            task_name=task.get_name(),
            ctx=ctx,
            request=task.request,
            output_config=output_config,
            state=state,
        )
        return state, attach_test_summary(output, state.run_outcome)

    async def _finish_task(self, run, *, for_close=False):
        ctx, control, state = run["ctx"], run["control"], run["state"]
        # Cancel producers, including those not present in ctx.background_tasks.
        producers = list(ctx.background_tasks)
        producers += [
            job["task"]
            for job in ctx.background_jobs.values()
            if isinstance(job.get("task"), asyncio.Task)
        ]
        producers += [entry.task for entry in ctx.checkpoint_tasks.values()]
        if ctx.planner_task is not None:
            producers.append(ctx.planner_task)
        for producer in producers:
            if not producer.done():
                producer.cancel()
        if producers:
            _, pending = await asyncio.wait(producers, timeout=self.cleanup_timeout)
            if pending:
                raise TimeoutError("Task producers did not stop")
            await asyncio.gather(*producers, return_exceptions=True)
        from artemis.runtime.task_lifecycle import harvest_task_checkpoints

        harvest_task_checkpoints(ctx, state)
        from artemis.tools.command_tool import shutdown_adb_background_tasks

        await shutdown_adb_background_tasks(ctx)
        if ctx.action_session is not None:
            closing = asyncio.create_task(ctx.action_session.aclose())
            _, pending = await asyncio.wait([closing], timeout=self.cleanup_timeout)
            if pending:
                control.operations.add(closing)
                closing.add_done_callback(control.operations.discard)
                raise TimeoutError("Action transport did not close")
            await closing
            ctx.action_session = None
        await control.drain(self.cleanup_timeout)
        # A shielded command may have registered a background process while
        # its original waiter was being cancelled.
        await shutdown_adb_background_tasks(ctx)
        with execution_scope(replace(run["scope"], phase="cleanup")):
            if run["recording"]:
                from artemis.controllers.controller_factory import get_controller

                await get_controller(ctx).stop_video_recording()
                run["recording"] = False
        await control.drain(self.cleanup_timeout)
        await control.stop_processes(self.cleanup_timeout)
        ledger = self.resources.ledger
        if ledger:
            turn = control.turn_outcome
            ledger.commit_staged(
                step_key=turn.step_id if turn else None,
                validator_result=turn.validator_result if turn else None,
            )
        if ctx.step_memory:
            await ctx.step_memory.flush()
        if ledger and ledger.chunker:
            await ledger.chunker.flush()
        if ctx.mcp_client_ctx is not None:
            await ctx.mcp_client_ctx.__aexit__(None, None, None)
            ctx.mcp_client_ctx = None
        await flush_data_engine_logs(ctx.data_engine)
        await ctx.data_engine.flush()
        if ctx.data_engine._write_errors and not for_close:
            raise RuntimeError("Session storage failed; Role cannot accept more tasks")

    @staticmethod
    async def _await_owned(task, *, propagate=True):
        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done() and task.cancelled():
                    break
                interrupted = True
            except Exception:
                break
        if not propagate:
            if not task.cancelled():
                task.exception()
            return
        result = task.result()
        if interrupted:
            raise asyncio.CancelledError
        return result

    async def close(self):
        self._check_loop()
        if self.status == "closed":
            return
        if self._close_task is None or self._close_task.done() and self._close_task.exception():
            self._close_task = asyncio.create_task(self._close())
        return await self._await_owned(self._close_task)

    async def _close(self):
        for handle in self._handles.values():
            if not handle._future.done():
                self._cancelled.add((handle.task_id, handle.attempt_id))
        if self._active and self._run_task:
            self._active["control"].accepting_actions = False
            self._run_task.cancel()
        self._queue.put_nowait(None)
        if self._worker:
            await self._worker
        self.status = "closing"
        try:
            for run in self._retained_runs:
                with execution_scope(run["scope"]):
                    for name in ("cleanup_task", "finalize_task"):
                        previous = run.get(name)
                        if previous is not None and not previous.done():
                            _, pending = await asyncio.wait(
                                [previous], timeout=self.cleanup_timeout
                            )
                            if pending:
                                raise TimeoutError(
                                    "Task cleanup still owns resources; device lease retained"
                                )
                    await self._finish_task(run, for_close=True)
            await self._settle_resources()
        except BaseException:
            self.status = "quarantined"
            raise
        self.status = "closed"

    async def _settle_resources(self):
        closing = self._resource_close_task
        if closing is None or closing.done() and closing.exception() is not None:
            closing = self._resource_close_task = asyncio.create_task(self._close_resources())
            closing.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        _, pending = await asyncio.wait([closing], timeout=self.cleanup_timeout)
        if pending:
            raise TimeoutError("Session resources are still closing; device lease retained")
        return closing.result()

    async def _close_resources(self):
        prewarm = getattr(self._agent, "_prewarm_task", None)
        if prewarm is not None:
            if not prewarm.done():
                prewarm.cancel()
            await asyncio.gather(prewarm, return_exceptions=True)
        if self.resources:
            engine = self.resources.context.data_engine
            with execution_scope(
                ExecutionScope(
                    engine=engine,
                    role_id=self.role_id,
                    session_id=self.session_id,
                    device_id=self.resources.context.device.device_id,
                    workspace=engine.base_dir,
                )
            ):
                client = self.resources.context._genai_client
                if client is not None:
                    await client.aio.aclose()
                    client.close()
                    self.resources.context._genai_client = None
                await self._agent.clean(force=True)
                await asyncio.to_thread(engine.end_session, "completed")
                await flush_data_engine_logs(engine)
                await engine.shutdown()
                await flush_data_engine_logs(engine)
                await engine.flush()
        elif getattr(self._agent, "_ui_adb_client", None) is not None:
            await self._agent.clean(force=True)
        if self._lease_held:
            await asyncio.to_thread(self._lease.release)
            self._lease_held = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
