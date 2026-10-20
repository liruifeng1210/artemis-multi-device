"""Offline audit/replay of a CLOSED Main calculator run; never executes devices."""
import argparse
import copy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from collections import Counter

sys.path.insert(0,str(Path.cwd()))
from artemis.runtime.session_orchestration.models import WorkflowSpec,PlanProposal,TaskResult
from artemis.runtime.session_orchestration.scheduler import Scheduler

p=argparse.ArgumentParser(description=__doc__);p.add_argument('run',type=Path)
p.add_argument('--output',type=Path,help='Optional separate audit path; preserves prior audit evidence')
a=p.parse_args()
root=a.run
if not (root/'closed.json').exists():raise SystemExit('Run is not closed')
events=[json.loads(s) for f in root.rglob('events.jsonl') for s in f.read_text(encoding='utf-8').splitlines()]
observer=[json.loads(s) for s in (root/'observer-events.jsonl').read_text(encoding='utf-8').splitlines()]
workflow=WorkflowSpec.model_validate(json.loads((root/'request.json').read_text(encoding='utf-8')))
scheduler=Scheduler(workflow)
terminal={e['task_id']:TaskResult.model_validate(e['result']) for e in events if e['kind']=='terminal'}
replay=[]
seen_terminal={}
for e in events:
    if e['kind']=='cancel_authorized':
        scheduler.authorized_cancellations.add((e['task_id'],e['attempt_id']))
        continue
    if e['kind']=='terminal':
        seen_terminal[e['task_id']]=TaskResult.model_validate(e['result'])
        scheduler.result_attempts[e['task_id']]=e.get('attempt_id')
        continue
    if e['kind']!='plan_proposal':continue
    proposal=PlanProposal.model_validate(e['proposal'])
    scheduler.executed.update(seen_terminal)
    scheduler.results=dict(seen_terminal)
    candidate=copy.deepcopy(scheduler)
    row={'proposal_index':len(replay)+1,'base_revision':proposal.base_plan_revision,'tasks':len(proposal.tasks),
         'diagnostics':len(proposal.diagnostics),'missing_input':proposal.missing_input}
    if proposal.diagnostics or proposal.missing_input:
        row['validation']='not applied by production control flow'
    else:
        try:
            candidate.apply(proposal.tasks,proposal.base_plan_revision,proposal.recoveries)
            row['validation']='accepted';scheduler=candidate
        except ValueError as exc:row['validation']='rejected';row['reason']=str(exc)
    replay.append(row)
request=next((e for e in events if e['kind']=='harness_cancel_request'),None)
ack=json.loads((root/'cancel.json').read_text(encoding='utf-8')) if (root/'cancel.json').exists() else None
cancel_id=request['task_id'] if request else None
cancel_epoch=datetime.fromisoformat(request['time']).timestamp() if request else None
dispatch={e['task_id']:e for e in observer if e['kind']=='dispatch_seen'}
actions=[];connections=[];cancel_spans=[];unfinished=[]
for db in root.rglob('data_engine.db'):
    c=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
    connections += [dict(r) for r in c.execute('select session_id,pid,start_time,end_time,status from sessions')]
    for r in c.execute("select t.timestamp,t.duration,t.session_id,t.name,s.extra_metadata from traces t left join steps s on s.step_id=t.step_id where t.type='action' order by t.timestamp"):
        r=dict(r);meta=json.loads(r.pop('extra_metadata') or '{}')
        r['task_id']=meta.get('task_id');r['attempt_id']=meta.get('attempt_id');actions.append(r)
    for r in c.execute("select t.trace_id,t.timestamp,t.duration,t.session_id,t.type,t.name,t.status,s.extra_metadata from traces t left join steps s on s.step_id=t.step_id where t.type in ('agent','llm_call','action','tool')"):
        r=dict(r);meta=json.loads(r.pop('extra_metadata') or '{}')
        r['task_id']=meta.get('task_id');r['attempt_id']=meta.get('attempt_id')
        if r['status'] in ('running','retrying'):unfinished.append(r)
        if cancel_epoch is not None and r['task_id']==cancel_id and r['timestamp']<=cancel_epoch:
            if r['duration'] is not None and r['timestamp']+r['duration']>=cancel_epoch:
                cancel_spans.append(r)
    c.close()
late=[r for r in actions if r['task_id'] in terminal and r['timestamp']>datetime.fromisoformat(terminal[r['task_id']].observed_at).timestamp()]
overlaps=[r for r in actions if r['task_id'] in terminal and r['timestamp']+(r['duration'] or 0)>datetime.fromisoformat(terminal[r['task_id']].observed_at).timestamp()]
b_after=[r for r in actions if cancel_epoch is not None and dispatch.get(r['task_id'],{}).get('role')=='B' and r['timestamp']>cancel_epoch]
cancel_stage=('not exercised' if request is None else
    'device operation interval overlaps request' if any(r['type']=='action' for r in cancel_spans) else
    'LLM call interval overlaps request' if any(r['type']=='llm_call' for r in cancel_spans) else
    'not established by persisted intervals; absence of device actions alone does not prove LLM wait')
heartbeat=(request or {}).get('last_worker_status') or {}
heartbeat_traces=[r for r in heartbeat.get('active_traces',[])
    if (r.get('execution') or {}).get('task_id')==cancel_id
    and (r.get('execution') or {}).get('attempt_id')==(request or {}).get('attempt_id')]
if request and not cancel_spans and heartbeat_traces:
    kind='device action' if any(r['type']=='action' for r in heartbeat_traces) else 'LLM call' if any(r['type']=='llm_call' for r in heartbeat_traces) else 'agent/tool work'
    cancel_stage=f'last worker heartbeat reports {kind}; cached observation, not exact cancellation instant'
inspections=[]
for f in (root/'boundaries').glob('*/result.json'):
    row=json.loads(f.read_text(encoding='utf-8'));inspection=row.get('inspection') or {}
    inspections.append({'task_id':row['task']['task_id'],'display_name':row['task']['display_name'],
        'source':inspection.get('source'),'consistency':inspection.get('consistency'),
        'transcript_next_offset':row.get('transcript_next_offset'),
        'inspection_error':row.get('inspection_error')})
result={'offline_only':True,'replay_note':'Replays the production Scheduler validator against recorded model proposals and terminal results. It creates no workflow, device action, or replacement model decision.',
    'planner_replay':replay,'final_task_statuses':dict(Counter(r.status for r in terminal.values())),
    'bindings':list(dispatch.values()),'runtime_interpreters':connections,
    'cancel_request':request,'cancel_ack':ack,
    'request_to_ack_seconds':(datetime.fromisoformat(ack['time'])-datetime.fromisoformat(request['time'])).total_seconds() if request and ack else None,
    'request_to_task_result_seconds':datetime.fromisoformat(terminal[cancel_id].observed_at).timestamp()-cancel_epoch if cancel_id in terminal else None,
    'cancel_task_action_count':sum(r['task_id']==cancel_id for r in actions) if request else None,
    'cancel_stage':cancel_stage,'cancel_overlapping_recorded_spans':cancel_spans,
    'cancel_heartbeat_matching_traces':heartbeat_traces,
    'unfinished_trace_records':unfinished,
    'B_action_records_after_A_cancel':b_after,'late_actions_after_task_result':late,
    'action_intervals_ending_after_task_result':overlaps,
    'actions_without_duration':sum(r['duration'] is None for r in actions),
    'boundary_inspections':inspections,
    'limitations':'Action-task joins use immutable step metadata and worker Session bindings. Boundary inspections can already describe the next task. Cancellation stage requires recorded overlapping intervals; queued cancellation is not inferred. Zero late records is bounded by trace coverage.'}
(a.output or root/'execution-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
print(json.dumps({k:result[k] for k in ['planner_replay','final_task_statuses','request_to_ack_seconds','request_to_task_result_seconds','cancel_task_action_count','late_actions_after_task_result']},ensure_ascii=False,indent=2))
