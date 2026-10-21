"""Closed-run cancellation attribution must not invent identity from stale steps."""
import importlib.util
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest


@pytest.fixture
def audit():
    path = Path(__file__).resolve().parents[2] / 'scripts/audit_main_calculator_comparison.py'
    spec = importlib.util.spec_from_file_location('comparison_audit_tested', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(execution=None, step=None, **overrides):
    return dict(trace_id='span', timestamp=10.0, duration=5.0, session_id='session', step_id=None,
                type='llm_call', name='Synthetic', status='cancelled',
                payload=json.dumps({'execution': execution} if execution is not None else {}),
                extra_metadata=json.dumps(step) if step is not None else None, **overrides)


def test_stepless_llm_and_planner_intervals_use_exact_payload_owner(audit):
    execution = dict(task_id='task', attempt_id='attempt', session_id='session')
    for kind in ('llm_call', 'agent'):
        raw = record(execution)
        raw['type'] = kind
        row = audit.resolve_trace_owner(raw)
        assert row['owner_source'] == 'payload.execution'
        assert row['step_owner_state'] == 'missing' and row['owner_trusted']
        assert audit.span_overlaps_request(row, dict(task_id='task', attempt_id='attempt'), 12)
        assert 'payload' not in row  # Public summary must not copy model/tool contents.


@pytest.mark.parametrize('cancel_request', [dict(task_id='task', attempt_id='other'), dict(task_id='other', attempt_id='attempt'), dict(task_id='task')])
def test_cancel_requires_both_exact_identifiers(audit, cancel_request):
    row = audit.resolve_trace_owner(record(dict(task_id='task', attempt_id='attempt')))
    assert not audit.span_overlaps_request(row, cancel_request, 12)


def test_payload_step_conflict_is_reported_and_not_trusted(audit):
    row = audit.resolve_trace_owner(record(dict(task_id='task', attempt_id='attempt'), dict(task_id='old', attempt_id='old-attempt')))
    assert row['owner_source'] == 'payload.execution'
    assert not row['owner_trusted']
    assert row['ownership_conflicts'] == ['payload_step_task_attempt_mismatch']
    assert not audit.span_overlaps_request(row, dict(task_id='task', attempt_id='attempt'), 12)


def test_legacy_step_identity_is_used_only_when_payload_has_neither_identifier(audit):
    legacy = audit.resolve_trace_owner(record({}, dict(task_id='task', attempt_id='attempt')))
    assert legacy['owner_source'] == 'legacy_step.extra_metadata' and legacy['owner_trusted']
    partial = audit.resolve_trace_owner(record(dict(task_id='task'), dict(task_id='task', attempt_id='attempt')))
    assert partial['owner_source'] == 'partial_payload.execution'
    assert not partial['owner_trusted'] and partial['attempt_id'] is None
    assert not audit.span_overlaps_request(partial, dict(task_id='task', attempt_id='attempt'), 12)


@pytest.mark.parametrize('field', ['session_id', 'child_session_id'])
def test_payload_session_conflict_is_never_attributed_to_cancel(audit, field):
    row = audit.resolve_trace_owner(record(dict(task_id='task', attempt_id='attempt', **{field: 'wrong-session'})))
    assert not row['owner_trusted']
    assert 'payload_' + field + '_mismatch' in row['ownership_conflicts']


@pytest.mark.parametrize('duration', [None, -1, float('nan'), float('inf')])
def test_unknown_negative_and_nonfinite_durations_do_not_prove_overlap(audit, duration):
    raw = record(dict(task_id='task', attempt_id='attempt'))
    raw['duration'] = duration
    row = audit.resolve_trace_owner(raw)
    assert not audit.span_overlaps_request(row, dict(task_id='task', attempt_id='attempt'), 12)


def test_closed_sqlite_run_reports_stepless_span_and_keeps_conflicting_unfinished_row(audit, tmp_path):
    from artemis.runtime.session_orchestration.models import WorkflowSpec, RoleSpec
    task, attempt, old_attempt = str(uuid4()), str(uuid4()), str(uuid4())
    workflow = WorkflowSpec(goal='Synthetic offline audit', roles=[RoleSpec(role_id='A', device_id='synthetic')],
                            root=tmp_path, max_workflow_seconds=60, max_artifact_bytes=100000)
    (tmp_path / 'closed.json').write_text('{}')
    (tmp_path / 'request.json').write_text(workflow.model_dump_json())
    (tmp_path / 'observer-events.jsonl').write_text('')
    (tmp_path / 'events.jsonl').write_text(json.dumps(dict(kind='harness_cancel_request', task_id=task, attempt_id=attempt,
                                                         time='1970-01-01T00:00:12+00:00')) + '\n')
    with sqlite3.connect(tmp_path / 'data_engine.db') as connection:
        connection.executescript('''
            CREATE TABLE sessions(session_id TEXT,pid INTEGER,start_time REAL,end_time REAL,status TEXT);
            CREATE TABLE steps(step_id TEXT,extra_metadata TEXT);
            CREATE TABLE traces(trace_id TEXT,timestamp REAL,duration REAL,session_id TEXT,step_id TEXT,type TEXT,name TEXT,status TEXT,payload TEXT);
        ''')
        connection.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', ('session', 123, 1, 20, 'closed'))
        connection.execute('INSERT INTO steps VALUES(?,?)', ('stale', json.dumps(dict(task_id=task, attempt_id=old_attempt))))
        for span, step, status in [('good', None, 'cancelled'), ('conflict', 'stale', 'running')]:
            connection.execute('INSERT INTO traces VALUES(?,?,?,?,?,?,?,?,?)',
                (span, 10, 5, 'session', step, 'llm_call', 'Synthetic', status,
                 json.dumps(dict(execution=dict(task_id=task, attempt_id=attempt, session_id='session')))))
    output = tmp_path / 'derived-audit.json'
    audit.main([str(tmp_path), '--output', str(output)])
    result = json.loads(output.read_text())
    assert [row['trace_id'] for row in result['cancel_overlapping_recorded_spans']] == ['good']
    assert result['cancel_stage'] == 'LLM call interval overlaps request'
    assert [row['trace_id'] for row in result['ownership_conflicts']] == ['conflict']
    assert [row['trace_id'] for row in result['unfinished_trace_records']] == ['conflict']
