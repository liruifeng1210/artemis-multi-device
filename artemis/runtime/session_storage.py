"""Serialize native SQLite initialization without changing StorageManager.

The operating system releases this advisory lock when a worker exits. Steady
state writes continue to use Artemis's own SQLite concurrency and transactions.
"""

import asyncio
from contextlib import asynccontextmanager
import errno
import os

from artemis.runtime.session_paths import absolute_path


@asynccontextmanager
async def native_storage_startup(root, timeout_seconds=120):
    root = absolute_path(root)
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / ".session-storage-startup.lock").open("a+b", buffering=0)
    acquired = False
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("Native storage initialization lock timed out") from exc
                await asyncio.sleep(0.05)
        yield
    finally:
        try:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
