"""Two local devices, persistent Roles, and explicit meeting dependencies.

Run from the repository root after configuring Artemis and unlocking both phones:
    python -m examples.persistent_roles --device-a SERIAL_A --device-b SERIAL_B --b-name Bob

This example operates real devices and calls the configured models. It assumes
the meeting application is installed and the two accounts can sign in normally.
"""

import argparse
import asyncio
from contextlib import AsyncExitStack

from pydantic import BaseModel

from artemis.sdk import RoleSession
from artemis.sdk.types import TaskRequest


class Meeting(BaseModel):
    meeting_id: str


class Application(BaseModel):
    submitted: bool


async def run_goal(role, goal, *, output_format=None):
    handle = role.submit(TaskRequest(goal=goal, profile="pro", output_format=output_format))
    try:
        result = await asyncio.wait_for(handle.result(), timeout=900)
    except (TimeoutError, asyncio.CancelledError):
        await role.cancel(handle.task_id, handle.attempt_id)
        raise
    if result.status != "completed" or not result.reusable:
        raise RuntimeError(f"{role.role_id}: {result.error}; cleanup={result.cleanup_errors}")
    return result.output


async def main(device_a, device_b, b_name):
    async with AsyncExitStack() as stack:
        a = await stack.enter_async_context(await RoleSession.open("A", device_a))
        b = await stack.enter_async_context(await RoleSession.open("B", device_b))
        await run_goal(a, "登录会议应用，确认账号 A 已登录。")
        meeting = Meeting.model_validate(
            await run_goal(
                a, "创建需要主持人批准的会议，返回真实 MeetingID。", output_format=Meeting
            )
        )

        # B stops at application submission. Waiting for admission here would
        # entangle task completion with a later dependency on A's approval.
        application, _ = await asyncio.gather(
            run_goal(
                b,
                f"登录账号 B，申请加入会议 {meeting.meeting_id}。"
                "确认申请已提交后立即结束。不要等待批准，不要声称已经入会。",
                output_format=Application,
            ),
            run_goal(
                a,
                f"在当前会议等待 {b_name} 的申请，出现后批准。"
                "确认批准已生效后结束，无需等待 B 的后续确认任务。",
            ),
        )
        if not Application.model_validate(application).submitted:
            raise RuntimeError("B has not submitted an application")
        await run_goal(b, f"确认自己已经进入会议 {meeting.meeting_id}；仅提交申请不算成功。")
        await run_goal(a, f"在当前会议向 {b_name} 发送消息：欢迎加入。确认消息已发送。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-a", required=True)
    parser.add_argument("--device-b", required=True)
    parser.add_argument("--b-name", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.device_a, args.device_b, args.b_name))
