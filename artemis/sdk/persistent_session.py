"""Reusable single-device Pro session, isolated from workflow policy."""

from __future__ import annotations
import asyncio
import copy
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from artemis.config import OutputConfig
from artemis.context import ArtemisContext
from artemis.runtime import DeviceExecutionLock
from artemis.runtime.session_operations import TaskOperationTracker
from artemis.runtime.session_scope import SESSION_PAUSE_PATH, TaskScope
from artemis.sdk.session_boundary import settle_task
from third_party.mobile_use.sdk.types.task import Task, TaskRequest


@dataclass
class SessionOptions:
    session_id: str = field(default_factory=lambda: str(uuid4()))
    goal: str = "Persistent role session"
    profile: str = "pro"
    traces_path: Path | None = None
    app_path: Path | None = None
    locked_app_package: str | None = None
    settlement_timeout_seconds: float = 30
    max_resident_tasks: int = 20

    def __post_init__(self):
        UUID(self.session_id)
        if self.settlement_timeout_seconds <= 0 or self.max_resident_tasks < 1:
            raise ValueError("Positive settlement timeout and resident task limit required")


@dataclass
class NativeExecutionResult:
    output: Any = None
    native_outcome: dict | None = None
    last_state: Any = None
    execution_error: str | None = None
    cancelled: bool = False
    settlement_report: dict = field(default_factory=dict)
    completion_data: Any = None
    persistence_ref: Any = None
    session_reusable: bool = False


class PersistentSession:
    def __init__(self, agent, options: SessionOptions):
        self.agent, self.options = agent, options
        self.context = None
        self.state = "STARTING"
        self.current = None
        self.latest_state = None
        self._busy = False
        self._lease = None
        self._pause_token = None
        self._entered = False
        self._recording = False
        self._closed_report = None
        self._snapshot = {}
        self._query_lock = asyncio.Lock()

    async def __aenter__(self):
        if self.context is not None or self._closed_report is not None:
            raise RuntimeError("PersistentSession can only be opened once")
        if self.options.profile.lower() not in ("pro", "default"):
            raise ValueError("Persistent sessions support Pro only")
        from artemis.config import load_agent_config

        if not load_agent_config().memory.transcript.enabled:
            raise ValueError("Persistent Pro requires transcript memory")
        if self.agent._config.disable_checker or self.agent._config.disable_final_check:
            raise ValueError("Persistent Pro requires final checks")
        self.agent._session_id = self.options.session_id
        if self.options.traces_path is not None:
            self.agent._tmp_traces_dir = Path(self.options.traces_path).resolve()
        pause = self.agent._tmp_traces_dir / self.options.session_id / "control" / "pause"
        pause.parent.mkdir(parents=True, exist_ok=True)
        self._pause_token = SESSION_PAUSE_PATH.set(pause)
        try:
            device_id = self.agent._config.device_id
            if not device_id:
                raise ValueError("Persistent session requires an explicit device")
            self._lease = DeviceExecutionLock(
                device_id,
                session_id=self.options.session_id,
                description=self.options.goal[:120],
                concurrency_mode=self.agent._config.concurrency_mode,
                max_concurrency=self.agent._config.max_concurrency,
                ingress="persistent-session",
            )
            cancel = threading.Event()
            acquire = asyncio.create_task(
                asyncio.to_thread(self._lease.acquire, cancel_event=cancel)
            )
            try:
                await asyncio.shield(acquire)
            except BaseException:
                cancel.set()
                await asyncio.shield(acquire)
                raise
            if not self.agent._initialized:
                await self.agent.init()
            profile = self.agent._config.agent_profiles.get(self.options.profile)
            profile = profile or self.agent._config.default_profile
            self.context = ArtemisContext(
                device=self.agent._device_context,
                adb_client=self.agent._adb_client,
                ui_adb_client=self.agent._ui_adb_client,
                llm_config=profile.llm_config,
                agent_config=self.agent._config,
            )
            request = TaskRequest(
                goal=self.options.goal,
                profile=self.options.profile,
                task_name=self.options.session_id,
                app_path=self.options.app_path,
                locked_app_package=self.options.locked_app_package,
            )
            task = Task(
                id=self.options.session_id,
                device=self.context.device,
                status="pending",
                request=request,
                created_at=datetime.now(),
            )
            self.agent._prepare_tracing(task=task, context=self.context)
            self.context.execution_setup.pause_path = pause
            if self.agent._ui_adb_client is not None:
                await self.agent._connect_screen_client(self.context, self.options.session_id)
            await self.agent._ensure_device_unlocked()
            await self.agent._prepare_app_installation(task=task)
            await self.agent._prepare_device_environment(context=self.context)
            await self.agent._prepare_app_lock(task=task, context=self.context)
            await self.context.__aenter__()
            self._entered = True
            if self.agent._config.video_recording_tools_enabled:
                from artemis.controllers.controller_factory import get_controller

                result = await get_controller(self.context).start_video_recording(
                    output_dir=self.context.data_engine.base_dir
                )
                self._recording = bool(result and result.success)
            self.state = "READY"
            return self
        except BaseException:
            await self.close("startup_failed")
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close("completed" if exc_type is None else "cancelled")

    def _publish_state(self, state):
        self.latest_state = state
        self._snapshot = state.model_dump(mode="json")

    async def execute(self, request, scope: TaskScope, completion=None):
        if self.state != "READY" or self._busy or self._query_lock.locked():
            raise RuntimeError("Session is not ready")
        if request.profile and request.profile != self.options.profile:
            raise ValueError("Task cannot change the session profile")
        if request.app_path or request.locked_app_package:
            raise ValueError("Task cannot repeat session application preparation")
        self._busy = True
        self.state = "EXECUTING"
        result = NativeExecutionResult()
        ctx = self.context
        pause_token = SESSION_PAUSE_PATH.set(ctx.execution_setup.pause_path)
        task = None
        try:
            ctx.reset_task_state()
            ctx.operation_tracker = TaskOperationTracker()
            scope.start_step_exclusive = ctx.data_engine.current_step_number
            ctx.data_engine.begin_task(scope)
            self.latest_state = None
            self._snapshot = {}
            task = Task(
                id=scope.attempt_id,
                device=ctx.device,
                status="pending",
                request=request,
                created_at=datetime.now(),
            )
            self.agent._tasks.append(task)
            self.agent._tasks[:] = self.agent._tasks[-self.options.max_resident_tasks :]
            self.agent._prepare_output_files(task)
            state = self.agent._get_graph_state(task)
            config = (
                OutputConfig(
                    output_description=request.output_description,
                    structured_output=request.output_format,
                )
                if request.output_description or request.output_format
                else None
            )
            self.current = asyncio.create_task(
                self.agent._execute_pro_task(
                    task, ctx, state, config, finalize_session=False, on_state=self._publish_state
                )
            )
            try:
                result.output, self.latest_state = await self.current
                result.native_outcome = getattr(self.latest_state, "run_outcome", None)
                if task.result is not None:
                    result.execution_error = task.result.error
            except asyncio.CancelledError:
                result.cancelled = True
                result.execution_error = "cancelled"
                await task.finalize(error="cancelled", cancelled=True)
            except Exception as exc:
                result.execution_error = repr(exc)
                if task.result is None:
                    await task.finalize(error=result.execution_error)
            self.state = "SETTLING"
            result.last_state = self.latest_state
            result.settlement_report = await settle_task(
                ctx, self.latest_state, self.options.settlement_timeout_seconds
            )
            scope.end_step_inclusive = ctx.data_engine.current_step_number
            if completion is not None and not result.cancelled and not result.execution_error:
                self.state = "VERIFYING"
                ctx.operation_tracker = TaskOperationTracker()
                self.current = asyncio.create_task(completion.verify(ctx, result))
                try:
                    result.completion_data = await self.current
                except asyncio.CancelledError:
                    result.cancelled = True
                    result.execution_error = "cancelled"
                except Exception as exc:
                    result.execution_error = repr(exc)
            self.state = "SETTLING"
            result.settlement_report = await settle_task(
                ctx, self.latest_state, self.options.settlement_timeout_seconds
            )
            result.session_reusable = True
            if completion is not None:
                result.persistence_ref = await completion.persist(ctx, result)
            ctx.data_engine.end_task()
            ctx.active_task = None
            self.state = "READY"
            return result
        except BaseException as exc:
            self.state = "FAULTED"
            result.execution_error = repr(exc)
            result.session_reusable = False
            raise
        finally:
            self.current = None
            self._busy = False
            SESSION_PAUSE_PATH.reset(pause_token)

    def request_cancel(self, task_id, attempt_id):
        scope = getattr(self.context, "active_task", None)
        if scope is None or (scope.task_id, scope.attempt_id) != (task_id, attempt_id):
            return {"accepted": False, "code": "stale_attempt"}
        if self.current is not None and not self.current.done():
            self.current.cancel()
        return {"accepted": True}

    async def inspect(self, query=None):
        query = query or {}
        ledger = getattr(self.context, "transcript_ledger", None)
        return {
            "state": self.state,
            "scope": (self.context.active_task.metadata() if self.context.active_task else None),
            "last_state": copy.deepcopy(self._snapshot),
            "transcript": ledger.export_snapshot(
                offset=query.get("offset", 0), limit=query.get("limit", 20)
            )
            if ledger
            else None,
        }

    async def observe(self, query=None):
        query = query or {}
        if set(query) - {"mode", "timeout_seconds"} or query.get("mode", "cached") not in (
            "cached",
            "fresh",
        ):
            raise ValueError("Unsupported observation query")
        if not 0 < query.get("timeout_seconds", 10) <= 30:
            raise ValueError("Observation timeout must be within 30 seconds")
        if query.get("mode", "cached") == "cached":
            return {"consistency": "stream_checkpoint", "state": copy.deepcopy(self._snapshot)}
        if self._busy or self.state != "READY":
            raise RuntimeError("busy_requires_boundary")
        async with self._query_lock:
            from artemis.mcp.action_session import get_action_session

            actions = await get_action_session(self.context)
            await actions.wait_idle(query.get("timeout_seconds", 10))
            return {
                "consistency": "settled",
                "screenshot_b64": await actions.screenshot_b64(timeout=10),
                "ui": await actions.ui_hierarchy(timeout=10),
            }

    async def close(self, reason="close"):
        if self._closed_report is not None:
            return self._closed_report
        if self._busy:
            self.request_cancel(
                self.context.active_task.task_id, self.context.active_task.attempt_id
            )
            raise RuntimeError("Wait for active execute to settle before closing")
        self.state = "CLOSING"
        errors = []
        ctx = self.context
        actions = getattr(ctx, "action_session", None)
        if ctx is not None:
            try:
                await settle_task(ctx, self.latest_state, self.options.settlement_timeout_seconds)
                if self._recording:
                    from artemis.controllers.controller_factory import get_controller

                    await get_controller(ctx).stop_video_recording()
                if ctx.data_engine:
                    ctx.data_engine.end_session(
                        "completed"
                        if reason == "completed"
                        else "failed"
                        if reason == "failed"
                        else "cancelled"
                    )
                    await ctx.data_engine.flush(self.options.settlement_timeout_seconds)
                # Context may own resources even when startup failed before __aenter__.
                await ctx.__aexit__(None, None, None)
                if actions is not None and not actions.closed:
                    raise RuntimeError("Action owner did not exit")
            except BaseException as exc:
                errors.append(repr(exc))
        if not errors:
            try:
                await self.agent.clean(force=True)
                if self._lease is not None:
                    await asyncio.to_thread(self._lease.release)
            except BaseException as exc:
                errors.append(repr(exc))
        if self._pause_token is not None:
            # close may be called by the worker's control task, outside the
            # Context in which startup set the token.
            try:
                SESSION_PAUSE_PATH.reset(self._pause_token)
            except ValueError:
                pass
            self._pause_token = None
        self.state = "CLOSED" if not errors else "FAULTED"
        self._closed_report = {"reason": reason, "lease_released": not errors, "errors": errors}
        return self._closed_report
