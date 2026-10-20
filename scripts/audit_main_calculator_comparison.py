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

p=argparse.ArgumentParser(description=__doc__);p.add_argument('run',type=Path);a=p.parse_args()
root=a.run
if not (root/'closed.json').exists():raise SystemExit('Run is not closed')
events=[json.loads(s) for f in root.rglob('events.jsonl') for s in f.read_text(encoding='utf-8').splitlines()]
observer=[json.loads(s) for s in (root/'observer-events.jsonl').read_text(encoding='utf-8').splitlines()]
workflow=WorkflowSpec.model_validate(json.loads((root/'request.json').read_text(encoding='utf-8')))
scheduler=Scheduler(workflow)
terminal={e['task_id']:TaskResult.model_validate(e['result']) for e in events if e['kind']=='terminal'}
replay=[]
for i,e in enumerate(x for x in events if x['kind']=='plan_proposal'):
    proposal=PlanProposal.model_validate(e['proposal'])
    if i:
        scheduler.executed.update(terminal)
        scheduler.results=terminal
    candidate=copy.deepcopy(scheduler)
    row={'proposal_index':i+1,'base_revision':proposal.base_plan_revision,'tasks':len(proposal.tasks),
         'diagnostics':len(proposal.diagnostics),'missing_input':proposal.missing_input}
    if proposal.diagnostics or proposal.missing_input:
        row['validation']='not applied by production control flow'
    else:
        try:
            candidate.apply(proposal.tasks,proposal.base_plan_revision,proposal.recoveries)
            row['validation']='accepted';scheduler=candidate
        except ValueError as exc:row['validation']='rejected';row['reason']=str(exc)
    replay.append(row)
request=next(e for e in events if e['kind']=='harness_cancel_request')
ack=json.loads((root/'cancel.json').read_text(encoding='utf-8'))
cancel_id=request['task_id'];cancel_epoch=datetime.fromisoformat(request['time']).timestamp()
dispatch={e['task_id']:e for e in observer if e['kind']=='dispatch_seen'}
actions=[];connections=[]
for db in root.rglob('data_engine.db'):
    c=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
    connections += [dict(r) for r in c.execute('select session_id,pid,start_time,end_time,status from sessions')]
    for r in c.execute("select t.timestamp,t.duration,t.session_id,t.name,s.extra_metadata from traces t left join steps s on s.step_id=t.step_id where t.type='action' order by t.timestamp"):
        r=dict(r);meta=json.loads(r.pop('extra_metadata') or '{}')
        r['task_id']=meta.get('task_id');r['attempt_id']=meta.get('attempt_id');actions.append(r)
    c.close()
late=[r for r in actions if r['task_id'] in terminal and r['timestamp']>datetime.fromisoformat(terminal[r['task_id']].observed_at).timestamp()]
overlaps=[r for r in actions if r['task_id'] in terminal and r['timestamp']+(r['duration'] or 0)>datetime.fromisoformat(terminal[r['task_id']].observed_at).timestamp()]
b_after=[r for r in actions if dispatch.get(r['task_id'],{}).get('role')=='B' and r['timestamp']>cancel_epoch]
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
    'request_to_ack_seconds':(datetime.fromisoformat(ack['time'])-datetime.fromisoformat(request['time'])).total_seconds(),
    'request_to_task_result_seconds':datetime.fromisoformat(terminal[cancel_id].observed_at).timestamp()-cancel_epoch,
    'cancel_task_action_count':sum(r['task_id']==cancel_id for r in actions),
    'cancel_stage':'Planner/LLM wait before first device action; queued/device-operation cancel not exercised',
    'B_action_records_after_A_cancel':b_after,'late_actions_after_task_result':late,
    'action_intervals_ending_after_task_result':overlaps,
    'actions_without_duration':sum(r['duration'] is None for r in actions),
    'boundary_inspections':inspections,
    'limitations':'Native traces omit direct Role/device/task fields; action-task joins use immutable step metadata and worker Session bindings. Boundary inspections can already describe the next task. Zero late records is bounded by trace coverage.'}
(root/'execution-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:result[k] for k in ['planner_replay','final_task_statuses','request_to_ack_seconds','request_to_task_result_seconds','cancel_task_action_count','late_actions_after_task_result']},ensure_ascii=False,indent=2))
