"""Multi-device Pro case API. Workers reuse the existing device lease protocol."""

import asyncio
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from artemis.runtime.multi_device.models import MultiDeviceRequest
from artemis.runtime.multi_device import jobs

router = APIRouter(prefix="/api/cases", tags=["multi-device cases"])


@router.post("", status_code=202)
async def submit_case(request: MultiDeviceRequest):
    try:
        return await asyncio.to_thread(jobs.submit_case, request)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{run_id}")
async def get_case(run_id: str):
    try:
        return await asyncio.to_thread(jobs.get_case, run_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{run_id}/cancel")
async def cancel_case(run_id: str):
    try:
        return await asyncio.to_thread(jobs.cancel_case, run_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/{run_id}/videos/{role_id}")
async def case_video(run_id: str, role_id: str):
    try:
        path = await asyncio.to_thread(jobs.case_video, run_id, role_id)
        return FileResponse(path, media_type="video/mp4")
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
