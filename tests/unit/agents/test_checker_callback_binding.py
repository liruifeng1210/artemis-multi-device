"""Real RunnableBinding transformations must retain independent Checker telemetry."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from artemis.agents.checker import checker
from artemis.context import ArtemisContext, DeviceContext, ExecutionSetup
from artemis.data_engine.context_vars import CURRENT_TRACE_ID
from artemis.data_engine.engine import DataEngine
from artemis.data_engine.trace import DataEngineCallbackHandler
from artemis.llm.codex import CodexChatModel
from artemis.runtime.session_scope import TaskScope
from artemis.services.llm import RobustChatModelWrapper


class SyntheticCodex(CodexChatModel):
    """Use the real adapter binding/parser, replacing only its CLI generation."""
    answers: list[dict] = Field(default_factory=list)
    wait_for_cancel: bool = False
    entered: object = Field(default_factory=asyncio.Event, exclude=True)
    exited: object = Field(default_factory=asyncio.Event, exclude=True)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.entered.set()
        if self.wait_for_cancel:
            try:
                await asyncio.Event().wait()
            finally:
                self.exited.set()
        if self.tools:
            response = AIMessage(content="", tool_calls=[dict(
                id=str(uuid4()), name=self.tools[0]["function"]["name"], args=self.answers.pop(0),
            )])
        else:
            response = AIMessage(content="Review the synthetic evidence.")
        return ChatResult(generations=[ChatGeneration(message=response)])


class ObserveCallbacks(BaseCallbackHandler):
    def __init__(self):
        self.starts, self.ends, self.errors = [], [], []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.starts.append((messages, kwargs))

    def on_llm_end(self, response, **kwargs):
        self.ends.append(kwargs)

    def on_llm_error(self, error, **kwargs):
        self.errors.append(error)


@asynccontextmanager
async def owned_engine(tmp_path, monkeypatch):
    from artemis.data_engine import engine as engine_module
    monkeypatch.setattr(engine_module, "_CURRENT_DATA_ENGINE", None)
    monkeypatch.setattr(DataEngine, "_connect_ipc_locked", lambda *args, **kwargs: False)
    ctx = ArtemisContext(device=DeviceContext(device_id="synthetic-device"), execution_setup=ExecutionSetup(
        traces_path=tmp_path, trace_name="checker-callbacks", checker_max_iterations=2))
    ctx.execution_owner = {"role_id": "A", "workflow_id": "synthetic-workflow"}
    engine = DataEngine(ctx)
    ctx.data_engine = engine
    engine.start_session(goal="Synthetic protocol test; no device/model access")
    task = TaskScope(str(uuid4()), str(uuid4()), "synthetic")
    engine.begin_task(task)
    record = MagicMock(wraps=engine.record_trace)
    monkeypatch.setattr(engine, "record_trace", record)
    parent = uuid4()
    token = CURRENT_TRACE_ID.set(parent)
    try:
        yield ctx, engine, record, parent, task.metadata()
    finally:
        CURRENT_TRACE_ID.reset(token)
        await engine.shutdown()


def wrapped(ctx, model, observe):
    handler = DataEngineCallbackHandler(ctx)
    bound = model.with_config(callbacks=[handler, observe], tags=["synthetic-checker"],
                              metadata={"audit_marker": "preserved"}, run_name="synthetic-checker")
    return RobustChatModelWrapper(bound, ctx)


def trace_calls(record, status):
    return [call.kwargs for call in record.call_args_list
            if call.kwargs.get("type") == "llm_call" and call.kwargs.get("status") == status]


async def persisted_rows(engine):
    await engine.flush()
    with engine.storage._get_connection() as connection:
        return connection.execute("SELECT * FROM traces WHERE type='llm_call'").fetchall()


@pytest.mark.asyncio
async def test_no_graph_tool_and_structured_calls_preserve_callbacks_prompt_parent_and_owner(tmp_path, monkeypatch):
    item = SimpleNamespace(text="[contract:synthetic] Synthetic evidence only.", kind="verify", when="at_end")
    payload = dict(verdicts=[dict(item_text=item.text, kind=item.kind, status="failed", evidence="Synthetic failure evidence.")])
    async with owned_engine(tmp_path, monkeypatch) as (ctx, engine, record, parent, owner):
        observe = ObserveCallbacks()
        llm = wrapped(ctx, SyntheticCodex(answers=[payload]), observe)
        monkeypatch.setattr(checker, "get_llm", lambda **kwargs: llm)
        report = await checker._run_check_loop(ctx, [HumanMessage(content="SYNTHETIC_CHECKER_PROMPT")], [], [item])
        assert report.verdicts[0].status == "failed"
        assert len(observe.starts) == len(observe.ends) == 2
        assert len(trace_calls(record, "running")) == len(trace_calls(record, "success")) == 2
        assert not observe.errors
        for _, metadata in observe.starts:
            assert metadata["metadata"]["audit_marker"] == "preserved"
            assert "synthetic-checker" in metadata["tags"]
        for row in await persisted_rows(engine):
            assert row["status"] == "success"
            assert row["parent_trace_id"] == str(parent)
            assert row["step_id"] is None
            data = json.loads(row["payload"])
            assert "SYNTHETIC_CHECKER_PROMPT" in json.dumps(data["messages"])
            assert all(data["execution"][key] == value for key, value in owner.items())


@pytest.mark.asyncio
async def test_structured_parser_error_is_data_and_only_one_correction_no_gateway_retry(tmp_path, monkeypatch):
    item = SimpleNamespace(text="[contract:synthetic] Original text.", kind="verify", when="at_end")
    def payload(text):
        return dict(verdicts=[dict(item_text=text, kind="verify", status="passed", evidence="Synthetic evidence.")])
    async with owned_engine(tmp_path, monkeypatch) as (ctx, engine, record, parent, owner):
        observe = ObserveCallbacks()
        llm = wrapped(ctx, SyntheticCodex(answers=[payload("Changed text."), payload(item.text)]), observe)
        report = await checker._structured_report(llm, [HumanMessage(content="SYNTHETIC")], [item])
        assert report.verdicts[0].item_text == item.text
        assert len(observe.starts) == len(observe.ends) == 2
        assert not observe.errors  # schema parsing stayed outside transport retry.
        assert len(trace_calls(record, "running")) == len(trace_calls(record, "success")) == 2
        assert all(row["status"] == "success" for row in await persisted_rows(engine))


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_reapplying_existing_callback_config_does_not_duplicate_callbacks(tmp_path, monkeypatch, structured):
    item = SimpleNamespace(text="Exact synthetic text.", kind="verify", when="at_end")
    payload = dict(verdicts=[dict(item_text=item.text, kind="verify", status="passed", evidence="Synthetic.")])
    async with owned_engine(tmp_path, monkeypatch) as (ctx, engine, record, parent, owner):
        observe = ObserveCallbacks()
        llm = wrapped(ctx, SyntheticCodex(answers=[payload]), observe)
        changed = llm.with_structured_output(checker._declared_report_model([item]), include_raw=True) if structured else llm.bind_tools([])
        for _ in range(3):
            changed = checker._with_checker_config(llm, changed)
        await changed.ainvoke([HumanMessage(content="SYNTHETIC")])
        assert len(observe.starts) == len(observe.ends) == 1
        assert len(trace_calls(record, "running")) == len(trace_calls(record, "success")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_no_graph_cancelled_call_has_owned_terminal_trace(tmp_path, monkeypatch, structured):
    item = SimpleNamespace(text="Exact synthetic text.", kind="verify", when="at_end")
    async with owned_engine(tmp_path, monkeypatch) as (ctx, engine, record, parent, owner):
        observe = ObserveCallbacks()
        model = SyntheticCodex(wait_for_cancel=True)
        llm = wrapped(ctx, model, observe)
        if structured:
            operation = checker._structured_report(llm, [HumanMessage(content="SYNTHETIC_CANCEL")], [item])
        else:
            active = checker._with_checker_config(llm, llm.bind_tools([]))
            operation = active.ainvoke([HumanMessage(content="SYNTHETIC_CANCEL")])
        pending = asyncio.create_task(operation)
        await asyncio.wait_for(model.entered.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert pending.done() and model.exited.is_set()
        assert len(observe.starts) == 1
        assert not observe.errors  # LC agenerate's cancelled gather skips error callbacks.
        assert not observe.ends
        task = ctx.active_task
        ctx.active_task = TaskScope(task.task_id, "other-attempt", task.goal)
        other = engine.record_trace(type="agent", name="other-attempt-in-flight", payload={}, status="running")
        ctx.active_task = task
        # This boundary is safe only after the actual model producer has exited.
        engine.finalize_task_traces("cancelled")
        assert len(trace_calls(record, "running")) == len(trace_calls(record, "cancelled")) == 1
        rows = await persisted_rows(engine)
        assert len(rows) == 1 and rows[0]["status"] == "cancelled"
        assert rows[0]["parent_trace_id"] == str(parent)
        data = json.loads(rows[0]["payload"])
        assert "SYNTHETIC_CANCEL" in json.dumps(data["messages"])
        assert all(data["execution"][key] == value for key, value in owner.items())
        with engine.storage._get_connection() as connection:
            assert connection.execute("SELECT status FROM traces WHERE trace_id=?", (str(other),)).fetchone()["status"] == "running"
        engine.record_trace(type="agent", name="other-attempt-in-flight", payload={}, trace_id=other, status="cancelled")


@pytest.mark.asyncio
@pytest.mark.parametrize('structured', [False, True])
@pytest.mark.parametrize('relation', ['same_instance', 'same_context', 'other_context'])
async def test_real_graph_inherited_callbacks_do_not_duplicate_same_context_or_lose_current_parent(
    tmp_path, monkeypatch, structured, relation,
):
    from langgraph.graph import StateGraph, START, END
    item = SimpleNamespace(text='Exact synthetic graph identity.', kind='verify', when='at_end')
    payload = dict(verdicts=[dict(item_text=item.text, kind='verify', status='passed', evidence='Synthetic graph record.')])
    async with owned_engine(tmp_path, monkeypatch) as (ctx, engine, record, outer_parent, owner):
        observe = ObserveCallbacks()
        llm = wrapped(ctx, SyntheticCodex(answers=[payload]), observe)
        source_handler = llm.base_model.config['callbacks'][0]
        other_records = MagicMock()
        other_context = SimpleNamespace(data_engine=SimpleNamespace(current_step_id=None, record_trace=other_records), session_resources=None)
        graph_handler = source_handler if relation == 'same_instance' else DataEngineCallbackHandler(ctx if relation == 'same_context' else other_context)
        current_parent = uuid4()
        async def node(state):
            token = CURRENT_TRACE_ID.set(current_parent)
            try:
                if structured:
                    result = await checker._structured_report(llm, [HumanMessage(content='SYNTHETIC_GRAPH')], [item])
                    assert result.verdicts[0].status == 'passed'
                else:
                    changed = checker._with_checker_config(llm, llm.bind_tools([]))
                    await changed.ainvoke([HumanMessage(content='SYNTHETIC_GRAPH')])
                return {'done': True}
            finally:
                CURRENT_TRACE_ID.reset(token)
        graph = StateGraph(dict)
        graph.add_node('checker', node)
        graph.add_edge(START, 'checker')
        graph.add_edge('checker', END)
        await graph.compile().ainvoke({}, config={'callbacks': [graph_handler, observe]})
        assert len(observe.starts) == len(observe.ends) == 1
        assert len(trace_calls(record, 'running')) == len(trace_calls(record, 'success')) == 1
        rows = await persisted_rows(engine)
        assert len(rows) == 1 and rows[0]['parent_trace_id'] == str(current_parent)
        assert llm.base_model.config['callbacks'][0] is source_handler
        if relation == 'other_context':
            other_llm_calls = [call.kwargs for call in other_records.call_args_list if call.kwargs.get('type') == 'llm_call']
            assert [call['status'] for call in other_llm_calls] == ['running', 'success']
