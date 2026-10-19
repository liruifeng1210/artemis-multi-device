"""Plan Pro participants before deciding whether the native single-device path applies."""

from pathlib import Path

from artemis.agents.role_planner import plan_roles
from artemis.context import ArtemisContext, DeviceContext
from artemis.runtime.execution_scope import ExecutionScope, execution_scope
from artemis.sdk.multi_device_agent import MultiDeviceAgent


async def dispatch_multi_role_if_needed(
    config,
    request,
    *,
    session_id=None,
    test_name=None,
    traces_path=None,
) -> bool:
    profile = config.agent_profiles.get("pro") or config.default_profile
    planning_ctx = ArtemisContext(
        device=DeviceContext(device_id="role-planning"),
        llm_config=profile.llm_config,
        agent_config=config,
    )
    with execution_scope(ExecutionScope(phase="role_planning")):
        role_plan = await plan_roles(planning_ctx, request)
    if len(role_plan.roles) == 1:
        return False
    result = await MultiDeviceAgent(
        config=config,
        session_id=session_id,
        test_name=test_name,
        traces_path=Path(traces_path) if traces_path else None,
    ).run_case(request, role_plan=role_plan)
    if result["status"] != "completed":
        raise RuntimeError(result.get("error") or f"Pro case ended: {result['status']}")
    return True
