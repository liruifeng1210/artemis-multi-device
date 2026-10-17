"""Public Pro case runner: a single session spanning distinct Android devices."""

import asyncio
from contextlib import AsyncExitStack
import os
from pathlib import Path
import time
from uuid import uuid4

from artemis.agents.role_planner import plan_roles
from artemis.config import settings
from artemis.context import ArtemisContext, DeviceContext, ExecutionSetup, AppLaunchResult
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.engine import DataEngine
from artemis.graph.graph import get_graph
from artemis.graph.state import State
from artemis.runtime.cancel_requests import watch_for_cancel_request
from artemis.runtime.execution_scope import execution_scope
from artemis.runtime.execution_scope import ExecutionScope
from artemis.runtime.multi_device.leases import DeviceSetLease
from artemis.runtime.multi_device.models import MultiDeviceRequest
from artemis.runtime.multi_device.runtime import MultiDeviceRuntime
from artemis.sdk.agent import Agent
from artemis.utils.plan_grammar import parse_plan
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


class MultiDeviceAgent:
    """Run a role-bound Pro case. Call ``stop_current_task`` to cancel the whole case."""

    def __init__(self, *, config=None, session_id=None, binding_preparer=None):
        if session_id is not None:
            from uuid import UUID

            session_id = str(UUID(session_id))
        if config is None:
            from artemis.sdk.builders import Builders

            config = Builders.AgentConfig.with_checker(
                midway_checks=True,
                final_check=True,
                assert_failure_policy="halt",
            ).build()
        self.config = config
        self.session_id = session_id
        # Trusted host callback, deliberately absent from serializable case requests.
        self.binding_preparer = binding_preparer
        self._task = None
        self._lock = asyncio.Lock()

    def stop_current_task(self):
        if self._task is not None and not self._task.cancelling():
            logger.warning(f"[multi-device] case.cancel_requested run_id={self.session_id}")
            self._task.cancel()

    async def run_case(self, request=None, **kwargs):
        request = MultiDeviceRequest.model_validate(
            request if request is not None else kwargs
        ).model_copy(deep=True)
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
            raise ValueError("Multi-device cases require directly addressable ADB devices")
        async with self._lock:
            self._task = asyncio.create_task(self._run(request))
            watcher = (
                asyncio.create_task(
                    watch_for_cancel_request(
                        self.stop_current_task,
                        session_id=self.session_id,
                    )
                )
                if self.session_id
                else None
            )
            try:
                return await self._task
            finally:
                self._task = None
                if watcher is not None:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)

    async def _run(self, request):
        run_id = self.session_id or str(uuid4())
        started = time.monotonic()
        stage = "role_planning"
        logger.info(f"[multi-device] case.start run_id={run_id} binding_count={len(request.role_bindings)} max_turns={request.max_turns} timeout_s={request.timeout_seconds}")
        contexts, agents, recordings = {}, {}, {}
        runtime, engine = None, None
        result = {"run_id": run_id, "status": "failed", "profile": "pro"}
        config = self.config
        profile = config.agent_profiles.get("pro") or config.default_profile
        planning_ctx = ArtemisContext(
            device=DeviceContext(device_id="role-planning"),
            llm_config=profile.llm_config,
            agent_config=config,
        )

        async def execute():
            nonlocal runtime, engine, stage
            with execution_scope(ExecutionScope(phase="role_planning")):
                role_plan = await plan_roles(planning_ctx, request)
            logger.info(f"[multi-device] case.roles_ready run_id={run_id} roles={[role.id for role in role_plan.roles]}")
            result["role_plan"] = role_plan.model_dump()
            from artemis.agents.role_planner import validate_role_preparation

            if self.binding_preparer is None:
                validate_role_preparation(role_plan, request)
            if not request.role_bindings:
                from artemis.runtime.multi_device.allocation import allocate_roles

                request.role_bindings = await allocate_roles(role_plan, request.preferred_device)
            logger.info(f"[multi-device] case.allocated run_id={run_id} bindings={ {r: d.serial for r, d in request.role_bindings.items()} }")
            result["device_bindings"] = {r: d.model_dump() for r, d in request.role_bindings.items()}
            from artemis.utils.startup_progress import publish_startup_progress

            publish_startup_progress(
                "devices_allocated",
                "RolePlan: " + ", ".join(f"{r} → {d.serial}" for r, d in request.role_bindings.items()),
                session_id=run_id,
            )
            stage = "lease_wait"
            from artemis.runtime.multi_device.jobs import write_json, case_directory

            write_json(
                case_directory(run_id) / "case_progress.json",
                {
                    "status": "waiting_for_devices",
                    "role_plan": role_plan.model_dump(),
                },
            )
            async with (
                DeviceSetLease(
                    [d.serial for d in request.role_bindings.values()],
                    run_id,
                    timeout=request.lease_timeout_seconds,
                    max_concurrency=config.max_concurrency,
                    concurrency_mode=config.concurrency_mode,
                ),
                AsyncExitStack() as stack,
            ):
                try:
                    stage = "initializing_devices"
                    for role in role_plan.roles:
                        spec = request.role_bindings[role.id]
                        logger.info(f"[multi-device] device.init_start run_id={run_id} role_id={role.id} device_id={spec.serial}")
                        agent = Agent(config=config, device_serial=spec.serial, session_id=run_id)
                        agents[role.id] = agent
                        await agent.init()
                        setup_fields = {
                            field: getattr(config, field)
                            for field in ExecutionSetup.model_fields
                            if hasattr(config, field)
                        }
                        setup_fields.update(
                            traces_path=Path(settings.TRACES_PATH),
                            trace_name=run_id,
                            video_recording_tools_enabled=request.record_video,
                            # Case audits use recorded evidence, never a default device probe.
                            disable_device_probes=True,
                        )
                        ctx = ArtemisContext(
                            device=agent._device_context,
                            adb_client=agent._adb_client,
                            ui_adb_client=agent._ui_adb_client,
                            llm_config=profile.llm_config,
                            agent_config=config,
                            execution_setup=ExecutionSetup(**setup_fields),
                            role_id=role.id,
                            owns_data_engine=False,
                        )
                        contexts[role.id] = ctx
                        await stack.enter_async_context(ctx)
                        if engine is None:
                            engine = DataEngine(ctx)
                            engine.start_session(
                                request.case, {"profile": "pro", "multi_device": True}, run_id
                            )
                        ctx.data_engine = engine
                        logger.info(
                            f"[multi-device] device.init_complete run_id={run_id} role_id={role.id} device_id={ctx.device.device_id}"
                        )
                    if self.binding_preparer is not None:
                        import inspect

                        stage = "binding_preparation"
                        with execution_scope(ExecutionScope(engine=engine, phase=stage)):
                            logger.info(
                                f"[bindings] preparation.started run_id={run_id} roles={len(role_plan.roles)}"
                            )
                            prepared = self.binding_preparer(
                                engine.value_bindings, role_plan, request
                            )
                            if inspect.isawaitable(prepared):
                                await prepared
                            validate_role_preparation(role_plan, request, engine.value_bindings)
                            logger.info(
                                "[bindings] preparation.completed run_id={} resources={}".format(
                                    run_id,
                                    len(engine.value_bindings.export_definitions()["resources"]),
                                )
                            )
                    stage = "initializing_runtime"
                    runtime = MultiDeviceRuntime(role_plan, contexts, max_turns=request.max_turns)
                    runtime.save_manifest(request)
                    runtime.save_progress("preparing_devices")
                    stage = "preparing_devices"
                    for role_id, ctx in contexts.items():
                        agent = agents[role_id]
                        logger.info(f"[multi-device] device.prepare_start run_id={run_id} role_id={role_id} device_id={ctx.device.device_id}")
                        with execution_scope(runtime.scope(phase="preparation", role_id=role_id)):
                            await agent._connect_screen_client(ctx, run_id)
                            try:
                                await agent._ensure_device_unlocked()
                            except Exception as exc:
                                raise RuntimeError(f"Role {role_id}, device {ctx.device.device_id}: {exc}") from exc
                            await agent._prepare_device_environment(ctx)
                            app = request.app_profile
                            if app is not None:
                                result.setdefault("app_packages", {})[role_id] = app.package
                                if app.apk_path:
                                    await agent.install_apk(app.apk_path)
                                from third_party.mobile_use.utils.app_launch_utils import (
                                    launch_app_with_retries,
                                )

                                ok, error = await launch_app_with_retries(ctx, app.package)
                                ctx.execution_setup.app_lock_status = AppLaunchResult(
                                    locked_app_package=app.package,
                                    locked_app_initial_launch_success=ok,
                                    locked_app_initial_launch_error=error,
                                )
                                if not ok:
                                    raise RuntimeError(
                                        f"App preparation failed for {role_id}: {error}"
                                    )
                            controller = UnifiedMobileController(ctx)
                            runtime.initial_observations[
                                role_id
                            ] = await controller.take_screenshot()
                            if request.record_video:
                                # Startup may already own a scrcpy process while
                                # waiting for its first frame. Register cleanup
                                # ownership before the first cancellable await.
                                recordings[role_id] = controller
                                recording = await controller.start_video_recording(
                                    output_dir=engine.base_dir / "recordings" / role_id,
                                )
                                if not recording.success:
                                    logger.warning(f"[multi-device] recording.start_failed run_id={run_id} role_id={role_id}")
                                    engine._publish(
                                        "recording_warning",
                                        {"role_id": role_id, "message": recording.message},
                                    )
                        logger.info(f"[multi-device] device.prepare_complete run_id={run_id} role_id={role_id} device_id={ctx.device.device_id} recording={request.record_video}")
                    if request.app_profile:
                        runtime.app_instructions = request.app_profile.model_dump_json()
                    stage = "graph"
                    logger.info(f"[multi-device] graph.start run_id={run_id} roles={len(contexts)}")
                    graph = await get_graph(runtime.control_ctx, runtime=runtime)
                    final_state = await graph.ainvoke(
                        State.initial(request.case).model_dump(),
                        config={
                            "recursion_limit": request.max_turns * 10 + 20,
                            "callbacks": config.graph_config_callbacks,
                        },
                    )
                    result["outcome"] = final_state.get("run_outcome")
                    if not config.disable_outputter and (
                        request.output_description or config.outputter.force_synthesis
                    ):
                        from artemis.agents.outputter.outputter import outputter
                        from artemis.config import OutputConfig

                        result["output"] = await outputter(
                            ctx=runtime.control_ctx,
                            output_config=OutputConfig(output_description=request.output_description),
                            graph_output=State.model_validate(final_state),
                        )
                    result["device_bindings"] = {
                        k: v.model_dump() for k, v in request.role_bindings.items()
                    }
                    result["turns"] = runtime.turns
                    outcome = result["outcome"] or {}
                    tests = outcome.get("tests") or {}
                    from artemis.graph.checkpoints import final_check_enabled

                    audit_required = final_check_enabled(runtime.control_ctx)
                    result["final_review_status"] = runtime.final_review_status
                    result["status"] = (
                        "failed"
                        if outcome.get("task_status") == "blocked"
                        or tests.get("failed", 0)
                        or not parse_plan(runtime.read_plan()).all_top_level_done
                        else "inconclusive"
                        if (
                            tests.get("inconclusive", 0)
                            or tests.get("unchecked", 0)
                            or (audit_required and runtime.final_review_status != "passed")
                        )
                        else "completed"
                    )
                    logger.info(f"[multi-device] graph.complete run_id={run_id} status={result['status']} turns={runtime.turns} final_review={runtime.final_review_status} tests_passed={tests.get('passed', 0)} tests_failed={tests.get('failed', 0)} tests_inconclusive={tests.get('inconclusive', 0)} tests_unchecked={tests.get('unchecked', 0)}")
                    return result
                finally:
                    # Keep the failing phase for diagnostics while resources are cleaned.
                    logger.info(f"[multi-device] cleanup.start run_id={run_id} initialized_roles={list(contexts)} recordings={list(recordings)}")
                    # Device resources are cleaned while the complete set of leases is still held.
                    if runtime is not None:
                        runtime.closed = True
                    async def cleanup_resources():
                        pending = []
                        for ctx in contexts.values():
                            for entry in ctx.checkpoint_tasks.values():
                                task = getattr(entry, "task", None)
                                if task is not None and not task.done():
                                    task.cancel()
                                    pending.append(task)
                            if ctx.planner_task is not None and not ctx.planner_task.done():
                                ctx.planner_task.cancel()
                                pending.append(ctx.planner_task)
                        if pending:
                            try:
                                await asyncio.wait_for(
                                    asyncio.gather(*pending, return_exceptions=True), 15
                                )
                            except Exception as exc:
                                logger.warning(f"[multi-device] cleanup.error run_id={run_id} resource=background_checks error_type={type(exc).__name__}")
                                result.setdefault("cleanup_errors", []).append(
                                    f"Background checks: {exc}"
                                )
                        video_map = {}
                        for role_id, controller in recordings.items():
                            try:
                                recording = await asyncio.wait_for(
                                    controller.stop_video_recording(), 20
                                )
                                if recording and recording.success:
                                    video_map[role_id] = str(recording.video_path)
                            except Exception as exc:
                                logger.warning(f"[multi-device] cleanup.error run_id={run_id} resource=recording role_id={role_id} error_type={type(exc).__name__}")
                                result.setdefault("cleanup_errors", []).append(
                                    f"Recording {role_id}: {exc}"
                                )
                        result["videos"] = video_map
                        if engine is not None:
                            try:
                                engine.update_session_device_info(video_map=video_map)
                            except Exception as exc:
                                logger.warning(f"[multi-device] cleanup.error run_id={run_id} resource=recording_metadata error_type={type(exc).__name__}")
                                result.setdefault("cleanup_errors", []).append(
                                    f"Recording metadata: {exc}"
                                )
                        from artemis.tools.command_tool import shutdown_adb_background_tasks

                        for role_id, ctx in contexts.items():
                            try:
                                await asyncio.wait_for(shutdown_adb_background_tasks(ctx), 15)
                            except Exception as exc:
                                logger.warning(f"[multi-device] cleanup.error run_id={run_id} resource=adb_tasks role_id={role_id} error_type={type(exc).__name__}")
                                result.setdefault("cleanup_errors", []).append(
                                    f"Background tasks {role_id}: {exc}"
                                )
                        for role_id, agent in agents.items():
                            try:
                                await asyncio.wait_for(agent.clean(force=True), 15)
                            except Exception as exc:
                                logger.warning(f"[multi-device] cleanup.error run_id={run_id} resource=device role_id={role_id} error_type={type(exc).__name__}")
                                result.setdefault("cleanup_errors", []).append(
                                    f"Device {role_id}: {exc}"
                                )

                    cleanup_task = asyncio.create_task(cleanup_resources())
                    interrupted = False
                    while not cleanup_task.done():
                        try:
                            await asyncio.shield(cleanup_task)
                        except asyncio.CancelledError:
                            interrupted = True
                    cleanup_task.result()
                    logger.info(f"[multi-device] cleanup.complete run_id={run_id} errors={len(result.get('cleanup_errors', []))}")
                    if interrupted:
                        raise asyncio.CancelledError

        try:
            async with asyncio.timeout(request.timeout_seconds):
                await execute()
        except asyncio.CancelledError:
            logger.warning(
                f"[multi-device] case.cancelled run_id={run_id} stage={stage} elapsed_ms={int((time.monotonic() - started) * 1000)}"
            )
            result.update(status="cancelled", error="Case cancelled")
            raise
        except Exception as exc:
            logger.error(
                f"[multi-device] case.failed run_id={run_id} stage={stage} error_type={type(exc).__name__} elapsed_ms={int((time.monotonic() - started) * 1000)}"
            )
            error = (
                "Case binding preparation failed"
                if stage == "binding_preparation"
                else f"{type(exc).__name__}: {exc}"
            )
            result.update(status="failed", error=error)
            from artemis.utils.startup_progress import publish_startup_progress

            publish_startup_progress("failed", result["error"], session_id=run_id)
        finally:
            from artemis.runtime.multi_device.jobs import case_directory, write_json

            directory = engine.base_dir if engine is not None else case_directory(run_id)
            result["trace_path"] = str(directory)
            if engine is not None:
                replay_builder = getattr(engine, "build_replay_definition", None)
                if callable(replay_builder):
                    try:
                        replay = replay_builder()
                        result["replay_ready"] = replay["replay_ready"]
                        result["replay_issues"] = replay["issues"]
                    except (ValueError, TypeError, KeyError, RuntimeError):
                        # Artifact validation must never bypass device/engine finalization.
                        result["replay_ready"] = False
                        result["replay_issues"] = [{"reason": "binding_validation_failed"}]
                try:
                    engine.end_session(
                        result["status"]
                        if result["status"] in ("completed", "cancelled")
                        else "failed"
                    )
                except Exception as exc:
                    logger.warning(f"[multi-device] trace.finalize_failed run_id={run_id} error_type={type(exc).__name__}")
                    result.setdefault("cleanup_errors", []).append(f"Trace finalization: {exc}")
                try:
                    await engine.shutdown()
                except Exception as exc:
                    logger.warning(f"[multi-device] trace.shutdown_failed run_id={run_id} error_type={type(exc).__name__}")
                    result.setdefault("cleanup_errors", []).append(f"Trace shutdown: {exc}")
            write_json(directory / "case_result.json", result)
            logger.info(f"[multi-device] case.finished run_id={run_id} status={result['status']} elapsed_ms={int((time.monotonic()-started)*1000)} trace_path={directory} cleanup_errors={len(result.get('cleanup_errors', []))}")
        return result
