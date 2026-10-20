"""One authenticated, persistent Role process. Never shares a native Context."""

from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from artemis.runtime.session_orchestration.models import Envelope, TaskSpec, TaskResult, now
from artemis.runtime.session_orchestration.artifacts import (
    AttemptStore,
    atomic_json,
    digest,
    require_clean_boundary,
)
from artemis.runtime.session_orchestration.contract import (
    ContractVerifier,
    output_model,
    render_goal,
)
from artemis.runtime.session_orchestration.diagnostics import Diagnostics
from artemis.runtime.session_orchestration.transport import Connection, MAX_FRAME_BYTES


class Worker:
    def __init__(self, bootstrap):
        self.config = bootstrap
        self.binding = {
            k: bootstrap[k] for k in ("workflow_id", "role_id", "child_session_id", "worker_epoch")
        }
        self.secret = bootstrap["token"]
        self.reconnecting = False
        self.conn = None
        self.connected = asyncio.Event()
        self.stop = asyncio.Event()
        self.sequence = 0
        self.session = None
        self.active = None
        self.active_identity = None
        self.pending_results = {}
        self.seen_requests = {}
        self.active_hash = None
        self.query_tasks = set()
        self.diagnostics = Diagnostics()
        self.state = "STARTING"
        self.close_reason = "main_disconnected"
        self._execution_started = False
        self._cancel_before_start = False

    def message(self, kind, payload=None, reply=None, task=None, attempt=None):
        self.sequence += 1
        return Envelope(
            kind=kind,
            sequence=self.sequence,
            **self.binding,
            task_id=task,
            attempt_id=attempt,
            in_reply_to=reply,
            payload=payload or {},
        )

    async def send(self, kind, payload=None, reply=None, task=None, attempt=None):
        msg = self.message(kind, payload, reply, task, attempt)
        if kind == "result":
            self.pending_results[msg.message_id] = msg
        deadline = asyncio.get_running_loop().time() + self.config["reconnect_grace_seconds"]
        while True:
            await asyncio.wait_for(
                self.connected.wait(), max(0.001, deadline - asyncio.get_running_loop().time())
            )
            connection = self.conn
            try:
                await connection.send(msg)
                return msg
            except (ConnectionError, OSError):
                if self.conn is connection:
                    self.connected.clear()
                    await connection.close()
                if asyncio.get_running_loop().time() >= deadline:
                    raise

    async def connect(self):
        reader, writer = await asyncio.open_connection(
            self.config["host"], self.config["port"], limit=MAX_FRAME_BYTES + 1
        )
        conn = Connection(reader, writer)
        hello = self.message("hello", {"token": self.secret, "reconnect": self.reconnecting})
        await conn.send(hello)
        response = await asyncio.wait_for(conn.receive(), 10)
        if (
            response.kind != "ack"
            or response.in_reply_to != hello.message_id
            or any(getattr(response, k) != v for k, v in self.binding.items())
        ):
            await conn.close()
            raise ValueError("Handshake rejected")
        self.secret = response.payload["reconnect_secret"]
        self.reconnecting = True
        self.conn = conn
        self.connected.set()

    async def controls(self):
        while not self.stop.is_set():
            try:
                message = await self.conn.receive()
                if any(getattr(message, k) != v for k, v in self.binding.items()):
                    raise ValueError("identity_mismatch")
                fingerprint = digest(message.model_dump(mode="json"))
                previous = self.seen_requests.get(message.message_id)
                if previous is not None and previous != fingerprint:
                    raise ValueError("message_payload_conflict")
                self.seen_requests[message.message_id] = fingerprint
                if message.kind == "execute":
                    try:
                        await self.dispatch(message)
                    except (ValueError, OSError) as exc:
                        if isinstance(exc, OSError) and self.session is not None:
                            self.session.state = "FAULTED"
                        await self.send(
                            "rejected",
                            {
                                "code": "invalid_contract"
                                if isinstance(exc, ValueError)
                                else "persistence_failed",
                                "description": str(exc),
                            },
                            message.message_id,
                            message.task_id,
                            message.attempt_id,
                        )
                elif message.kind == "cancel_task":
                    result = self.cancel(message.task_id, message.attempt_id)
                    await self.send(
                        "cancel_received",
                        result,
                        message.message_id,
                        message.task_id,
                        message.attempt_id,
                    )
                elif message.kind == "close_session":
                    self.close_reason = message.payload.get("reason", "main_closed")
                    self.stop.set()
                elif message.kind == "ack":
                    result = self.pending_results.get(message.in_reply_to)
                    if result is not None and message.payload.get("result_hash") == digest(
                        result.payload
                    ):
                        self.pending_results.pop(message.in_reply_to, None)
                elif message.kind in ("inspect_session", "observe_session"):
                    # A bounded child task keeps cancel/close responsive.
                    if len(self.query_tasks) >= 16:
                        await self.send("error", {"code": "diagnostic_busy"}, message.message_id)
                    else:
                        query_task = asyncio.create_task(self.query(message))
                        self.query_tasks.add(query_task)
                        query_task.add_done_callback(self.query_tasks.discard)
                else:
                    await self.send("error", {"code": "unexpected_message"}, message.message_id)
            except (EOFError, ConnectionError, OSError) as exc:
                print("worker control connection lost:", repr(exc), file=sys.stderr, flush=True)
                self.connected.clear()
                await self.conn.close()
                deadline = (
                    asyncio.get_running_loop().time() + self.config["reconnect_grace_seconds"]
                )
                while not self.stop.is_set():
                    try:
                        await self.connect()
                        for result in list(self.pending_results.values()):
                            await self.conn.send(result)
                        if self.session:
                            await self.send("session_status", {"state": self.session.state})
                        break
                    except (OSError, EOFError, TimeoutError):
                        if asyncio.get_running_loop().time() >= deadline:
                            self.stop.set()
                            break
                        await asyncio.sleep(0.2)
            except Exception as exc:
                print("worker protocol failure:", repr(exc), file=sys.stderr, flush=True)
                try:
                    await self.send("error", {"code": "protocol_error", "description": str(exc)})
                finally:
                    self.stop.set()

    async def query(self, msg):
        try:
            if self.session is None:
                raise RuntimeError("Session not ready")
            if msg.kind == "inspect_session":
                payload = await self.diagnostics.inspect(self.session, msg.payload)
                kind = "inspection"
            else:
                payload = await self.session.observe(msg.payload)
                kind = "observation"
                # Never put image bytes in the control frame.
                if "screenshot_b64" in payload:
                    import base64
                    from artemis.runtime.session_orchestration.models import new_id

                    folder = self.root / "diagnostics" / new_id()
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / "screen.jpg").write_bytes(
                        base64.b64decode(payload.pop("screenshot_b64"))
                    )
                    payload["screenshot_ref"] = str((folder / "screen.jpg").relative_to(self.root))
                payload["captured_at"] = now()
            payload["source"] = {
                **self.binding,
                "active_task_id": self.active_identity[0] if self.active_identity else None,
                "active_attempt_id": self.active_identity[1] if self.active_identity else None,
            }
            await self.send(kind, payload, msg.message_id)
        except Exception as exc:
            await self.send(
                "error", {"code": "diagnostic_unavailable", "description": str(exc)}, msg.message_id
            )

    async def dispatch(self, msg):
        spec = TaskSpec.model_validate(msg.payload)
        if self.active and not self.active.done():
            if self.active_identity == (msg.task_id, msg.attempt_id):
                if digest(msg.payload) != self.active_hash:
                    raise ValueError("attempt_payload_conflict")
                await self.send("accepted", {}, msg.message_id, msg.task_id, msg.attempt_id)
            else:
                await self.send(
                    "rejected", {"code": "busy"}, msg.message_id, msg.task_id, msg.attempt_id
                )
            return
        if self.session is None or self.session.state != "READY":
            await self.send(
                "rejected", {"code": "not_ready"}, msg.message_id, msg.task_id, msg.attempt_id
            )
            return
        store = AttemptStore(self.root, msg.task_id, msg.attempt_id)
        existed = (store.path / "accepted.json").exists()
        first = store.accept(msg.payload)
        saved = store.saved_result()
        if saved is not None:
            await self.send("result", saved, msg.message_id, msg.task_id, msg.attempt_id)
            return
        if existed and not first:
            await self.send("error", {"code": "accepted_attempt_without_result"}, msg.message_id)
            return
        require_clean_boundary(self.root)
        self.active_identity = (msg.task_id, msg.attempt_id)
        self.active_hash = digest(msg.payload)
        self._execution_started = False
        self._cancel_before_start = False
        self.active = asyncio.create_task(self.execute(msg, spec, store))

    def cancel(self, task_id, attempt_id):
        if (
            self.active_identity != (task_id, attempt_id)
            or self.active is None
            or self.active.done()
        ):
            return {"accepted": False, "code": "stale_attempt"}
        if not self._execution_started:
            self._cancel_before_start = True
            return {"accepted": True}
        return self.session.request_cancel(task_id, attempt_id)

    async def execute(self, msg, spec, store):
        async def watchdog():
            await asyncio.sleep(spec.execution_timeout_seconds)
            if self.session.state == "EXECUTING":
                self.session.request_cancel(msg.task_id, msg.attempt_id)

        guard = None
        try:
            await self.send("accepted", {}, msg.message_id, msg.task_id, msg.attempt_id)
            await self.send("started", {}, msg.message_id, msg.task_id, msg.attempt_id)
            guard = asyncio.create_task(watchdog())
            self._execution_started = True
            if self._cancel_before_start:
                result = TaskResult(
                    status="cancelled", side_effect_state="none", session_reusable=True
                )
                store.finish(result.model_dump(mode="json"))
            elif self.config.get("fake_worker", False):
                result = await self.session.execute_spec(spec, store, msg.task_id, msg.attempt_id)
            else:
                from artemis.runtime.session_scope import TaskScope
                from third_party.mobile_use.sdk.types.task import TaskRequest

                request = TaskRequest(
                    goal=render_goal(spec),
                    profile=self.config["profile"],
                    max_steps=spec.max_steps,
                    output_format=output_model(spec.outputs_schema),
                )
                adapter = ContractVerifier(spec, store)
                self.session.options.settlement_timeout_seconds = spec.settlement_timeout_seconds
                await self.session.execute(
                    request,
                    TaskScope(msg.task_id, msg.attempt_id, request.goal),
                    completion=adapter,
                )
                result = adapter.result
            await self.send(
                "result",
                result.model_dump(mode="json"),
                msg.message_id,
                msg.task_id,
                msg.attempt_id,
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            # The execute/settlement failed. No successful business result is invented.
            self.state = "FAULTED"
            self.session.state = "FAULTED"
            await self.send(
                "error",
                {"code": "worker_execution_failed", "description": repr(exc)},
                msg.message_id,
                msg.task_id,
                msg.attempt_id,
            )
        finally:
            if guard is not None:
                guard.cancel()
                await asyncio.gather(guard, return_exceptions=True)
            self.active_identity = None

    async def heartbeat(self):
        while not self.stop.is_set():
            await asyncio.sleep(self.config["heartbeat_interval_seconds"])
            try:
                await self.send(
                    "heartbeat",
                    {
                        "state": self.session.state if self.session else "STARTING",
                        "active_attempt": self.active_identity,
                        "pending_results": len(self.pending_results),
                    },
                )
            except (OSError, ConnectionError, TimeoutError):
                pass

    async def run(self):
        await self.connect()
        controls = asyncio.create_task(self.controls())
        pulse = asyncio.create_task(self.heartbeat())
        self.root = Path(self.config["traces_root"]) / self.binding["child_session_id"]
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            if self.config.get("fake_worker", False):
                from artemis.runtime.session_orchestration.testing import FakeSession

                self.session = FakeSession(self.root)
            else:
                from artemis.sdk.agent import Agent
                from artemis.sdk.persistent_session import SessionOptions

                agent = Agent(
                    device_id=self.config["device_id"], session_id=self.binding["child_session_id"]
                )
                self.session = agent.open_session(
                    SessionOptions(
                        session_id=self.binding["child_session_id"],
                        profile=self.config["profile"],
                        goal=self.config["role_goal"],
                        traces_path=Path(self.config["traces_root"]),
                        app_path=Path(self.config["app_path"])
                        if self.config.get("app_path")
                        else None,
                        locked_app_package=self.config.get("locked_app_package"),
                    )
                )
                await self.session.__aenter__()
            self.state = "READY"
            await self.send("ready", {"state": "READY"})
            await self.stop.wait()
        finally:
            for query in list(self.query_tasks):
                query.cancel()
            if self.query_tasks:
                await asyncio.gather(*self.query_tasks, return_exceptions=True)
            if self.active is not None and not self.active.done():
                if self.active_identity is not None:
                    self.cancel(*self.active_identity)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self.active), self.config.get("close_timeout_seconds", 45)
                    )
                except Exception:
                    self.session.state = "FAULTED"
            try:
                if self.active is not None and not self.active.done():
                    raise RuntimeError("Active attempt did not settle before close deadline")
                report = (
                    await self.session.close(self.close_reason) if self.session else {"errors": []}
                )
            except Exception as exc:
                report = {"errors": [repr(exc)], "lease_released": False}
            try:
                try:
                    await self.send("closed", report)
                except (OSError, ConnectionError, TimeoutError):
                    pass
                if report.get("errors"):
                    # Do not normally exit and orphan operations. Give Main a
                    # bounded opportunity to reap us, then terminate our own tree
                    # even when Main is gone. No graceful lease release is claimed.
                    await asyncio.sleep(min(5, self.config.get("close_timeout_seconds", 45)))
                    await self.terminate_self()
            finally:
                controls.cancel()
                pulse.cancel()
                await asyncio.gather(controls, pulse, return_exceptions=True)
                if self.conn:
                    await self.conn.close()

    async def terminate_self(self):
        import os
        import psutil

        owner = psutil.Process(os.getpid())
        # Freeze asyncio producers during this final synchronous teardown. Kill
        # and reap descendants before killing their owner (also on POSIX).
        try:
            children = owner.children(recursive=True)
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(children, timeout=3)
            if not alive:
                owner.kill()
        except (psutil.Error, OSError) as exc:
            print("Emergency process cleanup failed:", repr(exc), file=sys.stderr, flush=True)
        # A failed platform termination must retain the quarantined owner rather
        # than release its lease with potentially live device operations.
        await asyncio.Event().wait()


def main():
    raw = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1)
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("Bootstrap too large")
    bootstrap = json.loads(raw)
    asyncio.run(Worker(bootstrap).run())


if __name__ == "__main__":
    main()
