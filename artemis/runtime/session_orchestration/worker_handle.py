"""Parent-side worker process handle and durable result receipt."""

from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from artemis.runtime.session_orchestration.models import Envelope, new_id
from artemis.runtime.session_orchestration.artifacts import atomic_json, digest
from artemis.runtime.session_orchestration.transport import Connection, MAX_FRAME_BYTES


class WorkerHandle:
    def __init__(self, workflow, role, events):
        self.workflow, self.role, self.events = workflow, role, events
        self.session_id, self.epoch = new_id(), new_id()
        self.token = secrets.token_urlsafe(32)
        self.reconnect_secret = secrets.token_urlsafe(32)
        self.authenticated = False
        self.sequence = 0
        self.process = None
        self.created_at = None
        self.conn = None
        self.connected = asyncio.Event()
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_report = None
        self.state = "STARTING"
        self.quarantined = False
        self.last_heartbeat = time.monotonic()
        self.last_status = {}
        self.pending = {}
        self.inflight = {}
        self.abandoned = {}
        self._close_lock = asyncio.Lock()
        self._exit_task = None
        self._closing = False
        self.readers = set()
        self.log_tasks = []
        self.root = workflow.root / workflow.workflow_id / "roles" / role.role_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.binding = {
            "workflow_id": workflow.workflow_id,
            "role_id": role.role_id,
            "child_session_id": self.session_id,
            "worker_epoch": self.epoch,
        }

    async def start(self, host, port):
        if self.process is not None or self._closing:
            raise RuntimeError("Worker cannot be started twice or after close")
        # Retain ownership when cancellation races with OS process creation.
        start = asyncio.create_task(self._start(host, port))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(start)
                break
            except asyncio.CancelledError:
                if start.done():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _start(self, host, port):
        source_root = Path(__file__).resolve().parents[3]
        env = os.environ.copy()
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "PYTHONUTF8": "1",
                "PYTHONPATH": str(source_root) + os.pathsep + env.get("PYTHONPATH", ""),
                "ARTEMIS_TRACES_DIR": str(
                    self.workflow.root / self.workflow.workflow_id / "traces"
                ),
            }
        )
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "artemis.runtime.session_orchestration.worker",
            cwd=source_root,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        import psutil

        self.created_at = psutil.Process(self.process.pid).create_time()
        bootstrap = {
            **self.binding,
            "token": self.token,
            "host": host,
            "port": port,
            "device_id": self.role.device_id,
            "profile": self.role.profile,
            "role_goal": self.role.description or self.workflow.goal,
            "app_path": str(self.role.app_path) if self.role.app_path else None,
            "locked_app_package": self.role.locked_app_package,
            "traces_root": env["ARTEMIS_TRACES_DIR"],
            "fake_worker": self.workflow.fake_worker,
            "heartbeat_interval_seconds": self.workflow.heartbeat_interval_seconds,
            "reconnect_grace_seconds": self.workflow.reconnect_grace_seconds,
            "close_timeout_seconds": self.workflow.close_timeout_seconds,
        }
        self.log_tasks = [
            asyncio.create_task(self.drain_log(self.process.stdout, "stdout.log")),
            asyncio.create_task(self.drain_log(self.process.stderr, "stderr.log")),
        ]
        self._exit_task = asyncio.create_task(self.watch_exit())
        self.process.stdin.write(json.dumps(bootstrap).encode() + b"\n")
        await asyncio.wait_for(self.process.stdin.drain(), 10)
        self.process.stdin.close()
        atomic_json(
            self.root / "worker.json",
            {**self.binding, "pid": self.process.pid, "created_at": self.created_at},
        )

    async def drain_log(self, reader, name):
        path = self.root / name
        while data := await reader.read(8192):
            if path.exists() and path.stat().st_size > 1024 * 1024:
                path.replace(path.with_suffix(".previous"))
            with path.open("ab") as handle:
                handle.write(data)

    async def watch_exit(self):
        code = await self.process.wait()
        self.state = "EXITED"
        self.ready.set()
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(RuntimeError(f"worker_lost: exit={code}"))
        await self.events.put((self.role.role_id, "exited", code))

    def message(self, kind, payload=None, task=None, attempt=None, reply=None):
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

    async def attach(self, conn, hello):
        expected = self.reconnect_secret if self.authenticated else self.token
        token = hello.payload.get("token", "")
        if not isinstance(token, str) or not secrets.compare_digest(token, expected):
            raise ValueError("Invalid handshake token")
        if any(getattr(hello, key) != val for key, val in self.binding.items()):
            raise ValueError("Wrong worker identity")
        previous = self.conn
        self.conn = conn
        self.authenticated = True
        try:
            await conn.send(
                self.message(
                    "ack", {"reconnect_secret": self.reconnect_secret}, reply=hello.message_id
                )
            )
            if previous:
                await previous.close()
            self.connected.set()
            self.last_heartbeat = time.monotonic()
            # A dispatch lost on a broken connection is retried with its original identity.
            for request in (
                [] if self.quarantined or self._closing else list(self.inflight.values())
            ):
                await conn.send(request)
            while True:
                message = await conn.receive()
                if any(getattr(message, k) != v for k, v in self.binding.items()):
                    raise ValueError("Wrong reply identity")
                self.last_heartbeat = time.monotonic()
                if (
                    message.kind in ("heartbeat", "session_status", "ready")
                    and not self.quarantined
                ):
                    self.state = message.payload.get("state", self.state)
                    self.last_status = {"observed_monotonic": self.last_heartbeat, **message.payload}
                if message.kind == "ready":
                    self.ready.set()
                if message.kind == "closed":
                    self.close_report = message.payload
                    self.closed.set()
                if message.kind == "result":
                    if message.attempt_id not in self.inflight:
                        # Keep duplicates only if the original result is already durable.
                        path = (
                            self.workflow.root
                            / self.workflow.workflow_id
                            / "tasks"
                            / message.task_id
                            / message.attempt_id
                            / "result.json"
                        )
                        abandoned = self.abandoned.get(message.attempt_id)
                        if not path.exists() and (
                            abandoned is None or abandoned.task_id != message.task_id
                        ):
                            raise ValueError("Unsolicited result")
                    else:
                        request = self.inflight[message.attempt_id]
                        if message.task_id != request.task_id:
                            raise ValueError("Result belongs to another task")
                        path = (
                            self.workflow.root
                            / self.workflow.workflow_id
                            / "tasks"
                            / message.task_id
                            / message.attempt_id
                            / "result.json"
                        )
                    if (
                        path.exists()
                        and json.loads(path.read_text(encoding="utf-8")) != message.payload
                    ):
                        raise ValueError("Conflicting duplicate result")
                    atomic_json(path, message.payload)
                    await conn.send(
                        self.message(
                            "ack",
                            {"result_hash": digest(message.payload)},
                            reply=message.message_id,
                        )
                    )
                future = self.pending.get(message.in_reply_to)
                if future and not future.done():
                    if message.kind in ("result", "observation", "inspection", "cancel_received"):
                        future.set_result(message)
                    elif message.kind in ("error", "rejected"):
                        future.set_exception(RuntimeError(str(message.payload)))
                if message.kind != "heartbeat":
                    await self.events.put((self.role.role_id, message.kind, message.payload))
        except (EOFError, ConnectionError, OSError) as exc:
            await self.events.put((self.role.role_id, "connection_lost", {"error": repr(exc)}))
        finally:
            if self.conn is conn:
                self.connected.clear()
            await conn.close()

    async def request(self, kind, payload=None, task=None, attempt=None, timeout=30):
        if kind == "execute" and (self.quarantined or self._closing):
            raise RuntimeError("Worker is quarantined or closing")
        message = self.message(kind, payload, task, attempt)
        future = asyncio.get_running_loop().create_future()
        self.pending[message.message_id] = future
        if kind == "execute":
            self.inflight[attempt] = message
            atomic_json(
                self.workflow.root
                / self.workflow.workflow_id
                / "tasks"
                / task
                / attempt
                / "request.json",
                message.model_dump(mode="json"),
            )
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            while not future.done():
                await asyncio.wait_for(
                    self.connected.wait(), max(0.001, deadline - asyncio.get_running_loop().time())
                )
                connection = self.conn
                try:
                    await connection.send(message)
                    break
                except (ConnectionError, OSError):
                    if self.conn is connection:
                        self.connected.clear()
                    await connection.close()
            return await asyncio.wait_for(
                asyncio.shield(future), max(0.001, deadline - asyncio.get_running_loop().time())
            )
        finally:
            self.pending.pop(message.message_id, None)
            if kind == "execute":
                self.inflight.pop(attempt, None)
                if not future.done() or future.cancelled() or future.exception() is not None:
                    self.abandoned[attempt] = message
                    self.quarantined = True
                    self.state = "FAULTED"
            if not future.done():
                future.cancel()

    async def execute(self, task, attempt):
        deadline = (
            task.spec.execution_timeout_seconds
            + task.spec.verification_timeout_seconds
            + task.spec.settlement_timeout_seconds * 2
            + 15
        )
        return await self.request(
            "execute", task.spec.model_dump(mode="json"), task.task_id, attempt, deadline
        )

    async def close(self, reason):
        self._closing = True
        async with self._close_lock:
            return await self._close(reason)

    async def _close(self, reason):
        if self.process is None:
            return {"errors": []}
        if self.process.returncode is None:
            if self.connected.is_set():
                try:
                    await self.conn.send(self.message("close_session", {"reason": reason}))
                except (ConnectionError, OSError, TimeoutError):
                    pass
            try:
                await asyncio.wait_for(self.process.wait(), self.workflow.close_timeout_seconds)
            except TimeoutError:
                await self.terminate()
        if self.conn is not None:
            await self.conn.close()
        if self._exit_task is not None:
            await self._exit_task
        try:
            await asyncio.wait_for(asyncio.gather(*self.log_tasks, return_exceptions=True), 5)
        except TimeoutError:
            raise RuntimeError("Worker log pipes remain open after process exit")
        return {
            "exited": self.process.returncode is not None,
            "returncode": self.process.returncode,
            "session": self.close_report,
            "graceful": bool(
                self.process.returncode == 0
                and self.close_report is not None
                and not self.close_report.get("errors")
            ),
        }

    async def terminate(self):
        from artemis.runtime.supervisor import ProcessSupervisor

        stopped = await asyncio.to_thread(
            ProcessSupervisor.terminate_tree_verified, self.process.pid, self.created_at
        )
        if not stopped:
            raise RuntimeError("Worker termination not verified")
        await asyncio.wait_for(self.process.wait(), 5)
