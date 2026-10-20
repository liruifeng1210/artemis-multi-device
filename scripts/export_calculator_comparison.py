"""Export bounded calculator evidence; never copy databases, auth or full prompts."""
import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import statistics


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if not re.search(
            r'(^|_)(api_key|password|secret|credential|reconnect_secret|token)$', k, re.I)
            and k not in {'screenshot_b64','latest_screenshot','latest_ui_hierarchy','xml_hierarchy','indexed_elements','operator_raw_data','messages','operator_raw_thinking',
                          'operator_native_thinking','transcript','last_state','inspection','ui'}}
    if isinstance(value, list):
        return [clean(x) for x in value]
    if isinstance(value, str):
        if value.startswith('data:image'):
            return '[image omitted]'
        value = re.sub(r'[A-Za-z]:[\\/](?:Users[\\/][^\\/\s"\']+)', '<USER>', value)
        value = value.replace('E:\\workplace\\artemis\\scratch\\','<LOCAL_EVIDENCE>/').replace('E:/workplace/artemis/scratch/','<LOCAL_EVIDENCE>/')
        return value[:16000] + (' [truncated]' if len(value)>16000 else '')
    return value


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')


def calculator_note(key):
    key=str(key or '').removesuffix('.md')
    return key in {'task_plan','session_context','session_progress','output','B','A_role_history'} or key.startswith(('calculator','calculation','cancel_a_plus10'))


def export(source, target, log=None):
    target.mkdir(parents=True, exist_ok=True)
    omissions_file=source/'evidence-omissions.json'
    omissions=json.loads(omissions_file.read_text(encoding='utf-8')) if omissions_file.exists() else []
    omitted_paths={r['source_relative_path'] for r in omissions}
    artifact_index={}
    def artifact_path(relative):
        compact=Path('artifacts',*(part[:8] if re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',part) else part for part in relative.parts))
        key=compact.as_posix();original=relative.as_posix()
        if key in artifact_index and artifact_index[key]!=original:
            raise ValueError('Evidence path prefix collision: '+key)
        artifact_index[key]=original
        return target/compact
    for name in ['sessions.json','timeline.json','config.json','closed.json','request.json',
                 'outcome.json','cancel.json','cancel-error.json','error.json','model-process-evidence.json',
                 'cleanup-probe.json','source.json','ownership-audit.json','execution-audit.json','evidence-omissions.json',
                 'checker-observation-conflict.json']:
        p = source / name
        if p.exists(): write(target / name, json.loads(p.read_text(encoding='utf-8')))
    result_file = source / 'results.json'
    if result_file.exists():
        results = json.loads(result_file.read_text(encoding='utf-8'))
        for row in results:
            state = row.pop('state', None) or {}
            row['native_run_outcome'] = state.get('run_outcome')
            row['checker_success_flag'] = state.get('checker_success')
        write(target / 'results.json', results)
    samples = source / 'process-samples.jsonl'
    if samples.exists():
        rows = [json.loads(s) for s in samples.read_text(encoding='utf-8').splitlines()]
        if rows:
            def stats(values):
                return {'min':min(values),'median':statistics.median(values),'max':max(values)}
            write(target / 'process-metrics.json', {'sample_interval_s':5,'samples':len(rows),
                'first':rows[0]['time'],'last':rows[-1]['time'],
                'method':'psutil current real interpreter plus recursive children; sampled RSS sum, not unique/private memory. Main venv launcher and shared adb daemon excluded; child venv launchers and transient CLI/conhost/ADB processes included when sampled. Nominal 5s; MP boundary capture can delay samples.',
                'total_processes':stats([len(r['processes']) for r in rows]),
                'python_processes':stats([sum(p['name'].lower()=='python.exe' for p in r['processes']) for r in rows]),
                'codex_processes':stats([sum(p['name'].lower()=='codex.exe' for p in r['processes']) for r in rows]),
                'summed_rss_bytes':stats([sum(p['rss'] for p in r['processes']) for r in rows]),
                'peak_sample':max(rows,key=lambda r:sum(p['rss'] for p in r['processes']))})
            # Compact raw samples make the resource claims independently reproducible.
            (target / 'process-samples.jsonl').write_text(samples.read_text(encoding='utf-8'),encoding='utf-8',newline='\n')
    for p in source.rglob('screen.png'):
        if p.relative_to(source).as_posix() in omitted_paths:continue
        if 'traces' not in p.relative_to(source).parts:
            out = target / p.relative_to(source)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p,out)
    for p in source.rglob('contract-screen.jpg'):
        if p.relative_to(source).as_posix() in omitted_paths:continue
        out=artifact_path(p.relative_to(source))
        out.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(p,out)
    # Export explicit task manifests/checks and calculator notes, not arbitrary runtime files.
    names = {'task_manifest.json','manifest.json','task_result.json','result.json','contract_report.json',
             'workflow.json','plan.json','boundary.json','activation.json','worker.json','request.json','accepted.json','run_outcome.json',
             'session_context.md','session_progress.md','task_plan.md'}
    for p in source.rglob('*'):
        if not p.is_file() or (p.name not in names and not (p.suffix=='.md' and 'notes' in p.relative_to(source).parts and calculator_note(p.name))) or p.stat().st_size > (5000000 if p.suffix=='.json' else 300000):
            continue
        rel = p.relative_to(source)
        if p.suffix == '.json':
            try:
                value=json.loads(p.read_text(encoding='utf-8'))
                if p.name in {'task_result.json','result.json'} and isinstance(value,dict) and isinstance(value.get('state'),dict):
                    state=value.pop('state')
                    value['native_run_outcome']=state.get('run_outcome')
                    value['checker_success_flag']=state.get('checker_success')
                if isinstance(value,dict) and isinstance(value.get('inspection'),dict):
                    inspection=value['inspection']
                    value['inspection_source']=inspection.get('source')
                    value['inspection_consistency']=inspection.get('consistency')
                    sections=inspection.get('sections') or {}
                    state=sections.get('last_state') or {}
                    value['observed_temporary_state']={k:state.get(k) for k in [
                        'initial_goal','injected_instruction','user_stop_requested','checker_success',
                        'run_outcome','current_step_id','open_incident','subagent_calls']}
                write(artifact_path(rel), value)
            except (ValueError, UnicodeError): pass
        else:
            out=artifact_path(rel);out.parent.mkdir(parents=True,exist_ok=True)
            out.write_text(clean(p.read_text(encoding='utf-8')).rstrip('\r\n')+'\n',encoding='utf-8',newline='\n')
    for p in source.rglob('events.jsonl'):
        write(target/'events.json', [json.loads(s) for s in p.read_text(encoding='utf-8').splitlines()])
    observer=source/'observer-events.jsonl'
    if observer.exists():write(target/'observer-events.json',[json.loads(s) for s in observer.read_text(encoding='utf-8').splitlines()])
    check_files=[]
    for p in source.rglob('check_*.jsonl'):
        records=[json.loads(s) for s in p.read_text(encoding='utf-8').splitlines()]
        if p.name=='check_streams.jsonl':
            for record in records:
                record['segments']=[s for s in record.get('segments',[]) if s.get('role')=='answer']
        check_files.append({'path':p.relative_to(source).as_posix(),'records':records})
    if check_files:write(target/'check-evidence.json',check_files)
    expected={}
    if (source/'sessions.json').exists():
        for role,binding in json.loads((source/'sessions.json').read_text(encoding='utf-8')).items():
            expected[binding['session_id']]={'role_id':role,'device_id':binding['device']}
    if (source/'request.json').exists():
        devices={r['role_id']:r['device_id'] for r in json.loads((source/'request.json').read_text(encoding='utf-8'))['roles']}
        for p in source.rglob('worker.json'):
            binding=json.loads(p.read_text(encoding='utf-8'))
            expected[binding['child_session_id']]={'role_id':binding['role_id'],'device_id':devices[binding['role_id']]}
    safe_calls=[]
    state_boundaries=[]
    for p in [*source.rglob('*.log'),*([log] if log else [])]:
        log_label=p.relative_to(source).as_posix() if p.is_relative_to(source) else 'external-'+p.name
        for line in p.read_text(encoding='utf-8',errors='replace').splitlines():
            m=re.search(r'CODEX_CALL (?:start id=[a-f0-9]+ pid=\d+ model=[\w.-]+ effort=\w+|(?:exit|reaped) id=[a-f0-9]+ pid=\d+ (?:code=-?\d+|reason=cancel_or_timeout))',line)
            if m:safe_calls.append({'log':log_label,'event':m.group(0)})
            marker='Invoking graph with input: '
            if marker in line:
                try:
                    state=ast.literal_eval(line.split(marker,1)[1])
                    attempt=re.search(r'\[([0-9a-f-]{36})\]',line)
                    state_boundaries.append({'log':log_label,
                        'attempt':attempt.group(1) if attempt else None,
                        'state':{k:state.get(k) for k in ['initial_goal','injected_instruction',
                            'user_stop_requested','checker_success','run_outcome','current_step_id',
                            'open_incident','subagent_calls']},
                        'screenshot_present':state.get('latest_screenshot') is not None,
                        'hierarchy_present':state.get('latest_ui_hierarchy') is not None})
                except (ValueError,SyntaxError):pass
    if safe_calls:write(target/'model-process-log.json',safe_calls)
    if state_boundaries:write(target/'task-start-states.json',state_boundaries)
    for db in source.rglob('data_engine.db'):
        connection=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
        connection.row_factory=sqlite3.Row
        sessions=[dict(r) for r in connection.execute('select session_id,initial_goal,start_time,end_time,status,pid from sessions')]
        steps=[dict(r) for r in connection.execute('select step_id,session_id,step_number,timestamp,action_taken,summary,extra_metadata from steps order by timestamp')]
        for row in steps:
            row['extra_metadata']=json.loads(row['extra_metadata'] or '{}')
        chunks=[dict(r) for r in connection.execute('select chunk_id,session_id,start_step_id,end_step_id,start_step_number,end_step_number,source_step_ids,version,status,created_at,band1,band2 from history_chunks')]
        # Storage is append-only: pending v1 and ready v2 have distinct row IDs.
        # Match storage.get_history_chunks's newest-version-per-step-range view.
        latest_chunks={}
        for chunk in chunks:
            key=(chunk['session_id'],chunk['start_step_number'],chunk['end_step_number'])
            previous=latest_chunks.get(key)
            if previous is None or (chunk['version'],chunk['created_at']) >= (previous['version'],previous['created_at']):
                latest_chunks[key]=chunk
        jobs=[dict(r) for r in connection.execute('select task_id,session_id,summary,status,start_time,end_time,trace_id from background_tasks')]
        traces=[]
        for r in connection.execute('select * from traces order by timestamp'):
            row=dict(r);payload=json.loads(row.pop('payload') or '{}')
            row['execution']=payload.get('execution',{})
            if payload.get('termination_reason'):
                row['termination_reason']=payload['termination_reason']
            if row['name']=='compress_history':
                row['compression_state']={k:v for k,v in (payload.get('args') or {}).items()
                    if k in {'phase','chunk_id','start_step_id','end_step_id','start_step_number','end_step_number'}}
            if row['type']=='action': row['action']=payload.get('action');row['success']=payload.get('success')
            if row['type']=='log' and 'CODEX_CALL' in str(payload.get('message','')):
                row['model_process_event']=payload['message']
            if row['type']=='tool' and row['name'] in ['save_note','update_note','read_note','append_note']:
                args=payload.get('args') or {}
                row['note_args']=args if calculator_note(args.get('key')) else {'key':args.get('key'),'content_omitted':True}
            traces.append(row)
        # Referential and task/step ownership audit, without inferring running OS work from trace status.
        step_by_id={r['step_id']:r for r in steps};trace_by_id={r['trace_id']:r for r in traces}
        errors=[];gaps=[]
        for row in traces:
            meta=row['execution'];sid=row['session_id']
            if meta.get('session_id') and meta['session_id']!=sid: errors.append([row['trace_id'],'session mismatch'])
            for field,wanted in expected.get(sid,{}).items():
                if meta.get(field) and meta[field]!=wanted:errors.append([row['trace_id'],field+' mismatch'])
            for key,table in [('step_id',step_by_id),('parent_trace_id',trace_by_id)]:
                linked=table.get(row[key]);
                if linked and linked['session_id']!=sid:errors.append([row['trace_id'],'foreign '+key])
            linked=step_by_id.get(row['step_id'])
            if linked and meta.get('task_id'):
                sm=linked['extra_metadata']
                if (meta.get('task_id'),meta.get('attempt_id'))!=(sm.get('task_id'),sm.get('attempt_id')):
                    errors.append([row['trace_id'],'task-step mismatch'])
            if not meta.get('device_id'):gaps.append(row['trace_id'])
        check_errors=[];check_without_task_meta=0
        for file in check_files:
            parts=Path(file['path']).parts
            if 'tasks' not in parts:continue
            ix=parts.index('tasks')
            sid,task,attempt=parts[ix-1],parts[ix+1],parts[ix+2]
            for record in file['records']:
                trace=trace_by_id.get(record.get('trace_id'))
                if not trace:
                    check_errors.append([file['path'],'missing trace']);continue
                if trace['session_id']!=sid:check_errors.append([file['path'],'foreign trace Session'])
                meta=trace['execution']
                if not meta.get('task_id'):check_without_task_meta+=1
                elif (meta.get('task_id'),meta.get('attempt_id'))!=(task,attempt):
                    check_errors.append([file['path'],'foreign task/attempt'])
        write(target/'database-summary.json', {'sessions':sessions,'steps':steps,'history_chunks':chunks,
            'history_chunk_all_version_status_counts':dict(Counter(r['status'] for r in chunks)),
            'history_chunk_latest_range_status_counts':dict(Counter(r['status'] for r in latest_chunks.values())),
            'history_chunk_version_note':'Append-only versions have distinct row IDs; historical pending v1 is superseded when the same Session/step range has ready v2. Current view follows storage.get_history_chunks.',
            'background_jobs':jobs,'trace_counts':dict(Counter(r['type'] for r in traces)),
            'trace_session_counts':dict(Counter(str(r['session_id']) for r in traces)),
            'running_trace_ids':[r['trace_id'] for r in traces if r['status']=='running'],
            'expected_bindings':expected,'referential_or_task_owner_errors':errors,'trace_device_metadata_gaps':gaps,
            'check_file_owner_errors':check_errors,'check_records_without_direct_task_metadata':check_without_task_meta,
            'limitation':'No task/step mismatch does not prove all prompt/note semantics isolated; running trace is persisted metadata, not OS liveness.'})
        (target/'trace-timeline.jsonl').write_text('\n'.join(json.dumps(clean(r),ensure_ascii=False) for r in traces)+'\n',encoding='utf-8',newline='\n')
        connection.close()
    write(target/'artifact-index.json',[{'public_path':k,'original_relative_path':v} for k,v in sorted(artifact_index.items())])
    entries=[]
    for p in target.rglob('*'):
        if p.is_file() and p.name!='sha256.json':entries.append({'path':p.relative_to(target).as_posix(),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    write(target/'sha256.json',entries)
    print(json.dumps({'target':str(target),'files':len(entries),'bytes':sum(e['bytes'] for e in entries)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('target',type=Path)
    parser.add_argument('--log',type=Path)
    a=parser.parse_args();export(a.source,a.target,a.log)
