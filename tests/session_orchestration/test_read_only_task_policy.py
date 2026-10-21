"""Trusted read-only recovery tasks must block effects before their dispatch."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langchain_core.messages import ToolMessage
from pydantic import BaseModel

from artemis.context import DeviceContext
from artemis.core.tool_failure import is_tool_failure
from artemis.graph.state import State
from artemis.runtime.session_scope import TaskScope, task_steps
from artemis.tools import scratchpad
from artemis.utils.notes import read_note_content, save_note_content


def context(tmp_path):
    return SimpleNamespace(data_engine=SimpleNamespace(base_dir=tmp_path, get_all_background_tasks=lambda: []), device=DeviceContext(),
                           active_task=TaskScope(str(uuid4()), str(uuid4()), 'Read evidence', effect_policy='read_only'))


def test_main_policy_schema_and_native_goal_explain_persistent_note_restrictions():
    from artemis.runtime.session_orchestration.contract import render_goal
    from artemis.runtime.session_orchestration.models import Completion, TaskSpec
    description = TaskSpec.model_json_schema()['properties']['effect_policy']['description']
    assert 'persistent business-note writes' in description and 'creating audit notes' in description
    spec = TaskSpec(goal='Read only', effect_policy='read_only', execution_timeout_seconds=60,
                    completion=[Completion(id='read', description='Read original evidence')])
    goal = render_goal(spec)
    assert 'exact task_plan' in goal and 'typed finish_output' in goal and 'new audit notes' in goal


@pytest.mark.asyncio
async def test_planner_pure_tool_cannot_create_business_note(tmp_path):
    ctx = context(tmp_path)
    tool = scratchpad.get_save_note_tool_pure(ctx)
    result = await tool.ainvoke(dict(key='calc_task_audit', content='Untrusted new memory'))
    assert is_tool_failure(result)
    assert 'Read-only task' in str(result)
    assert not (tmp_path / 'notes' / 'calc_task_audit.md').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_class,args', [
    (scratchpad.SaveNoteTool, dict(content='changed')),
    (scratchpad.AppendNoteTool, dict(content='changed')),
    (scratchpad.UpdateNoteTool, dict(target='original', replacement='changed')),
    (scratchpad.SaveNotePureTool, dict(content='changed')),
    (scratchpad.AppendNotePureTool, dict(content='changed')),
    (scratchpad.UpdateNotePureTool, dict(target='original', replacement='changed')),
])
async def test_all_note_mutation_paths_leave_existing_business_bytes_unchanged(tmp_path, tool_class, args):
    ctx = context(tmp_path)
    save_note_content(tmp_path, 'original_fact', 'original bytes')
    path = tmp_path / 'notes' / 'original_fact.md'
    before = path.read_bytes()
    result = await tool_class().execute(ctx=ctx, key='original_fact', state=State.initial('read'),
                                        tool_call_id='denied', **args)
    assert is_tool_failure(result) or (isinstance(result, ToolMessage) and result.status == 'error')
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_class,args', [
    (scratchpad.SaveNoteTool, dict(content='- [ ] Observe fresh screen')),
    (scratchpad.AppendNoteTool, dict(content='\n- [ ] Read original history')),
    (scratchpad.UpdateNoteTool, dict(target='Observe', replacement='Inspect')),
    (scratchpad.SaveNotePureTool, dict(content='- [ ] Observe fresh screen')),
    (scratchpad.AppendNotePureTool, dict(content='\n- [ ] Read original history')),
    (scratchpad.UpdateNotePureTool, dict(target='Observe', replacement='Inspect')),
])
async def test_internal_task_plan_remains_writable(tmp_path, tool_class, args):
    ctx = context(tmp_path)
    save_note_content(tmp_path, 'task_plan', '- [ ] Observe')
    before = (tmp_path / 'notes' / 'task_plan.md').read_bytes()
    result = await tool_class().execute(ctx=ctx, key='task_plan', state=State.initial('read'),
                                        tool_call_id='internal', **args)
    assert not is_tool_failure(result)
    if isinstance(result, ToolMessage):
        assert result.status == 'success'
    assert (tmp_path / 'notes' / 'task_plan.md').read_bytes() != before


@pytest.mark.asyncio
async def test_next_normal_task_restores_business_note_permission(tmp_path):
    ctx = context(tmp_path)
    denied = await scratchpad.SaveNotePureTool().execute(ctx=ctx, key='fact', content='old')
    assert is_tool_failure(denied)
    ctx.active_task = TaskScope(str(uuid4()), str(uuid4()), 'Normal write')
    result = await scratchpad.SaveNotePureTool().execute(ctx=ctx, key='fact', content='new')
    assert not is_tool_failure(result)
    assert read_note_content(tmp_path, 'fact') == 'new'


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['click', 'default_api:input_text', 'vendor_mutate', 'backend:vendor_mutate'])
async def test_actions_and_backend_extensions_are_rejected_before_start_or_translation(tmp_path, name):
    from artemis.mcp.action_executor import McpActionExecutor
    ctx = context(tmp_path)
    actuator = SimpleNamespace(controller=None, extensions=lambda: [SimpleNamespace(name='vendor_mutate')])
    executor = McpActionExecutor(ctx, actuator=actuator)
    executor._session_or_start = AsyncMock(side_effect=AssertionError('Device session must not start'))
    executor._translate = MagicMock(side_effect=AssertionError('No action translation'))
    result = await executor.execute(name, dict(target=[100, 100]), 'blocked', State.initial('read'))
    assert result.status == 'error' and 'Read-only task' in result.text_summary
    executor._session_or_start.assert_not_called()
    executor._translate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('short,persistent', [(False, False), (False, True), (True, False)])
async def test_adb_shell_paths_are_rejected_before_driver_or_subprocess(tmp_path, monkeypatch, short, persistent):
    import artemis.tools.command_tool as module
    ctx = context(tmp_path)
    driver = SimpleNamespace(execute_shell=AsyncMock(side_effect=AssertionError('No shell')))
    spawn = AsyncMock(side_effect=AssertionError('No process'))
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', spawn)
    tool = module.RunShortAdbCommandTool() if short else module.RunAdbCommandTool()
    result = await tool.execute(ctx=ctx, driver=driver, CommandLine='input keyevent 3', RunPersistent=persistent)
    assert is_tool_failure(result) and 'Read-only task' in str(result)
    driver.execute_shell.assert_not_called()
    spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['kill', 'send_input'])
async def test_background_process_mutations_are_blocked_before_registry_access(tmp_path, monkeypatch, action):
    import artemis.tools.command_tool as module
    registry = MagicMock(side_effect=AssertionError('No background mutation'))
    monkeypatch.setattr(module, 'get_adb_task_registry', registry)
    result = await module.ManageTaskTool().execute(ctx=context(tmp_path), Action=action, TaskId='old', Input='command')
    assert is_tool_failure(result) and 'Read-only task' in str(result)
    registry.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['list', 'list_finished', 'status'])
async def test_background_process_read_queries_remain_available(tmp_path, action):
    from artemis.tools.command_tool import ManageTaskTool
    result = await ManageTaskTool().execute(ctx=context(tmp_path), Action=action, TaskId='missing')
    assert 'Read-only task' not in str(result)


@pytest.mark.asyncio
async def test_free_python_image_processor_is_blocked_before_model_and_kernel(tmp_path, monkeypatch):
    import artemis.agents.image_processor.image_processor as module
    model = MagicMock(side_effect=AssertionError('No image processor model'))
    kernel = MagicMock(side_effect=AssertionError('No free Python kernel'))
    monkeypatch.setattr(module, 'get_llm', model)
    monkeypatch.setattr(module, 'PythonExecutor', kernel)
    result = await module.ImageProcessor(context(tmp_path)).run('Write a business note', 'unused.jpg')
    assert 'Read-only task' in result['error']
    model.assert_not_called()
    kernel.assert_not_called()


@pytest.mark.asyncio
async def test_readonly_keeps_fresh_screen_note_history_and_enumerated_probe_reads(tmp_path, monkeypatch):
    import artemis.agents.checker.checker as checker
    import artemis.controllers.unified_controller as controller_module
    ctx = context(tmp_path)
    capture = AsyncMock(return_value=SimpleNamespace(base64='fresh-synthetic-pixels', elements='ignored'))
    monkeypatch.setattr(controller_module, 'UnifiedMobileController', lambda ctx: SimpleNamespace(get_screen_data=capture))
    monkeypatch.setattr(checker, 'is_ocr_configured', lambda: False)
    image, _ = await checker._capture_final_screen(ctx)
    assert image == 'fresh-synthetic-pixels'
    save_note_content(tmp_path, 'original', 'Original archived result')
    assert 'Original archived result' in await scratchpad.ReadNotePureTool().execute(ctx=ctx, key='original')
    ctx.data_engine.current_step_number = 1
    ctx.data_engine.get_agent_friendly_steps_in_range = lambda *args: [dict(summary='Original history', extra_metadata=ctx.active_task.metadata())]
    assert task_steps(ctx)[0]['summary'] == 'Original history'
    shell = MagicMock(return_value='Synthetic read-only battery state')
    ctx.get_adb_client = lambda: SimpleNamespace(device=lambda **kwargs: SimpleNamespace(shell=shell))
    assert await checker._execute_probe(ctx, checker.build_probe_argv('battery')) == 'Synthetic read-only battery state'
    shell.assert_called_once_with(['dumpsys', 'battery'])


@pytest.mark.asyncio
async def test_typed_finish_can_write_internal_report_without_business_note_tools(tmp_path):
    from artemis.agents.outputter.outputter import _finish_output_tool, _Investigation
    from artemis.config import OutputConfig
    class Report(BaseModel):
        result: str
    ctx = context(tmp_path)
    investigation = _Investigation({}, State.initial('Read-only report'), object())
    finish = _finish_output_tool(ctx, OutputConfig(structured_output=Report), investigation)
    investigation.tool_map[finish.name] = finish
    await investigation.execute_tool_calls([dict(name='finish_output', id='report', args=dict(
        output=dict(result='Observed synthetic record'), report='Original archived evidence and current observation.'))])
    assert investigation.finished_output == {'result': 'Observed synthetic record'}
    assert (tmp_path / 'notes' / 'output.md').read_text() == 'Original archived evidence and current observation.'


@pytest.mark.asyncio
async def test_worker_propagates_trusted_effect_policy_instead_of_parsing_goal(tmp_path, monkeypatch):
    from artemis.runtime.session_orchestration import worker as module
    from artemis.runtime.session_orchestration.models import Completion, TaskResult, TaskSpec
    bootstrap = {key: str(uuid4()) for key in ('workflow_id', 'child_session_id', 'worker_epoch')}
    bootstrap.update(role_id='A', token='unit-test-only', profile='pro')
    worker = module.Worker(bootstrap)
    worker.send = AsyncMock()
    execute = AsyncMock()
    worker.session = SimpleNamespace(state='READY', options=SimpleNamespace(), execute=execute)
    verifier = SimpleNamespace(result=TaskResult(status='blocked'))
    monkeypatch.setattr(module, 'ContractVerifier', lambda *args: verifier)
    spec = TaskSpec(goal='Observe only', effect_policy='read_only', completion=[Completion(id='fact', description='Observed original evidence')], execution_timeout_seconds=60)
    msg = SimpleNamespace(task_id=str(uuid4()), attempt_id=str(uuid4()), message_id=str(uuid4()))
    await worker.execute(msg, spec, MagicMock())
    scope = execute.await_args.args[1]
    assert scope.effect_policy == 'read_only'
    assert scope.task_id == msg.task_id and scope.attempt_id == msg.attempt_id
