"""Regressions for failures observed in the real persistent Main workflow."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel, ConfigDict

from artemis.context import ArtemisContext, ExecutionSetup
from artemis.graph.state import State
from artemis.runtime.session_orchestration.models import (
    Completion, RoleSpec, TaskResult, TaskSpec, WorkflowSpec, WorkflowTask,
)
from artemis.runtime.session_orchestration.scheduler import Scheduler


@pytest.mark.asyncio
@pytest.mark.parametrize("plans,write_error,expected_calls,raises", [
    (["- [ ] Continue the calculation"], False, 1, False),
    (["- [x] Already complete"] * 3, False, 3, True),
    (["- [ ] Continue the calculation"] * 30, True, 30, True),
])
async def test_planner_hands_off_only_a_successful_pending_plan(
    tmp_path, monkeypatch, plans, write_error, expected_calls, raises,
):
    import artemis.agents.planner.planner as module

    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = ExecutionSetup()
    ctx.data_engine = MagicMock(base_dir=tmp_path, current_step_id=None)
    screenshot = tmp_path / "screen.jpg"
    screenshot.write_bytes(b"screenshot")
    state = State.initial("Continue from this session's current calculator value")
    state.latest_screenshot = str(screenshot)
    llm = MagicMock()
    llm.bind_tools.return_value = llm
    calls = []

    async def complete(*args, **kwargs):
        calls.append(args[1])
        if len(calls) > len(plans):
            pytest.fail("Planner requested another model turn after submitting its valid plan")
        return AIMessage(content="", tool_calls=[{
            "name": "save_note", "args": {"key": "task_plan", "content": plans[len(calls)-1]},
            "id": str(len(calls)), "type": "tool_call",
        }])

    async def write(*, args, tool_call_id, **kwargs):
        if not write_error:
            (tmp_path / "notes").mkdir(exist_ok=True)
            (tmp_path / "notes" / "task_plan.md").write_text(args["content"], encoding="utf-8")
        return ToolMessage(content="write failed" if write_error else "saved", tool_call_id=tool_call_id,
                           status="error" if write_error else "success")

    monkeypatch.setattr(module, "get_llm", lambda **kwargs: llm)
    monkeypatch.setattr(module, "acomplete", complete)
    monkeypatch.setattr(module, "invoke_tool_with_injection", write)
    node = module.PlannerNode(ctx, [SimpleNamespace(name="save_note", args={"key": {}, "content": {}})])
    if raises:
        with pytest.raises(RuntimeError, match="valid plan"):
            await node(state)
    else:
        assert await node(state) == {}
    assert len(calls) == expected_calls
    assert "## Planning handoff" in calls[0][0].content


class ReportOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    result: str


@pytest.mark.asyncio
async def test_typed_output_finalizes_in_one_turn_and_persists_report(tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module("artemis.agents.outputter.outputter")
    from artemis.config import OutputConfig

    ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir=tmp_path), execution_setup=None)
    state = State.initial("Read calculator result")
    llm = MagicMock()
    llm.bind_tools.return_value = llm
    calls = []

    async def complete(*args, **kwargs):
        calls.append(1)
        assert len(calls) == 1
        return AIMessage(content="", tool_calls=[{
            "name": "finish_output", "args": {"output": {"result": "8"}, "report": "Screen shows 8."},
            "id": "finish", "type": "tool_call",
        }])

    monkeypatch.setattr(module, "_initial_messages", lambda *args: [])
    monkeypatch.setattr(module, "_outputter_tools", lambda ctx: [])
    monkeypatch.setattr(module, "get_llm", lambda **kwargs: llm)
    monkeypatch.setattr(module, "acomplete", complete)
    output = await module.outputter(ctx, OutputConfig(structured_output=ReportOutput), state, "history")
    assert output == {"result": "8"}
    assert (tmp_path / "notes" / "output.md").read_text(encoding="utf-8") == "Screen shows 8."
    llm.with_structured_output.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_finish_output_stays_open_until_corrected(tmp_path):
    from artemis.agents.outputter.outputter import _finish_output_tool, _Investigation, _NO_FINISH
    from artemis.config import OutputConfig

    ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir=tmp_path))
    investigation = _Investigation({}, State.initial("read"), object())
    finish = _finish_output_tool(ctx, OutputConfig(structured_output=ReportOutput), investigation)
    investigation.tool_map[finish.name] = finish
    results = await investigation.execute_tool_calls([{
        "name": "finish_output", "args": {"output": {"result": 8}, "report": "wrong type"}, "id": "bad",
    }])
    assert any(getattr(item, "status", None) == "error" for item in results)
    assert investigation.finished_output is _NO_FINISH
    assert not (tmp_path / "notes" / "output.md").exists()
    await investigation.execute_tool_calls([{
        "name": "finish_output", "args": {"output": {"result": "8"}, "report": "Screen shows 8"}, "id": "good",
    }])
    assert investigation.finished_output == {"result": "8"}


@pytest.mark.asyncio
async def test_finish_output_prevents_later_writes_in_same_batch(tmp_path):
    from artemis.agents.outputter.outputter import _finish_output_tool, _Investigation
    from artemis.config import OutputConfig
    from langchain_core.tools import StructuredTool

    ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir=tmp_path))
    investigation = _Investigation({}, State.initial("read"), object())
    finish = _finish_output_tool(ctx, OutputConfig(structured_output=ReportOutput), investigation)
    overwrite = AsyncMock(return_value="overwritten")
    async def write_note(key: str, content: str):
        return await overwrite(key=key, content=content)
    tool = StructuredTool.from_function(coroutine=write_note, name="save_note", description="write note")
    investigation.tool_map.update({"finish_output": finish, "save_note": tool})
    await investigation.execute_tool_calls([
        {"name": "finish_output", "args": {"output": {"result": "8"}, "report": "Verified 8"}, "id": "finish"},
        {"name": "save_note", "args": {"key": "output", "content": "fake 9"}, "id": "overwrite"},
    ])
    overwrite.assert_not_called()
    assert (tmp_path / "notes" / "output.md").read_text(encoding="utf-8") == "Verified 8"


def cancellation_workflow(tmp_path, *, authorized=True, expected=True):
    task = WorkflowTask(
        role_id="A", display_name="cancel-A",
        expected_terminal_statuses=["succeeded", "cancelled"] if expected else ["succeeded"],
        spec=TaskSpec(goal="long operation", execution_timeout_seconds=1,
                      completion=[Completion(id="c", description="all operations completed")]),
    )
    workflow = WorkflowSpec(
        goal="cancel", roles=[RoleSpec(role_id="A", device_id="test")], tasks=[task],
        expected_cancellations=[{"role_id": "A", "display_name": "cancel-A"}] if authorized else [],
        root=tmp_path, max_workflow_seconds=5, max_artifact_bytes=100000,
    )
    return workflow, task


@pytest.mark.parametrize("authorized,expected,accepted,same_attempt,complete", [
    (True, True, True, True, True),
    (True, True, False, True, False),  # watchdog cancellation
    (True, True, True, False, False),
    (True, False, True, True, False),
])
def test_cancellation_completion_requires_exact_external_authorization(
    tmp_path, authorized, expected, accepted, same_attempt, complete,
):
    workflow, task = cancellation_workflow(tmp_path, authorized=authorized, expected=expected)
    scheduler = Scheduler(workflow)
    attempt = str(uuid4())
    scheduler.record_result(task.task_id, attempt, TaskResult(status="cancelled"))
    if accepted:
        scheduler.authorized_cancellations.add((task.task_id, attempt if same_attempt else str(uuid4())))
    assert scheduler.completed is complete
    assert scheduler.results[task.task_id].status == "cancelled"


def test_cancellation_policy_cannot_be_added_or_duplicated_after_dispatch(tmp_path):
    workflow, task = cancellation_workflow(tmp_path, authorized=False)
    with pytest.raises(ValueError, match="authorization"):
        Scheduler(workflow)
    workflow, task = cancellation_workflow(tmp_path)
    scheduler = Scheduler(workflow)
    duplicate = task.model_copy(update={"task_id": str(uuid4())})
    with pytest.raises(ValueError, match="unique"):
        scheduler.apply([duplicate], scheduler.revision)
    scheduler.executed.add(task.task_id)
    changed = task.model_copy(update={"expected_terminal_statuses": ["succeeded"]})
    with pytest.raises(ValueError, match="executed"):
        scheduler.apply([changed], scheduler.revision)


@pytest.mark.asyncio
async def test_cancelled_trace_is_terminal_and_keeps_initial_owner(tmp_path, monkeypatch):
    import json
    from artemis.context import DeviceContext
    from artemis.data_engine.engine import DataEngine
    from artemis.data_engine.trace import trace
    from artemis.runtime.session_scope import TaskScope

    monkeypatch.setattr(DataEngine, "_connect_ipc_locked", lambda *a, **k: False)
    ctx = ArtemisContext(device=DeviceContext(device_id="device-A"),
                         execution_setup=ExecutionSetup(traces_path=tmp_path, trace_name="trace-test"))
    engine = DataEngine(ctx)
    ctx.data_engine = engine
    engine.start_session(goal="trace-test")
    original = TaskScope(str(uuid4()), str(uuid4()), "old task")
    engine.begin_task(original)
    engine.record_step(summary="old task has a recorded step")
    entered = asyncio.Event()

    @trace(type="agent", name="waiting")
    async def waiting(ctx):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(waiting(ctx))
    await entered.wait()
    snapshot = engine.active_trace_snapshot()
    engine.begin_task(TaskScope(str(uuid4()), str(uuid4()), "next task"))
    assert engine.current_step_id is None, "The next Planner must not inherit the previous task's step"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await engine.flush(10)
    def read_traces():
        with engine.storage._get_connection() as connection:
            rows = connection.execute("SELECT * FROM traces WHERE session_id = ?", (str(engine.current_session_id),)).fetchall()
        return [SimpleNamespace(**{**dict(row), "payload": json.loads(row["payload"])}) for row in rows]
    record = next(r for r in read_traces() if r.name == "waiting")
    assert record.status == "cancelled"
    assert record.payload["execution"]["task_id"] == original.task_id
    # Replaying a delayed start cannot reopen the cancelled trace.
    from artemis.data_engine.models import TraceRecord
    engine.storage.create_trace(TraceRecord(trace_id=record.trace_id, session_id=record.session_id,
                                           type="agent", name="waiting", status="running", payload={}))
    assert next(r for r in read_traces() if r.name == "waiting").status == "cancelled"
    assert snapshot[0]["execution"]["device_id"] == "device-A"
    assert record.payload["execution"]["session_id"] == str(engine.current_session_id)
    assert record.step_id is None
    next_trace = engine.record_trace(type="agent", name="next-planner", payload={}, status="running")
    assert engine.active_trace_snapshot()[0]["execution"]["task_id"] != original.task_id
    engine.finalize_task_traces("failed")
    await engine.flush(10)
    next_record = next(r for r in read_traces() if r.trace_id == str(next_trace))
    assert next_record.step_id is None
    assert next_record.status == "failed"
    assert next_record.duration is not None
    await engine.shutdown()


@pytest.mark.asyncio
async def test_watchdog_cancellation_cannot_later_be_authorized_as_external():
    from artemis.runtime.session_orchestration.worker import Worker
    bootstrap = {key: str(uuid4()) for key in ("workflow_id", "child_session_id", "worker_epoch")}
    bootstrap.update(role_id="A", token="test")
    worker = Worker(bootstrap)
    worker.active_identity = (str(uuid4()), str(uuid4()))
    worker.active = asyncio.create_task(asyncio.Event().wait())
    worker._execution_started = True
    worker._execution_timed_out = True
    worker.session = SimpleNamespace(request_cancel=MagicMock(return_value={"accepted": True}))
    try:
        assert worker.cancel(*worker.active_identity) == {"accepted": False, "code": "execution_timeout"}
        worker.session.request_cancel.assert_not_called()
    finally:
        worker.active.cancel()
        await asyncio.gather(worker.active, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_settlement_never_finalizes_potentially_live_traces(tmp_path, monkeypatch):
    import artemis.sdk.persistent_session as module
    from artemis.context import DeviceContext
    from artemis.runtime.session_scope import TaskScope
    from third_party.mobile_use.sdk.types.task import TaskRequest

    engine = MagicMock(base_dir=tmp_path, current_step_number=0)
    ctx = SimpleNamespace(data_engine=engine, device=DeviceContext(),
                          execution_setup=SimpleNamespace(pause_path=tmp_path / "pause"),
                          reset_task_state=MagicMock())
    agent = SimpleNamespace(
        _tasks=[], _prepare_output_files=MagicMock(),
        _get_graph_state=lambda task: State.initial(task.request.goal),
        _execute_pro_task=AsyncMock(return_value=({}, State.initial("read"))),
    )
    session = module.PersistentSession(agent, module.SessionOptions())
    session.context = ctx
    monkeypatch.setattr(module, "settle_task", AsyncMock(side_effect=RuntimeError("producer not drained")))
    with pytest.raises(RuntimeError, match="producer not drained"):
        await session._execute(TaskRequest(goal="read"), TaskScope(str(uuid4()), str(uuid4()), "read"), None)
    engine.finalize_task_traces.assert_not_called()


@pytest.mark.asyncio
async def test_conflicting_canonical_contract_verdicts_remain_inconclusive(tmp_path):
    from artemis.agents.checker.checker import CheckReport, CheckVerdict, _normalize_report
    from artemis.runtime.session_orchestration.contract import ContractVerifier
    from artemis.utils.plan_grammar import CheckItem

    spec = TaskSpec(goal="read", execution_timeout_seconds=10,
                    completion=[Completion(id="value", description="screen shows 8")])
    text = "[contract:value] screen shows 8"
    report = _normalize_report(CheckReport(verdicts=[
        CheckVerdict(item_text=text, kind="verify", status="failed", evidence="screen shows 9"),
        CheckVerdict(item_text=f"- [verify] (at_end) {text}", kind="verify", status="passed", evidence="screen shows 8"),
    ]), [CheckItem(kind="verify", when="at_end", text=text, parent_key=None)])
    store = SimpleNamespace(root=tmp_path, path=tmp_path / "task", finish=lambda result, paths: result)
    verifier = ContractVerifier(spec, store)
    native = SimpleNamespace(completion_data={"outputs": {}, "report": report.model_dump()},
                             cancelled=False, execution_error=None, session_reusable=True)
    ctx = SimpleNamespace(active_task=SimpleNamespace(start_step_exclusive=0, end_step_inclusive=0),
                          task_artifact_paths=set())
    await verifier.persist(ctx, native)
    assert verifier.result.status == "blocked"
    assert verifier.result.checks[0].verdict == "inconclusive"


@pytest.mark.asyncio
async def test_formal_main_cancel_completes_expected_terminal_and_reuses_worker(tmp_path):
    from artemis.runtime.session_orchestration.main_session import MainSession
    from artemis.runtime.session_orchestration.models import Dependency

    workflow, cancelled = cancellation_workflow(tmp_path)
    cancelled.spec.inputs = {"_test_delay": 10}
    cancelled.spec.execution_timeout_seconds = 15
    follow = WorkflowTask(role_id="A", display_name="after-cancel", spec=TaskSpec(
        goal="reuse the same session", execution_timeout_seconds=5,
        completion=[Completion(id="done", description="reuse complete")],
    ), depends_on=[Dependency(task_id=cancelled.task_id, statuses=["cancelled"])])
    workflow.tasks.append(follow)
    workflow.fake_worker = True
    workflow.max_workflow_seconds = 30
    workflow.close_timeout_seconds = 5
    class NoReplan:
        async def propose(self, *args, **kwargs):
            pytest.fail("An authorized expected cancellation must not trigger recovery")
    main = MainSession(planner=NoReplan())
    run = asyncio.create_task(main.run(workflow))
    try:
        async with asyncio.timeout(20):
            while cancelled.task_id not in main.running:
                if run.done():
                    await run
                await asyncio.sleep(0.01)
        exact_attempt = main.running[cancelled.task_id][0]
        reply = await main.cancel(cancelled.task_id, "test expected cancellation")
        assert reply["accepted"]
        outcome = await asyncio.wait_for(run, 30)
        assert outcome["business_status"] == "completed"
        assert outcome["expected_cancelled_tasks"] == [cancelled.task_id]
        assert outcome["tasks"][cancelled.task_id]["status"] == "cancelled"
        assert outcome["tasks"][follow.task_id]["status"] == "succeeded"
        assert (cancelled.task_id, exact_attempt) in main.scheduler.authorized_cancellations
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
