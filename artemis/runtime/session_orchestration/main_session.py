"""Workflow orchestration with durable dispatch and strict acceptance."""

from __future__ import annotations
import asyncio
import copy
import json
import time
from pathlib import Path
from artemis.runtime.session_orchestration.artifacts import atomic_json, validate_evidence
from artemis.runtime.session_orchestration.compatibility import preflight
from artemis.runtime.session_orchestration.contract import accept_result
from artemis.runtime.session_orchestration.device_manager import DeviceManager
from artemis.runtime.session_orchestration.facts import FactStore
from artemis.runtime.session_orchestration.journal import Journal
from artemis.runtime.session_orchestration.models import TaskResult, new_id
from artemis.runtime.session_orchestration.planner import MainPlanner
from artemis.runtime.session_orchestration.scheduler import Scheduler
from artemis.runtime.session_orchestration.transport import Connection, MAX_FRAME_BYTES
from artemis.runtime.session_orchestration.worker_handle import WorkerHandle


class MainSession:
    def __init__(self, planner=None):
        self.planner = planner or MainPlanner()
        self.workers = {}
        self.events = asyncio.Queue(maxsize=256)
        self.device_manager = DeviceManager()
        self.server = None
        self.state = "STARTING"
        self.running = {}
        self.closed_report = None
        self._connections = set()
        self.dispatched_workers = {}
        self.attempt_counts = {}
        self.diagnostic_history = []
        self._closing = False
        self.restart_counts = {}
        self.recovering = set()
        self._invalid_sessions = set()
        self._lifecycle_lock = asyncio.Lock()
        self._event_task = None
        self._monitor_task = None
        self._event_error = None
        self._close_task = None
        self._dependency_cancellations = set()
        self._pending_cancellations = set()

    async def _record_events(self):
        while True:
            role, kind, payload = await self.events.get()
            try:
                self.journal.append({"kind": kind, "role_id": role, "payload": payload})
            except Exception as exc:
                # Keep draining so a disk failure cannot deadlock process reaping.
                self._event_error = exc
            finally:
                self.events.task_done()

    def invalidate_worker(self, worker):
        if worker.session_id in self._invalid_sessions:
            return
        self._invalid_sessions.add(worker.session_id)
        invalidated = self.facts.invalidate_role(worker.role.role_id, worker.session_id)
        invalidated = self.scheduler.invalidate(invalidated)
        self.facts.invalidate_tasks(invalidated)
        for key in invalidated & self.running.keys():
            attempt, future = self.running[key]
            if not future.done():
                owner = self.dispatched_workers[(key, attempt)]
                cancellation = asyncio.create_task(self._cancel_invalidated(owner, key, attempt))
                self._dependency_cancellations.add(cancellation)
                cancellation.add_done_callback(self._dependency_cancellations.discard)
        self.journal.append(
            {"kind": "session_invalidated", **worker.binding, "task_ids": sorted(invalidated)}
        )

    async def _cancel_invalidated(self, worker, task_id, attempt_id):
        try:
            await worker.request(
                "cancel_task", {"reason": "dependency_invalidated"}, task_id, attempt_id, timeout=10
            )
        except Exception:
            # The result remains invalid even when the owner cannot acknowledge
            # cancellation. Its process is still reaped during normal shutdown.
            pass

    async def _monitor_workers(self):
        while not self._closing:
            for worker in list(self.workers.values()):
                if (
                    worker.ready.is_set()
                    and worker.process is not None
                    and worker.process.returncode is None
                    and time.monotonic() - worker.last_heartbeat
                    > self.workflow.heartbeat_timeout_seconds
                    + self.workflow.reconnect_grace_seconds
                ):
                    worker.state = "FAULTED"
                    worker.quarantined = True
                    self.invalidate_worker(worker)
                    await worker.terminate()
            await asyncio.sleep(min(1, self.workflow.heartbeat_interval_seconds))

    async def connect(self, reader, writer):
        task = asyncio.current_task()
        self._connections.add(task)
        conn = Connection(reader, writer)
        try:
            hello = await asyncio.wait_for(conn.receive(), 10)
            worker = self.workers.get(hello.role_id)
            if hello.kind != "hello" or worker is None:
                raise ValueError("Unknown worker")
            await worker.attach(conn, hello)
        except (ValueError, EOFError, ConnectionError, TimeoutError) as exc:
            if hasattr(self, "journal"):
                self.journal.append({"kind": "connection_error", "description": repr(exc)})
            await conn.close()
        finally:
            self._connections.discard(task)

    async def run(self, workflow):
        if hasattr(self, "workflow"):
            raise RuntimeError("Create a new MainSession for each workflow")
        preflight(workflow)
        self.scheduler = Scheduler(workflow)
        self.workflow = workflow
        root = workflow.root / workflow.workflow_id
        if (root / "workflow.json").exists():
            raise ValueError("Existing workflow is read-only; use a new workflow_id")
        root.mkdir(parents=True, exist_ok=True)
        atomic_json(root / "workflow.json", workflow.model_dump(mode="json"))
        self.journal = Journal(root / "events.jsonl")
        self.facts = FactStore(Journal(root / "facts.jsonl"))
        self.server = await asyncio.start_server(
            self.connect, "127.0.0.1", 0, limit=MAX_FRAME_BYTES + 1
        )
        self.host, self.port = self.server.sockets[0].getsockname()[:2]
        self._event_task = asyncio.create_task(self._record_events())
        deadline = time.monotonic() + workflow.max_workflow_seconds
        next_artifact_scan = time.monotonic()
        self.deadline = deadline
        interrupted = None
        try:
            self.workers = {
                role.role_id: WorkerHandle(workflow, role, self.events) for role in workflow.roles
            }
            async with self._lifecycle_lock:
                if self._closing:
                    raise asyncio.CancelledError
                starts = [
                    asyncio.create_task(w.start(self.host, self.port))
                    for w in self.workers.values()
                ]
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*starts),
                        min(
                            workflow.startup_timeout_seconds,
                            max(0.001, deadline - time.monotonic()),
                        ),
                    )
                finally:
                    for start in starts:
                        if not start.done():
                            start.cancel()
                    await asyncio.gather(*starts, return_exceptions=True)
            await asyncio.wait_for(
                asyncio.gather(*(w.ready.wait() for w in self.workers.values())),
                min(workflow.startup_timeout_seconds, max(0.001, deadline - time.monotonic())),
            )
            if any(w.state != "READY" for w in self.workers.values()):
                raise RuntimeError("Worker startup failed; inspect role stderr.log")
            self.state = "RUNNING"
            self._monitor_task = asyncio.create_task(self._monitor_workers())
            if not self.scheduler.tasks:
                await self.replan()
            while time.monotonic() < deadline:
                if self._closing:
                    self.state = "CANCELLED"
                    break
                if self._event_error is not None:
                    raise self._event_error
                for background in (self._event_task, self._monitor_task):
                    if background.done():
                        background.result()
                for worker in self.workers.values():
                    if worker.quarantined or (
                        worker.process and worker.process.returncode is not None
                    ):
                        self.invalidate_worker(worker)
                if self.scheduler.completed:
                    self.state = "COMPLETED"
                    break
                busy_roles = {self.scheduler.tasks[key].role_id for key in self.running}
                for task in self.scheduler.tasks.values():
                    worker = self.workers[task.role_id]
                    if (
                        task.role_id in busy_roles
                        or task.role_id in self.recovering
                        or worker.state not in ("READY",)
                        or not self.scheduler.ready(task)
                    ):
                        continue
                    try:
                        inputs = self.facts.resolve(task.spec.inputs)
                    except (KeyError, ValueError):
                        continue
                    dispatch = task.model_copy(deep=True)
                    dispatch.spec.inputs = inputs
                    dispatch.spec.input_sources = self.facts.provenance(task.spec.inputs)
                    dispatch.spec.constraints = list(
                        dict.fromkeys([*workflow.constraints, *task.spec.constraints])
                    )
                    attempt = new_id()
                    self.journal.append(
                        {
                            "kind": "dispatch",
                            "task_id": task.task_id,
                            "attempt_id": attempt,
                            "spec": dispatch.spec.model_dump(mode="json"),
                        }
                    )
                    self.scheduler.running.add(task.task_id)
                    self.scheduler.executed.add(task.task_id)
                    self.attempt_counts[task.task_id] = self.attempt_counts.get(task.task_id, 0) + 1
                    self.dispatched_workers[(task.task_id, attempt)] = worker
                    self.running[task.task_id] = (
                        attempt,
                        asyncio.create_task(worker.execute(dispatch, attempt)),
                    )
                    busy_roles.add(task.role_id)
                    worker.state = "EXECUTING"
                finished = [
                    (key, attempt, fut)
                    for key, (attempt, fut) in self.running.items()
                    if fut.done()
                ]
                for key, attempt, future in finished:
                    task = self.scheduler.tasks[key]
                    worker = self.dispatched_workers[(key, attempt)]
                    try:
                        message = future.result()
                        if (
                            worker.session_id in self._invalid_sessions
                            or self.workers[task.role_id] is not worker
                            or key in self.scheduler.invalidated
                        ):
                            raise RuntimeError("Result belongs to a retired worker session")
                        result = TaskResult.model_validate(message.payload)
                        if result.status == "succeeded":
                            accept_result(task.spec, result)
                            references = set(result.artifact_refs)
                            references.update(
                                ref for check in result.checks for ref in check.evidence_refs
                            )
                            validate_evidence(
                                workflow.root / workflow.workflow_id / "traces" / worker.session_id,
                                key,
                                attempt,
                                references,
                                result.model_dump(mode="json"),
                            )
                            self.facts.accept(key, attempt, task.role_id, result, worker.session_id)
                        worker.state = "READY" if result.session_reusable else "FAULTED"
                        if not result.session_reusable:
                            worker.quarantined = True
                    except Exception as exc:
                        result = TaskResult(
                            status="unknown",
                            error={"code": "worker_or_result_failed", "description": repr(exc)},
                        )
                        worker.state = "FAULTED"
                        worker.quarantined = True
                    if result.status == "unknown" or not result.session_reusable:
                        self.invalidate_worker(worker)
                    self.scheduler.record_result(key, attempt, result)
                    self.scheduler.running.discard(key)
                    self.running.pop(key)
                    self.journal.append(
                        {
                            "kind": "terminal",
                            "task_id": key,
                            "attempt_id": attempt,
                            "result": result.model_dump(mode="json"),
                        }
                    )
                    if (
                        result.status == "failed"
                        and result.session_reusable
                        and result.side_effect_state != "uncertain"
                        and task.spec.effect_policy in ("read_only", "retry_safe")
                        and self.attempt_counts[key] < workflow.max_attempts_per_task
                    ):
                        self.scheduler.results.pop(key)
                if not self.running and not self.scheduler.completed:
                    if self._pending_cancellations:
                        await asyncio.sleep(0.02)
                        continue
                    if self.recovering:
                        await asyncio.sleep(0.02)
                        continue
                    can_advance = False
                    for pending in self.scheduler.tasks.values():
                        if (
                            self.scheduler.ready(pending)
                            and self.workers[pending.role_id].state == "READY"
                        ):
                            try:
                                self.facts.resolve(pending.spec.inputs)
                                can_advance = True
                                break
                            except (KeyError, ValueError):
                                pass
                    if can_advance:
                        continue
                    revision = self.scheduler.revision
                    await self.replan()
                    if self.scheduler.revision == revision:
                        self.state = "BLOCKED"
                        break
                if time.monotonic() >= next_artifact_scan:
                    total = 0
                    for path in root.rglob("*"):
                        try:
                            if path.is_file():
                                total += path.stat().st_size
                        except FileNotFoundError:
                            pass  # Task archives atomically clear active files.
                    next_artifact_scan = time.monotonic() + 1
                    if total > workflow.max_artifact_bytes:
                        self.state = "FAILED"
                        break
                await asyncio.sleep(0.02)
            else:
                self.state = "FAILED"
            outcome = {
                "business_status": self.state.lower(),
                "tasks": {k: v.model_dump(mode="json") for k, v in self.scheduler.results.items()},
                "recoveries": self.scheduler.recoveries,
                "unresolved_tasks": [
                    k for k in self.scheduler.tasks if k not in self.scheduler.results
                ],
            }
        except asyncio.CancelledError:
            self.state = "CANCELLED"
            interrupted = asyncio.CancelledError()
        except Exception as exc:
            self.state = "FAILED"
            interrupted = exc
        finally:
            cleanup = await self.close(self.state.lower())
        if interrupted is not None:
            outcome = {
                "business_status": self.state.lower(),
                "tasks": {k: v.model_dump(mode="json") for k, v in self.scheduler.results.items()},
                "recoveries": self.scheduler.recoveries,
                "unresolved_tasks": [
                    k for k in self.scheduler.tasks if k not in self.scheduler.results
                ],
                "error": repr(interrupted),
            }
        outcome["invalidated_tasks"] = sorted(self.scheduler.invalidated)
        outcome["expected_cancelled_tasks"] = sorted(self.scheduler.expected_cancelled_tasks)
        outcome["cleanup_status"] = cleanup
        atomic_json(root / "result.json", outcome)
        if interrupted is not None:
            raise interrupted
        return outcome

    async def replan(self):
        async with asyncio.timeout(max(0.001, self.deadline - time.monotonic())):
            return await self._replan()

    async def _replan(self):
        self.diagnostic_history.append(
            {
                "role_states": {
                    key: {
                        "state": w.state,
                        "session_id": w.session_id,
                        "quarantined": w.quarantined,
                    }
                    for key, w in self.workers.items()
                }
            }
        )
        # Capture bounded native checkpoints before asking the model to diagnose a
        # failure. Read-only RPCs never acquire a second device action owner.
        failed_roles = {
            self.scheduler.tasks[key].role_id
            for key, result in self.scheduler.results.items()
            if result.status != "succeeded" or key in self.scheduler.invalidated
        }
        for role in sorted(failed_roles):
            try:
                self.diagnostic_history.append(
                    {"role_id": role, "inspection": await self.inspect(role, {"limit": 10})}
                )
            except (RuntimeError, TimeoutError, ConnectionError) as exc:
                self.diagnostic_history.append({"role_id": role, "error": repr(exc)})
        self.diagnostic_history[:] = self.diagnostic_history[-8:]
        for planning_attempt in range(1, 4):
            try:
                proposal = await asyncio.wait_for(
                    self.planner.propose(
                        self.workflow,
                        self.scheduler,
                        self.facts,
                        diagnostics=self.diagnostic_history,
                    ),
                    min(300, max(0.001, self.deadline - time.monotonic())),
                )
                self.journal.append(
                    {"kind": "plan_proposal", "proposal": proposal.model_dump(mode="json")}
                )
                if proposal.diagnostics:
                    for request in proposal.diagnostics:
                        if request.role_id not in self.workers:
                            raise ValueError("Unknown diagnostic role")
                        try:
                            method = self.inspect if request.kind == "inspect" else self.observe
                            data = await method(request.role_id, request.query)
                        except (RuntimeError, TimeoutError, ConnectionError) as exc:
                            data = {"error": repr(exc)}
                        self.diagnostic_history.append(
                            {"role_id": request.role_id, "kind": request.kind, "data": data}
                        )
                    self.diagnostic_history[:] = self.diagnostic_history[-8:]
                    continue
                if proposal.missing_input:
                    return
                # Validate the complete proposal before performing any restart.
                candidate = copy.deepcopy(self.scheduler)
                candidate.apply(proposal.tasks, proposal.base_plan_revision, proposal.recoveries)
                for role in proposal.restart_roles:
                    if role not in self.workers or self.workers[role].state not in (
                        "FAULTED",
                        "EXITED",
                    ):
                        raise ValueError("Only a failed or exited role may be restarted")
                    if self.restart_counts.get(role, 0) >= self.workflow.max_attempts_per_task:
                        raise ValueError("Role restart budget exhausted")
                for role in dict.fromkeys(proposal.restart_roles):
                    self.restart_counts[role] = self.restart_counts.get(role, 0) + 1
                    await self.takeover(role, "planner_recovery")
                if proposal.tasks or proposal.recoveries:
                    self.scheduler.apply(
                        proposal.tasks, proposal.base_plan_revision, proposal.recoveries
                    )
                    atomic_json(
                        self.workflow.root / self.workflow.workflow_id / "plan.json",
                        {
                            "revision": self.scheduler.revision,
                            "tasks": [
                                t.model_dump(mode="json") for t in self.scheduler.tasks.values()
                            ],
                            "recoveries": self.scheduler.recoveries,
                        },
                    )
                return
            except (ValueError, TimeoutError) as exc:
                rejection = {
                    "kind": "plan_rejected" if isinstance(exc, ValueError) else "plan_timeout",
                    "planning_attempt": planning_attempt,
                    "base_plan_revision": self.scheduler.revision,
                    "error": str(exc)[:4000] or type(exc).__name__,
                }
                self.journal.append(rejection)
                self.diagnostic_history.append(rejection)
                self.diagnostic_history[:] = self.diagnostic_history[-8:]
                continue

    async def inspect(self, role_id, query=None):
        if self.workers[role_id].process.returncode is not None:
            return self.archived_inspection(role_id)
        reply = await self.workers[role_id].request("inspect_session", query or {}, timeout=10)
        return reply.payload

    def archived_inspection(self, role_id):
        worker = self.workers[role_id]
        root = self.workflow.root / self.workflow.workflow_id / "traces" / worker.session_id
        records = []
        for path in sorted(root.glob("tasks/*/*/result.json"))[-20:]:
            records.append(
                {
                    "artifact_ref": str(path.relative_to(root)),
                    "result": json.loads(path.read_text(encoding="utf-8")),
                }
            )
        return {
            "source": worker.binding,
            "consistency": "committed_artifacts",
            "state": worker.state,
            "results": records,
            "limitation": "Dead worker memory is unavailable",
        }

    async def observe(self, role_id, query=None):
        reply = await self.workers[role_id].request("observe_session", query or {}, timeout=10)
        return reply.payload

    async def cancel(self, task_id, reason="cancelled"):
        attempt, _ = self.running[task_id]
        worker = self.dispatched_workers[(task_id, attempt)]
        identity = (task_id, attempt)
        self._pending_cancellations.add(identity)
        try:
            reply = await worker.request(
                "cancel_task", {"reason": reason}, task_id, attempt, timeout=10
            )
        finally:
            self._pending_cancellations.discard(identity)
        if reply.payload.get("accepted") is True:
            self.scheduler.authorized_cancellations.add((task_id, attempt))
            self.journal.append({"kind": "cancel_authorized", "task_id": task_id, "attempt_id": attempt})
        return reply.payload

    async def takeover(self, role_id, reason="recovery"):
        if role_id in self.recovering:
            raise RuntimeError("Role recovery already in progress")
        self.recovering.add(role_id)
        try:
            async with self._lifecycle_lock:
                if self._closing:
                    raise RuntimeError("Main session is closing")
                return await self._takeover(role_id, reason)
        finally:
            self.recovering.discard(role_id)

    async def _takeover(self, role_id, reason):
        old = self.workers[role_id]
        self.invalidate_worker(old)
        old.quarantined = True
        replacement = await self.device_manager.takeover(old, self.host, self.port)
        self.workers[role_id] = replacement
        try:
            await replacement.start(self.host, self.port)
            await asyncio.wait_for(replacement.ready.wait(), self.workflow.startup_timeout_seconds)
            if replacement.state != "READY":
                raise RuntimeError("Replacement failed to acquire the device")
            observed = await self.observe(role_id, {"mode": "fresh"})
        except BaseException:
            replacement.quarantined = True
            replacement.state = "FAULTED"
            await replacement.close("takeover_failed")
            raise
        self.journal.append(
            {
                "kind": "takeover",
                "role_id": role_id,
                "reason": reason,
                "previous_session_id": old.session_id,
                "session_id": replacement.session_id,
                "observation": observed,
            }
        )
        self.diagnostic_history.append({"role_id": role_id, "takeover_observation": observed})
        return {"session_id": replacement.session_id, "epoch": replacement.epoch}

    async def close(self, reason="close"):
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._locked_close(reason))
        cancelled = False
        while True:
            try:
                report = await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError:
                if self._close_task.done():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return report

    async def _locked_close(self, reason):
        async with self._lifecycle_lock:
            return await self._close(reason)

    async def _close(self, reason):
        if self.closed_report is not None:
            return self.closed_report
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
        reports = await asyncio.gather(
            *(w.close(reason) for w in self.workers.values()), return_exceptions=True
        )
        for _, future in self.running.values():
            if not future.done():
                future.cancel()
        if self.running:
            await asyncio.gather(*(f for _, f in self.running.values()), return_exceptions=True)
        for cancellation in list(self._dependency_cancellations):
            cancellation.cancel()
        if self._dependency_cancellations:
            await asyncio.gather(*self._dependency_cancellations, return_exceptions=True)
        if self.server:
            self.server.close()
        for task in list(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        if self.server:
            await asyncio.wait_for(self.server.wait_closed(), 5)
        if self._event_task is not None:
            if not self._event_task.done():
                await self.events.join()
                self._event_task.cancel()
            event_result = await asyncio.gather(self._event_task, return_exceptions=True)
            if isinstance(event_result[0], Exception):
                reports.append(event_result[0])
        self.closed_report = [str(r) if isinstance(r, BaseException) else r for r in reports]
        if self._event_error is not None:
            self.closed_report.append(
                {"errors": [repr(self._event_error)], "source": "event_journal"}
            )
        return self.closed_report
