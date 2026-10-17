"""MCP access to the same asynchronous multi-device jobs as the console."""

import asyncio
from mcp_server.base import mcp
from artemis.runtime.multi_device.models import MultiDeviceRequest
from artemis.runtime.multi_device import jobs


@mcp.tool()
async def mobile_run_case(request_json: str) -> dict:
    """Start a Pro multi-device case. JSON requires case and role_bindings {role: {serial}};
    optionally app_profile, role_plan, feature_catalog, budgets and record_video.
    Returns a run_id immediately; use mobile_get_case to inspect progress/results.
    """
    request = MultiDeviceRequest.model_validate_json(request_json)
    return await asyncio.to_thread(jobs.submit_case, request)


@mcp.tool()
async def mobile_get_case(run_id: str) -> dict:
    """Get the roles, active device, milestone plan, outcome and recordings for a case."""
    return await asyncio.to_thread(jobs.get_case, run_id)


@mcp.tool()
async def mobile_cancel_case(run_id: str) -> dict:
    """Cancel the entire multi-device case and release all of its devices after cleanup."""
    return await asyncio.to_thread(jobs.cancel_case, run_id)
