"""Real two-phone acceptance test for persistent coroutine Role Sessions.

Run: python -m examples.calculator_roles --device-a SERIAL --device-b SERIAL
Uses the configured real model. Only the first assignment clears each calculator.
Evidence and task IDs are saved under --output; failures stop later rounds.
"""

import argparse
import asyncio
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import subprocess

from artemis.sdk import RoleSession
from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder
from artemis.sdk.types import TaskRequest


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def capture(serial, folder):
    folder.mkdir(parents=True, exist_ok=True)
    screen = subprocess.run(
        ["adb", "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True,
        check=True,
        timeout=30,
    )
    (folder / "screen.png").write_bytes(screen.stdout)
    dumped = subprocess.run(
        ["adb", "-s", serial, "shell", "uiautomator", "dump", "/sdcard/window.xml"],
        capture_output=True,
        timeout=30,
    )
    xml = subprocess.run(
        ["adb", "-s", serial, "shell", "cat", "/sdcard/window.xml"], capture_output=True, timeout=30
    )
    if dumped.returncode == 0 and xml.returncode == 0 and b"<hierarchy" in xml.stdout:
        (folder / "screen.xml").write_bytes(xml.stdout)
    else:
        save(
            folder / "hierarchy-unavailable.json",
            {
                "dump_returncode": dumped.returncode,
                "read_returncode": xml.returncode,
                "detail": "Hierarchy unavailable; inspect the saved screenshot instead.",
            },
        )


async def main(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = AgentConfigBuilder().with_video_recording_tools(False).build()
    records = []
    roles = {}
    async with AsyncExitStack() as stack:
        for name, serial in [("A", args.device_a), ("B", args.device_b)]:
            role = await RoleSession.open(
                name,
                serial,
                config=config,
                cleanup_timeout=60,
                session_goal=f"在手机 {name} 的系统计算器中持续完成 Main 分配的连贯计算任务，"
                "记住本 Session 首次结果及后续结果。仅操作本机计算器。",
            )
            roles[name] = await stack.enter_async_context(role)
            print(f"OPEN {name} {serial} {role.session_id}", flush=True)
        save(
            output / "sessions.json",
            {
                n: {"session_id": r.session_id, "device": r.resources.context.device.device_id}
                for n, r in roles.items()
            },
        )

        async def collect(name, label, handle):
            role = roles[name]
            try:
                result = await asyncio.wait_for(handle.result(), timeout=args.timeout)
            except TimeoutError:
                await role.cancel(handle.task_id, handle.attempt_id)
                result = await handle.result()
            record = {"round": label, **asdict(result)}
            records.append(record)
            save(output / "results.json", records)
            print(
                f"RESULT {label} {name} {result.status} reusable={result.reusable} "
                f"output={result.output} error={result.error} cleanup={result.cleanup_errors}",
                flush=True,
            )
            await asyncio.to_thread(
                capture, role.resources.context.device.device_id, output / f"{label}-{name}"
            )
            return result

        def submit(name, goal):
            handle = roles[name].submit(TaskRequest(goal=goal, profile="pro", max_steps=100))
            print(f"SUBMIT {name} {handle.task_id} {handle.attempt_id}", flush=True)
            return handle

        async def round_pair(label, goal_a, goal_b):
            handles = [submit("A", goal_a), submit("B", goal_b)]
            results = await asyncio.gather(*(collect(n, label, h) for n, h in zip(roles, handles)))
            if any(r.status != "completed" or not r.reusable for r in results):
                raise RuntimeError(f"Round {label} failed; see {output / 'results.json'}")
            return results

        await round_pair(
            "01-initial",
            "打开系统计算器，清空本次计算输入，通过真实点击计算 125+75 并按等号。"
            "读取屏幕结果，记住这是你的首次计算结果，报告结果后结束。不要再清空。",
            "打开系统计算器，清空本次计算输入，通过真实点击计算 48÷6 并按等号。"
            "读取屏幕结果，记住这是你的首次计算结果，报告结果后结束。不要再清空。",
        )
        await round_pair(
            "02-continue",
            "接着刚才屏幕上的结果乘以 3 并按等号。不要清空，不要重新输入上一轮的算式或结果。"
            "实际点击并读取新结果，报告首次结果和当前结果。",
            "接着刚才屏幕上的结果加 17 并按等号。不要清空，不要重新输入上一轮的算式或结果。"
            "实际点击并读取新结果，报告首次结果和当前结果。",
        )
        if args.cancel_test:
            handle_a = submit(
                "A",
                "基于当前结果逐次执行加 1 并按等号，共做 30 次，每次都观察结果。"
                "不得合并为加 30。收到取消就停止。",
            )
            handle_b = submit("B", "接着当前结果乘以 4 并按等号。不要清空，读取并报告真实结果。")
            # Wait until A has entered its task, then cancel only that exact attempt.
            for _ in range(600):
                if roles["A"].status == "running":
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(8)
            cancelled = await roles["A"].cancel(handle_a.task_id, handle_a.attempt_id)
            print(f"CANCEL A {handle_a.task_id} acknowledged={cancelled is not None}", flush=True)
            a, b = await asyncio.gather(
                collect("A", "03-cancel", handle_a), collect("B", "03-unaffected", handle_b)
            )
            if (
                a.status != "cancelled"
                or not a.reusable
                or b.status != "completed"
                or not b.reusable
            ):
                raise RuntimeError("Independent cancellation did not meet acceptance criteria")
            await round_pair(
                "04-reuse",
                "上一任务已取消，先重新读取当前计算器实际值，不假设取消回滚。"
                "在此值上加 10 并按等号，不清空。报告操作前和操作后的实际值。",
                "不操作设备，仅根据本 Session 历史报告首次计算结果和当前结果。",
            )
    save(output / "closed.json", {n: r.status for n, r in roles.items()})
    # Native completed may still mean fail-open/inconclusive. Keep it distinct
    # from a conclusive check result in the acceptance report.
    uncertain = []
    for record in records:
        if record["status"] == "cancelled":
            continue
        tests = (record.get("state") or {}).get("run_outcome", {}).get("tests", {})
        if any(tests.get(k, 0) for k in ("failed", "inconclusive", "unchecked")) or not tests.get(
            "passed"
        ):
            uncertain.append({"role": record["role_id"], "round": record["round"], "tests": tests})
    save(
        output / "acceptance.json",
        {
            "inconclusive_or_failed_checks": uncertain,
            "screen_review_required": True,
        },
    )
    print(f"FINISHED {output}", flush=True)
    if uncertain:
        raise SystemExit(
            "Device run finished, but checks are not all conclusive; inspect acceptance.json"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-a", required=True)
    parser.add_argument("--device-b", required=True)
    parser.add_argument(
        "--output", default="scratch/calculator-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--cancel-test", action="store_true")
    asyncio.run(main(parser.parse_args()))
