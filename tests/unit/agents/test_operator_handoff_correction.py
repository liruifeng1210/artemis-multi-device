"""Bounded correction for observed text-only Operator replies with a pending plan."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from artemis.agents.operator import operator as module
from artemis.config.agent import MemoryTranscriptConfig
from artemis.graph.state import State
from artemis.utils.notes import save_note_content
from artemis.utils.plan_grammar import parse_plan


def node(tmp_path):
    ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir=tmp_path, current_step_id=None, record_trace=lambda **kwargs: uuid4()), transcript_ledger=None)
    return module.OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=False))


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [1, 10])
async def test_text_only_pending_plan_receives_exactly_one_actual_correction(tmp_path, monkeypatch, limit):
    save_note_content(tmp_path, 'task_plan', '- [/] Read original evidence')
    operator = node(tmp_path)
    model = MagicMock()
    model.bind_tools.return_value = model
    complete = AsyncMock(side_effect=[AIMessage(content='The record is ready.'), AIMessage(content='Done.')])
    monkeypatch.setattr(module, 'OPERATOR_MAX_TOOL_ITERATIONS', limit)
    monkeypatch.setattr(module, 'acomplete', complete)
    messages = [HumanMessage(content='Read the original record')]
    with pytest.raises(module.OperatorPlanHandoffError, match='after one correction'):
        await operator._invoke_llm_loop(model, messages, [], [], State.initial('read'))
    assert complete.await_count == 2
    assert messages[-2].content == 'The record is ready.'
    assert isinstance(messages[-1], HumanMessage)
    assert 'exact key task_plan' in messages[-1].content
    assert 'Do not mark unperformed or unverified' in messages[-1].content
    assert parse_plan(operator._read_task_plan_text()).all_top_level_done is False


@pytest.mark.asyncio
async def test_history_tool_between_empty_replies_does_not_reset_correction_budget(tmp_path, monkeypatch):
    from langchain_core.tools import StructuredTool
    save_note_content(tmp_path, 'task_plan', '- [/] Read original evidence')
    operator = node(tmp_path)
    async def read_history():
        return 'Actual archived synthetic record'
    history = StructuredTool.from_function(coroutine=read_history, name='search_history', description='Read history')
    complete = AsyncMock(side_effect=[
        AIMessage(content='I read it.'),
        AIMessage(content='', tool_calls=[dict(name='search_history', args={}, id='read')]),
        AIMessage(content='I am done.'),
    ])
    monkeypatch.setattr(module, 'acomplete', complete)
    model = MagicMock()
    model.bind_tools.return_value = model
    with pytest.raises(module.OperatorPlanHandoffError):
        await operator._invoke_llm_loop(model, [HumanMessage(content='read')], [history], [], State.initial('read'))
    assert complete.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('plan', [None, '- [x] Observed and verified'])
async def test_completed_or_unavailable_plan_keeps_original_convergence_behavior(tmp_path, monkeypatch, plan):
    if plan is not None:
        save_note_content(tmp_path, 'task_plan', plan)
    operator = node(tmp_path)
    complete = AsyncMock(return_value=AIMessage(content='Report'))
    monkeypatch.setattr(module, 'acomplete', complete)
    model = MagicMock()
    model.bind_tools.return_value = model
    result = await operator._invoke_llm_loop(model, [HumanMessage(content='read')], [], [], State.initial('read'))
    assert complete.await_count == 1 and result[0] is None and not result[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize('corrected', [False, True])
async def test_production_native_graph_uses_explicit_plan_handoff_or_propagates_failure(tmp_path, monkeypatch, corrected):
    from artemis.context import ArtemisContext, DeviceContext, ExecutionSetup
    from artemis.graph import graph as native
    from artemis.agents.checker.checker import CheckReport
    plan = '- [/] Read archived result\n  - [/] Inspect original note\n'
    save_note_content(tmp_path, 'task_plan', plan)
    save_note_content(tmp_path, 'original_fact', 'Synthetic archived result')
    engine = SimpleNamespace(base_dir=tmp_path, current_step_id=None,
                             get_agent_friendly_steps=lambda: [], record_trace=lambda **kwargs: uuid4(), _publish=lambda *args: None)
    ctx = ArtemisContext.model_construct(device=DeviceContext(), data_engine=engine,
        execution_setup=ExecutionSetup(disable_planner_validation=True, disable_midway_checks=True,
                                       video_recording_tools_enabled=False))
    class ExistingPlan:
        def __init__(self, ctx, tools):
            pass
        async def __call__(self, state):
            return {}
    monkeypatch.setattr(native, 'PlannerNode', ExistingPlan)
    perception = AsyncMock(return_value=dict(operator_raw_data=dict(screenshot_b64='synthetic', xml_hierarchy=[]), latest_ui_hierarchy=[]))
    monkeypatch.setattr(native, 'perception_node', perception)
    monkeypatch.setattr(native, 'execution_check_node', AsyncMock(return_value={}))
    checker = AsyncMock(return_value=CheckReport())
    monkeypatch.setattr(native, 'run_final_check', checker)
    monkeypatch.setattr(module.OperatorNode, '_build_prompt', AsyncMock(return_value=[HumanMessage(content='Read synthetic evidence')]))
    model = MagicMock()
    model.bind_tools.return_value = model
    monkeypatch.setattr(module, 'get_llm', lambda **kwargs: model)
    responses = [AIMessage(content='The original note is available.')]
    if corrected:
        responses += [AIMessage(content='', tool_calls=[dict(name='update_note', id='handoff', args=dict(
            key='task_plan', target=plan, replacement=plan.replace('[/]', '[x]')))]), AIMessage(content='Recorded the observed progress.')]
    else:
        responses += [AIMessage(content='No further actions needed.')]
    complete = AsyncMock(side_effect=responses)
    monkeypatch.setattr(module, 'acomplete', complete)
    graph = await native.get_graph(ctx)
    if corrected:
        result = await graph.ainvoke(State.initial('Read the archived synthetic result').model_dump())
        assert result['exit_settlement_route'] == 'end'
        assert parse_plan((tmp_path / 'notes/task_plan.md').read_text()).all_top_level_done
        checker.assert_awaited_once()
        assert complete.await_count == 3
    else:
        with pytest.raises(module.OperatorPlanHandoffError):
            await graph.ainvoke(State.initial('Read the archived synthetic result').model_dump())
        assert not parse_plan((tmp_path / 'notes/task_plan.md').read_text()).all_top_level_done
        checker.assert_not_awaited()
        assert complete.await_count == 2
    assert perception.await_count == 1
