"""The observer captures the existing settled session before production close."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from examples import calculator_main_comparison as module


def observed(tmp_path, monkeypatch, *, state='READY'):
    events = []
    async def close(self, reason):
        events.append(('close', reason))
        self.closed_report = ['closed']
        return self.closed_report
    monkeypatch.setattr(module.MainSession, '_close', close)
    main = module.ObservedMainSession(tmp_path)
    worker = SimpleNamespace(session_id='existing-session', state=state,
                             process=SimpleNamespace(pid=123, returncode=None))
    main.workers = {'A': worker}
    main.dispatched_workers = {('last-task', 'last-attempt'): worker}
    main.scheduler = SimpleNamespace(results={'last-task': SimpleNamespace(observed_at='2026-10-21T01:00:00+00:00')},
                                     result_attempts={'last-task': 'last-attempt'})
    return main, events


def page(messages, *, cursor=None, snapshot='immutable-snapshot', total=2, captured=2, truncated=False,
         captured_at='2026-10-21T01:00:01+00:00', staged=False, offset=0):
    return dict(snapshot_id=snapshot, captured_at=captured_at, consistency='settled', next_cursor=cursor,
                sections=dict(transcript=dict(messages=messages, total=total, captured_messages=captured, offset=offset,
                                               capture_truncated=truncated, has_staged_turn=staged)))


@pytest.mark.asyncio
async def test_final_pages_are_from_one_existing_snapshot_after_terminal_before_real_close(tmp_path, monkeypatch):
    main, events = observed(tmp_path, monkeypatch)
    responses = [page([dict(content=f'message-{index}') for index in range(100)], total=101, captured=101,
                      cursor=dict(snapshot_id='immutable-snapshot', offset=100)),
                 page([dict(content='last [truncated]')], total=101, captured=101, offset=100)]
    async def inspect(role, query):
        events.append(('inspect', role, query))
        return responses.pop(0)
    main.inspect = AsyncMock(side_effect=inspect)
    assert await main._close('finished') == ['closed']
    assert [row[0] for row in events] == ['inspect', 'inspect', 'close']
    assert main.inspect.await_args_list[1].args == ('A', dict(snapshot_id='immutable-snapshot', offset=100, limit=100))
    manifest = json.loads((tmp_path / 'final-inspections/A/manifest.json').read_text())
    assert manifest['complete'] and manifest['observed_messages'] == manifest['total'] == 101
    assert manifest['last_terminal'] == dict(task_id='last-task', attempt_id='last-attempt', observed_at='2026-10-21T01:00:00+00:00')
    assert manifest['captured_at'] == '2026-10-21T01:00:01+00:00'
    assert manifest['session_id'] == 'existing-session'
    assert manifest['content_truncation_markers'] == 1
    assert 'does not mean lossless' in manifest['content_limitations']
    await main._close('again')
    assert main.inspect.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['busy', 'dead', 'inflight', 'no_terminal'])
async def test_unsettled_or_unowned_workers_are_skipped_without_preventing_close(tmp_path, monkeypatch, mode):
    main, events = observed(tmp_path, monkeypatch)
    future = None
    if mode == 'busy':
        main.workers['A'].state = 'EXECUTING'
    elif mode == 'dead':
        main.workers['A'].process.returncode = 1
    elif mode == 'inflight':
        future = asyncio.get_running_loop().create_future()
        main.running = {'last-task': ('last-attempt', future)}
    else:
        main.scheduler.results.clear()
    main.inspect = AsyncMock(side_effect=AssertionError('No live observation'))
    await main._close('finished')
    main.inspect.assert_not_awaited()
    assert events[-1] == ('close', 'finished')
    manifest = json.loads((tmp_path / 'final-inspections/A/manifest.json').read_text())
    assert not manifest['complete'] and 'skipped' in manifest
    if future:
        future.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [
    page([dict(content='one')], total=3, captured=1, truncated=True),
    page([dict(content='one')], total=3, captured=3),
    page([dict(content='one')], total=1, captured=1, staged=True),
    page([dict(content='old')], captured_at='2026-10-21T00:59:59+00:00'),
])
async def test_omitted_staged_or_preterminal_snapshot_is_never_marked_complete(tmp_path, monkeypatch, invalid):
    main, events = observed(tmp_path, monkeypatch)
    main.inspect = AsyncMock(return_value=invalid)
    await main._close('finished')
    manifest = json.loads((tmp_path / 'final-inspections/A/manifest.json').read_text())
    assert not manifest['complete'] and 'error' in manifest
    assert events[-1][0] == 'close'


@pytest.mark.asyncio
async def test_inspection_timeout_and_save_failure_never_skip_production_close(tmp_path, monkeypatch):
    main, events = observed(tmp_path, monkeypatch)
    main.inspect = AsyncMock(side_effect=TimeoutError('synthetic'))
    await main._close('finished')
    manifest = json.loads((tmp_path / 'final-inspections/A/manifest.json').read_text())
    assert manifest['error'] == 'TimeoutError' and not manifest['complete']
    assert events[-1][0] == 'close'
    main.closed_report = None
    main._final_inspection_attempted = False
    monkeypatch.setattr(module, 'save', lambda *args: (_ for _ in ()).throw(OSError('synthetic')))
    assert await main._close('disk-failed') == ['closed']
    assert events[-1] == ('close', 'disk-failed')


@pytest.mark.asyncio
async def test_cancellation_closes_workers_then_propagates(tmp_path, monkeypatch):
    main, events = observed(tmp_path, monkeypatch)
    started = asyncio.Event()
    async def inspect(*args):
        started.set()
        await asyncio.Event().wait()
    main.inspect = AsyncMock(side_effect=inspect)
    pending = asyncio.create_task(main._close('cancelled'))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert events[-1] == ('close', 'cancelled')


@pytest.mark.asyncio
async def test_snapshot_identity_or_cursor_repetition_cannot_mix_pages(tmp_path, monkeypatch):
    main, events = observed(tmp_path, monkeypatch)
    cursor = dict(snapshot_id='immutable-snapshot', offset=100)
    main.inspect = AsyncMock(side_effect=[page([{} for _ in range(100)], total=100, captured=100, cursor=cursor),
                                         page([], total=100, captured=100, offset=100, cursor=cursor)])
    await main._close('finished')
    manifest = json.loads((tmp_path / 'final-inspections/A/manifest.json').read_text())
    assert manifest['error'] == 'invalid_or_repeated_inspection_cursor'
    assert not manifest['complete'] and main.inspect.await_count == 2


@pytest.mark.asyncio
async def test_jumping_cursor_cannot_omit_messages_even_when_count_claims_match(tmp_path, monkeypatch):
    main, events = observed(tmp_path, monkeypatch)
    main.inspect = AsyncMock(return_value=page([{}], total=1, captured=1,
        cursor=dict(snapshot_id='immutable-snapshot', offset=100)))
    await main._close('finished')
    manifest = json.loads((tmp_path / 'final-inspections/A/manifest.json').read_text())
    assert manifest['error'] == 'non_contiguous_inspection_cursor' and not manifest['complete']


@pytest.mark.asyncio
async def test_snapshot_metadata_cannot_change_between_pages(tmp_path, monkeypatch):
    main, events = observed(tmp_path, monkeypatch)
    main.inspect = AsyncMock(side_effect=[
        page([{} for _ in range(100)], total=101, captured=101, cursor=dict(snapshot_id='immutable-snapshot', offset=100)),
        page([{}], total=102, captured=102, offset=100),
    ])
    await main._close('finished')
    manifest = json.loads((tmp_path / 'final-inspections/A/manifest.json').read_text())
    assert manifest['error'] == 'inspection_snapshot_metadata_changed' and not manifest['complete']
