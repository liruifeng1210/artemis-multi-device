"""Actual independent DataEngines sharing native SQLite without schema changes."""

import asyncio
import os
import sys
from uuid import UUID

import pytest

from artemis.data_engine.storage import StorageManager
from artemis.runtime.session_orchestration.models import new_id
from artemis.runtime.session_orchestration.paths import absolute_path


@pytest.mark.asyncio
async def test_two_native_data_engines_keep_sqlite_session_ownership(tmp_path):
    root = absolute_path(tmp_path)
    sessions = [new_id(), new_id()]
    code = """
import asyncio, sys
from pathlib import Path
from uuid import UUID
from artemis.context import ArtemisContext, DeviceContext, ExecutionSetup
from artemis.data_engine.engine import DataEngine
from artemis.runtime.session_scope import TaskScope
async def run():
    root, sid = Path(sys.argv[1]), sys.argv[2]
    DataEngine._connect_ipc_locked = lambda *a, **k: False
    ctx = ArtemisContext(device=DeviceContext(),
        execution_setup=ExecutionSetup(traces_path=root, trace_name=sid))
    engine = DataEngine(ctx)
    ctx.data_engine = engine
    engine.start_session(goal=sid, session_id=UUID(sid))
    (root/(sid + ".ready")).touch()
    async with asyncio.timeout(15):
        while len(list(root.glob("*.ready"))) < 2:
            await asyncio.sleep(0.01)
    for index in range(3):
        engine.begin_task(TaskScope(sid, sid, str(index)))
        engine.record_step(summary=sid)
        await engine.flush(10)
        engine.end_task()
    engine.end_session("completed")
    await engine.flush(10)
    await engine.shutdown()
asyncio.run(run())
"""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            str(root),
            sid,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for sid in sessions
    ]
    try:
        for process in processes:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 40)
            assert process.returncode == 0, (
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
            )
        storage = StorageManager(root / "data_engine.db", root)
        for sid in sessions:
            steps = storage.get_steps(UUID(sid))
            assert len(steps) == 3
            assert [s.step_number for s in steps] == [1, 2, 3]
            assert all(
                str(s.session_id) == sid and s.extra_metadata["task_id"] == sid for s in steps
            )
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
