"""Persistent role processes and workflow orchestration (opt-in)."""

__all__ = ["MainSession", "WorkflowSpec"]


def __getattr__(name):
    if name == "MainSession":
        from artemis.runtime.session_orchestration.main_session import MainSession

        return MainSession
    if name == "WorkflowSpec":
        from artemis.runtime.session_orchestration.models import WorkflowSpec

        return WorkflowSpec
    raise AttributeError(name)
