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


class ObservedMainSession(MainSession):
    """Production Main with a bounded, read-only observer before worker close."""

    def __init__(self, output_folder):
        super().__init__()
        self.output_folder = Path(output_folder)
        self._final_inspection_attempted = False

    async def _capture_final_role(self, role_id, worker):
        folder = self.output_folder / "final-inspections" / role_id
        manifest = {
            "time": stamp(), "role_id": role_id, "session_id": worker.session_id,
            "pid": worker.process.pid, "worker_state": worker.state,
            "source": "official MainSession.inspect before worker close",
            "pages": [], "complete": False, "max_pages": 20, "timeout_seconds": 20,
            "observed_messages": 0, "content_truncation_markers": 0,
            "content_limitations": "Current ledger view may contain compression. Diagnostics redacts credentials/images and truncates individual strings at 8000 characters; complete pagination does not mean lossless content.",
        }
        try:
            active = any(
                not future.done() and self.dispatched_workers.get((task_id, attempt)) is worker
                for task_id, (attempt, future) in self.running.items()
            )
            if worker.state != "READY" or worker.process.returncode is not None or active:
                manifest["skipped"] = "worker_not_alive_settled_and_idle"
                return
            scheduler = getattr(self, "scheduler", None)
            terminals = []
            if scheduler is not None:
                for task_id, result in scheduler.results.items():
                    attempt = scheduler.result_attempts.get(task_id)
                    if self.dispatched_workers.get((task_id, attempt)) is worker:
                        terminals.append({"task_id": task_id, "attempt_id": attempt, "observed_at": result.observed_at})
            if not terminals:
                manifest["skipped"] = "no_terminal_attempt_for_this_worker_session"
                return
            terminal = max(terminals, key=lambda row: datetime.fromisoformat(row["observed_at"]))
            manifest["last_terminal"] = terminal
            query, seen, snapshot_id, frozen_meta = {"limit": 100}, set(), None, None
            async with asyncio.timeout(20):
                for index in range(20):
                    page = await self.inspect(role_id, query)
                    path = f"page-{index:03d}.json"
                    save(folder / path, page)
                    manifest["pages"].append({"path": path, "query": query})
                    if page.get("consistency") != "settled":
                        manifest["error"] = "inspection_not_settled"
                        break
                    if snapshot_id is None:
                        snapshot_id = page.get("snapshot_id")
                    if not snapshot_id or page.get("snapshot_id") != snapshot_id:
                        manifest["error"] = "inspection_snapshot_changed_or_missing"
                        break
                    captured_at = page.get("captured_at")
                    manifest["captured_at"] = captured_at
                    if not captured_at or datetime.fromisoformat(captured_at) < datetime.fromisoformat(terminal["observed_at"]):
                        manifest["error"] = "inspection_precedes_last_terminal"
                        break
                    transcript = page.get("sections", {}).get("transcript")
                    if not isinstance(transcript, dict):
                        manifest["error"] = "transcript_unavailable"
                        break
                    expected_offset = query.get("offset", 0)
                    if "offset" in transcript and transcript["offset"] != expected_offset:
                        manifest["error"] = "inspection_page_offset_mismatch"
                        break
                    messages = transcript.get("messages", [])
                    if not isinstance(messages, list):
                        manifest["error"] = "invalid_transcript_messages"
                        break
                    metadata = (transcript.get("total"), transcript.get("captured_messages"),
                                bool(transcript.get("capture_truncated")), bool(transcript.get("has_staged_turn")))
                    if frozen_meta is None:
                        frozen_meta = metadata
                    elif metadata != frozen_meta:
                        manifest["error"] = "inspection_snapshot_metadata_changed"
                        break
                    manifest["snapshot_id"] = snapshot_id
                    manifest["capture_truncated"] = bool(transcript.get("capture_truncated"))
                    manifest["captured_messages"] = transcript.get("captured_messages")
                    manifest["total"] = transcript.get("total")
                    manifest["has_staged_turn"] = bool(transcript.get("has_staged_turn"))
                    manifest["observed_messages"] += len(messages)
                    manifest["content_truncation_markers"] += json.dumps(messages, ensure_ascii=False).count("[truncated]")
                    cursor = page.get("next_cursor")
                    manifest["next_cursor"] = cursor
                    if cursor is None:
                        manifest["complete"] = (
                            not manifest["capture_truncated"] and not manifest["has_staged_turn"]
                            and manifest["observed_messages"] == manifest["captured_messages"] == manifest["total"]
                        )
                        if not manifest["complete"]:
                            manifest["error"] = "transcript_capture_incomplete_or_staged"
                        break
                    identity = (cursor.get("snapshot_id"), cursor.get("offset"))
                    if identity[1] != expected_offset + len(messages):
                        manifest["error"] = "non_contiguous_inspection_cursor"
                        break
                    if identity in seen or identity[0] != snapshot_id or not isinstance(identity[1], int):
                        manifest["error"] = "invalid_or_repeated_inspection_cursor"
                        break
                    seen.add(identity)
                    query = {"snapshot_id": identity[0], "offset": identity[1], "limit": 100}
                else:
                    manifest["error"] = "inspection_page_limit_reached"
        except Exception as exc:
            manifest["error"] = type(exc).__name__
        finally:
            save(folder / "manifest.json", manifest)

    async def _close(self, reason):
        try:
            if self.closed_report is None and not self._final_inspection_attempted:
                self._final_inspection_attempted = True
                await asyncio.gather(*(
                    self._capture_final_role(role, worker) for role, worker in self.workers.items()
                ), return_exceptions=True)
        finally:
            # Observation errors, timeouts, or cancellation never skip real close.
            report = await super()._close(reason)
        return report


async def monitor(main, folder, done):
    captured, cancel_at, cancelled = set(), {}, set()
    dispatched, proposals = set(), set()
    def observed(kind, **data):
        with (folder / "observer-events.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps({"observed_at":stamp(),"kind":kind,**data},ensure_ascii=False)+"\n")
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
                for proposal in main.planner.history:
                    digest = json.dumps(proposal, sort_keys=True)
                    if digest not in proposals:
                        proposals.add(digest)
                        observed("model_proposal", proposal=proposal)
                for key in list(main.running):
                    task = main.scheduler.tasks[key]
                    attempt = main.running[key][0]
                    if (key, attempt) not in dispatched:
                        dispatched.add((key, attempt))
                        worker = main.dispatched_workers[(key, attempt)]
                        observed("dispatch_seen",task_id=key,attempt_id=attempt,
                            role=task.role_id,device=worker.role.device_id,
                            session=worker.session_id,pid=worker.process.pid,
                            display_name=task.display_name)
                    if task.role_id == "A" and task.display_name == "cancel-A" and key not in cancelled:
                        cancel_at.setdefault(key, time.monotonic()+20)
                        if time.monotonic() >= cancel_at[key]:
                            cancelled.add(key)
                            attempt = main.running[key][0]
                            main.journal.append({"kind":"harness_cancel_request","task_id":key,
                                "attempt_id":attempt,"time":stamp(),"worker_state":main.workers['A'].state,
                                "last_worker_status":main.workers['A'].last_status,
                                "last_worker_status_age_seconds":max(0,time.monotonic()-main.workers['A'].last_status.get('observed_monotonic',time.monotonic()))})
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
                        snapshot["inspection"] = await main.inspect(task.role_id, {"limit":100})
                        transcript = snapshot["inspection"].get("sections",{}).get("transcript") or {}
                        snapshot["conversation_tail"] = transcript.get("messages",[])[-2:]
                        snapshot["transcript_next_offset"] = transcript.get("next_offset")
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
    history_semantics = (
        "本次“历史/首次结果保留”指当前Role/Session真实已归档任务结果、步骤、截图和持久notes，"
        "不是计算器应用的历史记录界面，不要求打开该界面。首次结果必须来自这些原始执行证据或真实观察；"
        "需要报告当前值时重新观察屏幕，不得凭目标中的预填数值推断已完成。"
    )
    continuity = (
        "在两台计算器完成连续计算。第一轮允许清除初始屏幕，A计算125+75=200，B计算48÷6=8，可并发。"
        "第二轮各自保持原Session、连接和历史：A接屏幕结果×3期望600，B接屏幕结果+17期望25。"
        "第二轮禁止清空或重输旧值或旧算式。记住首次结果和当前结果并报告。第一轮失败也不阻止另一Role继续。"
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
    combined = continuity + (
        "上述连续计算后保持相同子Session，继续执行独立取消及复用场景。"
        "即使连续计算失败也应安排独立取消场景：依赖可接受任意终态，不得伪造期望值。"
        "独立取消阶段必须等待A和B各自连续计算任务均达到任意终态后，"
        "同时派发A的cancel-A与B的当前值×4任务；两任务都依赖上述两个前序终态，"
        "前序failed/blocked/cancelled也放行。取消A不能取消或阻塞B的独立执行。"
        "A逐次+1并等号30次、每次观察，该长任务display_name必须为cancel-A；"
        "测试员会在派发20秒后用正式Main.cancel取消它。B同时在当前值×4等号期望100。"
        "A长任务取消后，在同一子Session重新读取实际屏幕值，再+10等号，不假设回滚。"
        "A后续任务依赖cancel-A的任意终态（尤其cancelled）。B最后仅根据自己的历史报告"
        "首次计算结果和当前结果。不得清空或重输旧值。把这些任务全都纳入计划。"
    )
    workflow = WorkflowSpec(goal={"continuity":continuity,"cancellation":cancellation,"all":combined}[args.scenario]+history_semantics,
        roles=[RoleSpec(role_id="A",device_id=args.device_a,description="MI6计算器com.miui.calculator；持续Role记忆首次和当前值"),
               RoleSpec(role_id="B",device_id=args.device_b,description="PJZ110计算器com.coloros.calculator；持续Role记忆首次和当前值")],
        root=folder, max_workflow_seconds=4500, max_artifact_bytes=1024**3,
        expected_cancellations=([{"role_id":"A", "display_name":"cancel-A"}] if args.scenario != "continuity" else []),
        startup_timeout_seconds=180, close_timeout_seconds=90, max_attempts_per_task=1,
        constraints=[history_semantics, "仅操作指定设备计算器；不使用shell直接计算替代真实点击。",
            "任务execution_timeout_seconds=900，verification_timeout_seconds=600，settlement_timeout_seconds=60，max_steps=100。",
            "使用简单标量outputs_schema，例如result为string，不要嵌套对象。每个任务有明确完成条件。",
            "不要重试有不确定副作用的操作。预算内诊断失败即可如实停止，后续独立任务仍应安排。"])
    save(folder / "request.json", workflow.model_dump(mode="json"))
    from artemis.config import get_default_llm_config, load_agent_config
    save(folder / "config.json", {"llm":get_default_llm_config().model_dump(mode="json"),
        "agent":load_agent_config().model_dump(mode="json"),"started":stamp(),
        "main_planner":"production MainPlanner, no seeded tasks"})
    session = ObservedMainSession(folder)
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
    parser.add_argument("--scenario", choices=["continuity","cancellation","all"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--device-a", default="21850fc5")
    parser.add_argument("--device-b", default="a8acf864")
    asyncio.run(main(parser.parse_args()))
