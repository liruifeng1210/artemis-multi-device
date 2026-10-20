"""Bounded real-device coroutine test; deterministic harness, NOT intelligent Main.

Run with the signed-in Codex config from this checkout. No device actions in harness.
Failures are recorded and later independent scenarios still run on reusable roles.
"""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import psutil
from examples.calculator_roles import capture, save
from artemis.sdk import RoleSession
from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder
from artemis.sdk.types import TaskRequest


def stamp():
    return datetime.now(timezone.utc).isoformat()


async def sample(folder, stop):
    with (folder / "process-samples.jsonl").open("a", encoding="utf-8") as stream:
        while not stop.is_set():
            rows = []
            for proc in [psutil.Process(), *psutil.Process().children(recursive=True)]:
                try:
                    rows.append({"pid": proc.pid, "ppid": proc.ppid(), "name": proc.name(),
                                 "rss": proc.memory_info().rss, "created": proc.create_time()})
                except psutil.Error:
                    pass
            stream.write(json.dumps({"time": stamp(), "processes": rows}) + "\n")
            stream.flush()
            try:
                await asyncio.wait_for(stop.wait(), 5)
            except TimeoutError:
                pass


async def main(args):
    folder = Path(args.output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    records, roles, events = [], {}, []
    stop = asyncio.Event()
    sampler = asyncio.create_task(sample(folder, stop))
    config = AgentConfigBuilder().with_video_recording_tools(False).build()
    from artemis.config import get_default_llm_config, load_agent_config
    save(folder / "config.json", {"llm": get_default_llm_config().model_dump(mode="json"),
         "agent": load_agent_config().model_dump(mode="json"), "task_timeout": args.timeout,
         "max_steps": 30, "pid": os.getpid(), "started": stamp(),
         "orchestrator": "deterministic test harness; intelligent Main absent"})

    def event(kind, **data):
        row = {"time": stamp(), "kind": kind, **data}
        events.append(row)
        save(folder / "timeline.json", events)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    async def collect(name, label, handle):
        role = roles[name]
        try:
            result = await asyncio.wait_for(handle.result(), args.timeout)
        except TimeoutError:
            event("guard_timeout", role=name, task=handle.task_id)
            result = await role.cancel(handle.task_id, handle.attempt_id)
        records.append({"round": label, "finished": stamp(), **asdict(result)})
        save(folder / "results.json", records)
        event("terminal", role=name, task=handle.task_id, status=result.status,
              reusable=result.reusable, cleanup=result.cleanup_errors)
        await asyncio.to_thread(capture, args.devices[name], folder / f"{label}-{name}")
        resource = role.resources
        save(folder / f"{label}-{name}" / "boundary.json", {
            "session": role.session_id, "context_id": id(resource.context),
            "driver_id": id(resource.driver), "ledger_id": id(resource.ledger),
            "role_status": role.status, "continuity": role._continuity.records,
            "background_tasks": len(resource.context.background_tasks),
            "background_jobs": len(resource.context.background_jobs)})
        return result

    def submit(name, label, goal):
        role = roles[name]
        handle = role.submit(TaskRequest(goal=goal, profile="pro", max_steps=30))
        event("submit", role=name, label=label, session=role.session_id,
              device=args.devices[name], task=handle.task_id, attempt=handle.attempt_id, goal=goal)
        return handle

    async def pair(label, goals):
        waits = []
        for name, goal in goals.items():
            if roles[name].status not in ("idle", "running", "settling"):
                event("not_executed", role=name, label=label, reason=roles[name].status)
                continue
            waits.append(collect(name, label, submit(name, label, goal)))
        return await asyncio.gather(*waits, return_exceptions=True)

    try:
        for name, serial in args.devices.items():
            roles[name] = await RoleSession.open(name, serial, config=config, cleanup_timeout=60,
                session_goal="在本机计算器持续计算，记住首次和当前结果；每项任务重新观察真实屏幕。")
            event("open", role=name, device=serial, session=roles[name].session_id)
        save(folder / "sessions.json", {n: {"session_id": r.session_id, "device": args.devices[n]}
                                       for n, r in roles.items()})
        await pair("01-initial", {
            "A": "打开系统计算器 com.miui.calculator，清空本次输入，真实点击计算125+75并按等号。读取结果并记住首次结果，写入session_context，不再清空。",
            "B": "打开系统计算器 com.coloros.calculator，清空本次输入，真实点击计算48÷6并按等号。读取结果并记住首次结果，写入session_context，不再清空。"})
        await pair("02-continue", {
            "A": "接着屏幕上轮结果乘3并按等号，期望600。不得清空或重新输入旧结果或旧算式。读真实屏幕，报告首次及当前结果。",
            "B": "接着屏幕上轮结果加17并按等号，期望25。不得清空或重新输入旧结果或旧算式。读真实屏幕，报告首次及当前结果。"})
        if all(r.status == "idle" for r in roles.values()):
            a = submit("A", "03-cancel", "在真实当前值上逐次加1并按等号30次，每次观察。不得合并成加30；收到取消立即停止。")
            b = submit("B", "03-independent", "在真实当前结果上乘4并按等号，期望100。不清空或重输旧值；读取并报告真实结果。")
            await asyncio.sleep(20)
            active = roles["A"]._active
            event("cancel_request", role="A", task=a.task_id, attempt=a.attempt_id,
                  status=roles["A"].status, active=bool(active),
                  children=[p.name() for p in psutil.Process().children(recursive=True)])
            await roles["A"].cancel(a.task_id, a.attempt_id)
            event("cancel_return", role="A", task=a.task_id)
            await asyncio.gather(collect("A", "03-cancel", a), collect("B", "03-independent", b),
                                 return_exceptions=True)
        else:
            event("not_executed", label="03-cancel", reason="one role not reusable")
        await pair("04-reuse", {
            "A": "上个任务已结束或取消。先重新读取真实屏幕当前值，不假设回滚，再在此值加10并按等号，不清空。报告前后值。",
            "B": "不操作设备，仅根据本Session历史报告首次计算结果和当前结果；不要通过重新计算替代记忆。"})
    except Exception as exc:
        event("harness_error", error=repr(exc))
    finally:
        for name, role in roles.items():
            try:
                await role.close()
                event("closed", role=name, status=role.status)
            except Exception as exc:
                event("close_error", role=name, error=repr(exc))
        from artemis.runtime.device_lock import DeviceExecutionLock
        save(folder / "closed.json", {"time": stamp(), "roles": {n:r.status for n,r in roles.items()},
             "locks": {n:DeviceExecutionLock.has_owner_record(s) for n,s in args.devices.items()},
             "children": [{"pid":p.pid,"name":p.name()} for p in psutil.Process().children(recursive=True)]})
        stop.set()
        await sampler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--device-a", default="21850fc5")
    parser.add_argument("--device-b", default="a8acf864")
    args = parser.parse_args()
    args.devices = {"A": args.device_a, "B": args.device_b}
    asyncio.run(main(args))
