"""Thread work owned until actual completion, independent of asyncio waiters."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import threading


class BackgroundWork:
    def __init__(self, name, on_error):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._pending = set()
        self._lock = threading.Lock()
        self._on_error = on_error

    def submit(self, fn, *args):
        context = copy_context()
        with self._lock:
            future = self._executor.submit(context.run, fn, *args)
            self._pending.add(future)
        future.add_done_callback(self._finished)

    def _finished(self, future):
        # Publish the error before removing ownership: flush must see both.
        error = future.exception()
        with self._lock:
            if error is not None:
                self._on_error(error)
            self._pending.discard(future)

    @property
    def pending(self):
        with self._lock:
            return bool(self._pending)

    async def flush(self):
        while True:
            with self._lock:
                futures = list(self._pending)
            if not futures:
                return
            # asyncio.wait does not propagate waiter cancellation to the work.
            wrapped = [asyncio.wrap_future(future) for future in futures]
            for future in wrapped:
                future.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
            await asyncio.wait(wrapped)

    def close(self):
        self._executor.shutdown(wait=False)
