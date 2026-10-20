"""Drive the production model-backed MainSession with an empty task list.

The harness observes dispatch/results and requests one externally timed per-task
cancel. It never constructs WorkflowTask, replaces MainPlanner or operates a phone.
Each scenario creates NEW child sessions; reuse claims apply only within a scenario.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
import psutil

from artemis.runtime.session_orchestration.main_session import MainSession
from artemis.runtime.session_orchestration.models import WorkflowSpec, RoleSpec


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def capture(serial, path):
    path.mkdir(parents=True, exist_ok=True)
    shot = subprocess.run(["adb", "-s", serial, "exec-out", "screencap", "-p"],
                          capture_output=True, timeout=30, check=True)
    (path / "screen.png").write_bytes(shot.stdout)
    subprocess.run(["adb", "-s", serial, "shell", "uiautomator", "dump", "/sdcard/window.xml"],
                   capture_output=True, timeout=30)
    xml = subprocess.run(["adb", "-s", serial, "shell", "cat", "/sdcard/window.xml"],
                         capture_output=True, timeout=30)
    if b"<hierarchy" in xml.stdout:
        (path / "screen.xml").write_bytes(xml.stdout)


async def monitor(main, folder, done):
    captured, cancel_at, cancelled = set(), {}, set()
    with (folder / "process-samples.jsonl").open("a", encoding="utf-8") as stream:
        last_sample = 0
        while not done.is_set():
            if time.monotonic() - last_sample >= 5:
                rows = []
                for proc in [psutil.Process(), *psutil.Process().children(recursive=True)]:
                    try:
                        rows.append({"pid":proc.pid,"ppid":proc.ppid(),"name":proc.name(),
                                     "rss":proc.memory_info().rss,"created":proc.create_time()})
                    except psutil.Error:
                        pass
                stream.write(json.dumps({"time":stamp(),"processes":rows})+"\n")
                stream.flush()
                last_sample = time.monotonic()
            if hasattr(main, "scheduler"):
                for key in list(main.running):
                    task = main.scheduler.tasks[key]
                    if task.role_id == "A" and task.display_name == "cancel-A" and key not in cancelled:
                        cancel_at.setdefault(key, time.monotonic()+20)
                        if time.monotonic() >= cancel_at[key]:
                            cancelled.add(key)
                            attempt = main.running[key][0]
                            main.journal.append({"kind":"harness_cancel_request","task_id":key,
                                "attempt_id":attempt,"time":stamp(),"worker_state":main.workers['A'].state})
                            try:
                                reply = await main.cancel(key, "authorized_independent_cancel_test")
                                save(folder / "cancel.json", {"time":stamp(),"task":key,
                                     "attempt":attempt,"reply":reply})
                            except Exception as exc:
                                save(folder / "cancel-error.json", {"error":repr(exc)})
                for key, result in list(main.scheduler.results.items()):
                    if key in captured:
                        continue
                    captured.add(key)
                    task = main.scheduler.tasks[key]
                    worker = main.workers[task.role_id]
                    snapshot = {"time":stamp(),"task":task.model_dump(mode="json"),
                        "result":result.model_dump(mode="json"),"session":worker.session_id,
                        "pid":worker.process.pid,"device":worker.role.device_id}
                    # Observe only at task boundaries. Busy workers return cached state.
                    try:
                        snapshot["inspection"] = await main.inspect(task.role_id, {"limit":10})
                    except Exception as exc:
                        snapshot["inspection_error"] = repr(exc)
                    save(folder / "boundaries" / key / "result.json", snapshot)
                    try:
                        await asyncio.to_thread(capture, worker.role.device_id, folder / "boundaries" / key)
                    except Exception as exc:
                        save(folder / "boundaries" / key / "capture-error.json", {"error":repr(exc)})
            await asyncio.sleep(.25)


async def main(args):
    folder = Path(args.output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    continuity = (
        "在两台计算器完成连续计算。第一轮A清空一次后125+75=200，B清空一次后48÷6=8，可并发。"
        "第二轮各自保持原Session、连接和历史：A接屏幕结果×3期望600，B接屏幕结果+17期望25。"
        "禁止清空或重输旧值或旧算式。记住首次结果和当前结果并报告。第一轮失败也不阻止另一Role继续。"
        "为每轮每Role安排一个任务，display_name使用initial-A/initial-B/continue-A/continue-B。"
    )
    cancellation = (
        "这是独立取消场景，子Session为新建，设备屏幕延续前一场景，不能证明跨场景Session复用。"
        "A在真实当前值逐次+1并等号30次，逐次观察，禁止合并；该长任务display_name必须为cancel-A，"
        "外部测试员会在它派发20秒后调用Main.cancel，不需要向用户提问。"
        "同时B在真实当前结果×4等号，若起始25则期望100，不清空。"
        "A取消后在同一子Session重新读取真实值，再+10等号，禁止假设回滚。该后续任务依赖A长任务"
        "cancelled/failed/blocked/succeeded/unknown任意终态，不能要求成功。"
        "B后续只根据本场景Session历史报告本场景首次观察值和当前值。前一场景首次计算值的记忆"
        "在新Session中未验证，必须如实说明。各Role互不依赖，A取消不能阻止B。"
    )
    workflow = WorkflowSpec(goal=continuity if args.scenario == "continuity" else cancellation,
        roles=[RoleSpec(role_id="A",device_id=args.device_a,description="MI6计算器com.miui.calculator；持续Role记忆首次和当前值"),
               RoleSpec(role_id="B",device_id=args.device_b,description="PJZ110计算器com.coloros.calculator；持续Role记忆首次和当前值")],
        root=folder, max_workflow_seconds=2400, max_artifact_bytes=1024**3,
        startup_timeout_seconds=180, close_timeout_seconds=90, max_attempts_per_task=1,
        constraints=["仅操作指定设备计算器；不使用shell直接计算替代真实点击。",
            "任务execution_timeout_seconds=900，verification_timeout_seconds=600，settlement_timeout_seconds=60，max_steps=100。",
            "使用简单标量outputs_schema，例如result为string，不要嵌套对象。每个任务有明确完成条件。",
            "不要重试有不确定副作用的操作。预算内诊断失败即可如实停止，后续独立任务仍应安排。"])
    save(folder / "request.json", workflow.model_dump(mode="json"))
    from artemis.config import get_default_llm_config, load_agent_config
    save(folder / "config.json", {"llm":get_default_llm_config().model_dump(mode="json"),
        "agent":load_agent_config().model_dump(mode="json"),"started":stamp(),
        "main_planner":"production MainPlanner, no seeded tasks"})
    session = MainSession()
    done = asyncio.Event()
    watcher = asyncio.create_task(monitor(session, folder, done))
    try:
        result = await session.run(workflow)
        save(folder / "outcome.json", result)
    except BaseException as exc:
        save(folder / "error.json", {"time":stamp(),"error":repr(exc)})
    finally:
        done.set()
        await watcher
        from artemis.runtime.device_lock import DeviceExecutionLock
        save(folder / "closed.json", {"time":stamp(),"report":session.closed_report,
            "workers":{n:{"pid":w.process.pid,"exit":w.process.returncode,"session":w.session_id}
                       for n,w in session.workers.items()},
            "locks":{s:DeviceExecutionLock.has_owner_record(s) for s in [args.device_a,args.device_b]},
            "children":[{"pid":p.pid,"name":p.name()} for p in psutil.Process().children(recursive=True)]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["continuity","cancellation"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--device-a", default="21850fc5")
    parser.add_argument("--device-b", default="a8acf864")
    asyncio.run(main(parser.parse_args()))
