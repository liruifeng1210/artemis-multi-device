"""Execute one Pro case against a set of role-bound devices."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer


def run_case_command(
    request: Annotated[
        Path, typer.Argument(help="JSON MultiDeviceRequest file", exists=True, dir_okay=False)
    ],
    session_id: Annotated[
        str | None, typer.Option(help="Run/session ID for status and cancellation")
    ] = None,
):
    from artemis.runtime.multi_device.models import MultiDeviceRequest
    from artemis.sdk.multi_device_agent import MultiDeviceAgent

    try:
        spec = MultiDeviceRequest.model_validate_json(request.read_text(encoding="utf-8-sig"))
        result = asyncio.run(MultiDeviceAgent(session_id=session_id).run_case(spec))
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "completed":
        raise typer.Exit(1)
