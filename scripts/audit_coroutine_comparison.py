"""Read-only audit of an exported closed coroutine run; no model/device actions."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('evidence',type=Path)
a=p.parse_args(); root=a.evidence
def read(name):return json.loads((root/name).read_text(encoding='utf-8'))
if not (root/'closed.json').exists():raise SystemExit('closed.json required')
events=read('timeline.json'); results=read('results.json')
traces=[json.loads(line) for line in (root/'trace-timeline.jsonl').read_text(encoding='utf-8').splitlines()]
actions=[r for r in traces if r['type']=='action']
terminals={r['task']:datetime.fromisoformat(r['time']).timestamp() for r in events if r['kind']=='terminal'}
late=[r for r in actions if r['execution'].get('task_id') in terminals and r['timestamp']>terminals[r['execution']['task_id']]]
overlaps=[r for r in actions if r['execution'].get('task_id') in terminals and r['timestamp']+(r.get('duration') or 0)>terminals[r['execution']['task_id']]]
request=next((r for r in events if r['kind']=='cancel_request'),None)
returned=next((r for r in events if r['kind']=='cancel_return'),None)
cancel_actions=[r for r in actions if request and r['execution'].get('task_id')==request['task']]
after=[r for r in actions if request and r['execution'].get('role_id')=='B' and r['timestamp']>datetime.fromisoformat(request['time']).timestamp()]
boundaries=[]
for f in sorted((root/'artifacts').rglob('boundary.json')):
 r=json.loads(f.read_text(encoding='utf-8'))
 boundaries.append({'path':f.relative_to(root).as_posix(),**{k:v for k,v in r.items() if k not in ['conversation_tail','continuity']}})
groups=defaultdict(list)
for b in boundaries:groups[b['session']].append(b)
resource_keys=['context_id','driver_id','ledger_id']
identity={sid:{k:len({str(r.get(k)) for r in rows})==1 for k in resource_keys} for sid,rows in groups.items()}
db=read('database-summary.json')
checks=Counter()
for r in results:checks.update((r.get('native_run_outcome') or {}).get('tests',{}) and {k:v for k,v in r['native_run_outcome']['tests'].items() if k in ['passed','failed','inconclusive','unchecked']})
out={'read_only':True,'execution_status_counts':dict(Counter(r['status'] for r in results)),
 'native_check_totals':dict(checks),'cancel_request':request,'cancel_return':returned,
 'cancel_latency_seconds':(datetime.fromisoformat(returned['time'])-datetime.fromisoformat(request['time'])).total_seconds() if request and returned else None,
 'cancel_task_device_actions':cancel_actions,
 'cancel_stage':'LLM/planner wait before first action' if request and not cancel_actions else 'inspect trace before classifying',
 'B_actions_after_A_cancel':after,'late_actions_after_task_terminal':late,
 'action_intervals_ending_after_task_terminal':overlaps,
 'actions_without_duration':sum(r.get('duration') is None for r in actions),
 'persistent_resource_boundaries':boundaries,'resource_identity_unchanged_per_session':identity,
 'history_chunk_status_counts':dict(Counter(r['status'] for r in db['history_chunks'])),
 'history_chunk_latest_range_status_counts':db.get('history_chunk_latest_range_status_counts'),
 'task_start_state_records':len(read('task-start-states.json')) if (root/'task-start-states.json').exists() else 0,
 'limitations':['Action timestamps are trace records, not device-side atomic commits.',
 'Template-context background counters do not enumerate every active task context.',
 'Activation observer in a2afa8d lacked parent mkdir; missing files are an instrumentation gap. Graph-start log evidence is separate.',
 'Queue and in-flight device-operation cancellation were not exercised. No crash injection.']}
(root/'execution-audit.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
print(json.dumps({k:out[k] for k in ['execution_status_counts','native_check_totals','cancel_latency_seconds','resource_identity_unchanged_per_session','task_start_state_records']},ensure_ascii=False))
