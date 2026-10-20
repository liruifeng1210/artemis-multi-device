"""Post-close bounded checks; only acquire/release our own temporary lock leases."""
import argparse
from datetime import datetime, timezone
import json
import inspect
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import psutil

sys.path.insert(0, str(Path.cwd()))
from artemis.runtime.device_lock import DeviceExecutionLock

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('run',type=Path)
a=parser.parse_args()
if not (a.run/'closed.json').exists():raise SystemExit('Run is not closed; no lock probe performed')
samples=a.run/'process-samples.jsonl'
identities={}
if samples.exists():
    for line in samples.read_text(encoding='utf-8').splitlines():
        for p in json.loads(line)['processes']:identities[(p['pid'],p['created'])]=p
alive=[]
for (pid,created),p in identities.items():
    try:
        current=psutil.Process(pid)
        if abs(current.create_time()-created)<.01:alive.append(p)
    except psutil.Error:pass
locks={}
for serial in ['21850fc5','a8acf864']:
    kwargs={'description':'post-close comparison probe','concurrency_mode':'per_device'}
    if 'consume_queue_ticket' in inspect.signature(DeviceExecutionLock).parameters:
        kwargs['consume_queue_ticket']=False
    else:
        # Never adopt a ticket inherited by this separate observer process.
        os.environ.pop(DeviceExecutionLock.QUEUE_TICKET_ENV,None)
    lock=DeviceExecutionLock(serial,**kwargs)
    row={'owner_before':DeviceExecutionLock.has_owner_record(serial)}
    try:
        lock.acquire(timeout=1)
        row['acquired']=True
    except Exception as exc:row['error']=repr(exc)
    finally:
        lock.release()
    row['owner_after']=DeviceExecutionLock.has_owner_record(serial)
    locks[serial]=row
dbs={}
for p in a.run.rglob('data_engine.db'):
    conn=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)
    dbs[str(p.relative_to(a.run))]={'quick_check':conn.execute('pragma quick_check').fetchall(),
        'before':{t:conn.execute('select count(*) from '+t).fetchone()[0] for t in ['sessions','steps','traces','history_chunks']}}
    conn.close()
time.sleep(10)
for p in a.run.rglob('data_engine.db'):
    conn=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)
    row=dbs[str(p.relative_to(a.run))]
    row['after']={t:conn.execute('select count(*) from '+t).fetchone()[0] for t in ['sessions','steps','traces','history_chunks']}
    row['counts_stable_10s']=row['before']==row['after']
    conn.close()
forward=subprocess.run(['adb','forward','--list'],capture_output=True,text=True,timeout=15)
result={'time':datetime.now(timezone.utc).isoformat(),'surviving_sampled_identities':alive,
    'locks':locks,'databases':dbs,'adb_forwards':forward.stdout.splitlines(),
    'limits':'PID plus create time avoids PID reuse. Only sampled descendants checked; ten seconds of stable row counts is bounded evidence, not a proof of all future writes. Shared adb daemon is pre-existing.'}
(a.run/'cleanup-probe.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
