"""Role-scoped evidence on the native DataEngine storage and event pipeline."""

from contextvars import copy_context

from artemis.data_engine.engine import DataEngine
from artemis.runtime.execution_scope import CURRENT_EXECUTION, set_scoped_step


class RoleDataEngine(DataEngine):
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

    def _publish(self, event_type, data):
        scope = self._scope()
        if scope is not None and isinstance(data, dict):
            data = {**scope.metadata(), **data}
        return super()._publish(event_type, data)

    def record_step(self, *args, extra_metadata=None, **kwargs):
        scope = self._scope()
        if len(args) > 10:
            # Preserve DataEngine's positional public argument contract.
            extra_metadata = args[10]
            args = args[:10]
        metadata = dict(extra_metadata or {})
        if scope is not None:
            # The turn's origin wins if the plan changed before this step was stamped.
            metadata.update(scope.metadata())
        return super().record_step(*args, extra_metadata=metadata, **kwargs)

    def record_trace(self, type, name, payload, **kwargs):
        scope = self._scope()
        if scope is not None:
            payload = {**payload, "execution": scope.metadata()}
        return super().record_trace(type, name, payload, **kwargs)

    def _run_in_background(self, fn, *args):
        context = copy_context()
        return super()._run_in_background(context.run, fn, *args)

    def _role_steps(self, steps):
        scope = self._scope()
        if scope is None or not scope.role_id or scope.phase in ("final", "planner"):
            return steps
        return [
            step for step in steps
            if (step.get("extra_metadata") or {}).get("role_id") == scope.role_id
        ]

    def get_agent_friendly_steps(self):
        return self._role_steps(super().get_agent_friendly_steps())

    def get_agent_friendly_steps_in_range(self, start_step, end_step=None):
        return self._role_steps(super().get_agent_friendly_steps_in_range(start_step, end_step))
