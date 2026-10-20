"""Role-owned native storage with task-local cursors and provenance.

Adapted from 788bc0f's RoleDataEngine; native storage schemas stay unchanged.
"""

from contextvars import copy_context
from dataclasses import replace

from artemis.data_engine.engine import DataEngine
from artemis.runtime.execution_scope import CURRENT_EXECUTION, execution_scope, set_scoped_step
from artemis.runtime.background_work import BackgroundWork


class SessionDataEngine(DataEngine):
    is_managed_session = True

    def __init__(self, ctx):
        self._write_errors = []
        self._reported_write_errors = 0
        self.task_scopes = {}
        # Each Role owns its log consumer and IPC sender. Slow telemetry must
        # never occupy another Role's consumer or the shared asyncio executor.
        self._logs = BackgroundWork("role-logs", self._write_errors.append)
        self._events = BackgroundWork("role-events", self._write_errors.append)
        super().__init__(ctx)

    def _scope(self):
        scope = CURRENT_EXECUTION.get()
        return scope if scope is not None and scope.engine is self else None

    @property
    def current_step_id(self):
        scope = self._scope()
        return scope.step_id if scope is not None else getattr(self, "_current_step_id", None)

    @current_step_id.setter
    def current_step_id(self, value):
        if self._scope() is not None:
            set_scoped_step(value)
        else:
            self._current_step_id = value

    @property
    def last_recorded_step_id(self):
        scope = self._scope()
        if scope is not None and scope.control is not None:
            return scope.control.last_recorded_step_id
        return getattr(self, "_last_recorded_step_id", None)

    @last_recorded_step_id.setter
    def last_recorded_step_id(self, value):
        scope = self._scope()
        if scope is not None and scope.control is not None:
            scope.control.last_recorded_step_id = value
        else:
            self._last_recorded_step_id = value

    @property
    def current_step_dir(self):
        scope = self._scope()
        if scope is not None and scope.control is not None:
            return scope.control.current_step_dir
        return getattr(self, "_current_step_dir", None)

    @current_step_dir.setter
    def current_step_dir(self, value):
        scope = self._scope()
        if scope is not None and scope.control is not None:
            scope.control.current_step_dir = value
        else:
            self._current_step_dir = value

    def _publish(self, event_type, data):
        scope = self._scope()
        if scope is not None and isinstance(data, dict):
            data = {**data, **scope.metadata()}
        self._events.submit(super()._publish, event_type, data)

    def enqueue_log(self, entry):
        from third_party.mobile_use.utils.logger import record_log_entry

        self._logs.submit(record_log_entry, entry)

    async def flush_logs(self):
        await self._logs.flush()

    def has_pending_operations(self):
        return self._logs.pending or self._events.pending or super().has_pending_operations()

    def record_step(self, *args, extra_metadata=None, **kwargs):
        if len(args) > 10:
            extra_metadata, args = args[10], args[:10]
        metadata = dict(extra_metadata or {})
        scope = self._scope()
        if scope is not None:
            metadata.update(scope.metadata())
        return super().record_step(*args, extra_metadata=metadata, **kwargs)

    def record_trace(self, type, name, payload, **kwargs):
        scope = self._scope()
        if scope is not None:
            payload = {**payload, "execution": scope.metadata()}
        return super().record_trace(type, name, payload, **kwargs)

    def _run_in_background(self, fn, *args):
        context = copy_context()

        def write():
            try:
                return context.run(fn, *args)
            except Exception as exc:
                self._write_errors.append(exc)
                raise

        return super()._run_in_background(write)

    async def flush(self):
        while True:
            await self._logs.flush()
            await super().flush()
            await self._events.flush()
            if not (self._logs.pending or self._events.pending or self.has_pending_operations()):
                break
        if len(self._write_errors) > self._reported_write_errors:
            error = self._write_errors[self._reported_write_errors]
            self._reported_write_errors = len(self._write_errors)
            raise RuntimeError("Session storage write failed") from error

    async def shutdown(self):
        await super().shutdown()
        # Native shutdown emits a final log; drain it before retiring consumers.
        await self.flush()
        self._logs.close()
        self._events.close()

    def get_session_steps(self):
        return super().get_agent_friendly_steps()

    def _task_steps(self, steps):
        scope = self._scope()
        if scope is None or scope.task_id is None or scope.phase == "memory":
            return steps
        return [
            step
            for step in steps
            if (step.get("extra_metadata") or {}).get("task_id") == scope.task_id
            and (step.get("extra_metadata") or {}).get("attempt_id") == scope.attempt_id
        ]

    def get_agent_friendly_steps(self):
        return self._task_steps(super().get_agent_friendly_steps())

    def get_agent_friendly_steps_in_range(self, start_step, end_step=None):
        return self._task_steps(super().get_agent_friendly_steps_in_range(start_step, end_step))

    def get_step_record(self, step_number):
        record = super().get_step_record(step_number)
        scope = self._scope()
        if record is not None and scope is not None and scope.task_id and scope.phase != "memory":
            meta = record.extra_metadata or {}
            if (meta.get("task_id"), meta.get("attempt_id")) != (scope.task_id, scope.attempt_id):
                return None
        return record

    def get_history_chunks(self, *args, **kwargs):
        rows = super().get_history_chunks(*args, **kwargs)
        scope = self._scope()
        if scope is None or scope.task_id is None or scope.phase == "memory":
            return rows
        allowed = {str(step["step_id"]) for step in self.get_agent_friendly_steps()}
        return [row for row in rows if row.source_step_ids and set(row.source_step_ids) <= allowed]

    def scope_for_steps(self, steps):
        if not steps:
            return None
        meta = steps[0].get("extra_metadata") or {}
        return self.task_scopes.get((meta.get("task_id"), meta.get("attempt_id")))

    def memory_scope(self):
        scope = self._scope()
        return execution_scope(replace(scope, phase="memory")) if scope else execution_scope(None)
