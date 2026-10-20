"""Explicit full-Role recall; execution/check evidence defaults to the task."""

from dataclasses import replace

from artemis.runtime.execution_scope import CURRENT_EXECUTION, execution_scope


class SessionHistoryReader:
    def __init__(self, engine):
        self.engine = engine

    def __getattr__(self, name):
        value = getattr(self.engine, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            scope = CURRENT_EXECUTION.get()
            if scope is None:
                return value(*args, **kwargs)
            with execution_scope(replace(scope, phase="memory")):
                return value(*args, **kwargs)

        return call


def history_reader(ctx):
    engine = ctx.data_engine
    scope = CURRENT_EXECUTION.get()
    if getattr(ctx, "session_resources", None) is not None and scope and scope.phase == "operator":
        return SessionHistoryReader(engine)
    return engine
