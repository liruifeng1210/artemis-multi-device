"""Preflight checks for the native extension points and resource admission."""


def preflight(workflow, config=None):
    if workflow.fake_worker:
        return
    if config is None:
        from artemis.sdk.agent import Agent

        config = Agent()._config
    if config.disable_checker or config.disable_final_check or config.disable_outputter:
        raise ValueError("Persistent orchestration requires Checker and Outputter")
    from artemis.runtime import DeviceExecutionLock

    effective = DeviceExecutionLock(
        workflow.roles[0].device_id,
        concurrency_mode=config.concurrency_mode,
        max_concurrency=config.max_concurrency,
    ).effective_concurrency
    if effective and effective < len(workflow.roles):
        raise ValueError("Device lease quota cannot hold all persistent roles")
    from artemis.config import load_agent_config

    if not load_agent_config().memory.transcript.enabled:
        raise ValueError("Enable transcript memory before opening persistent roles")
    import os

    if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
        raise ValueError("Persistent roles require local device ownership")
