"""Bounded all-or-release reservation using the existing device lock protocol."""

import asyncio
import threading
import time

from artemis.runtime.device_lock import DeviceExecutionLock
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


class DeviceSetLease:
    def __init__(
        self, serials, session_id, *, timeout=60, max_concurrency=None, concurrency_mode=None
    ):
        mode = concurrency_mode if concurrency_mode == "global" or not max_concurrency else None
        self.locks = [
            DeviceExecutionLock(
                serial,
                session_id=session_id,
                concurrency_mode=mode,
                max_concurrency=max_concurrency,
                description=f"Multi-device case {session_id}",
            )
            for serial in sorted(set(serials))
        ]
        self.timeout = timeout
        self.held = []
        self.cancelled = threading.Event()
        self.session_id = session_id

    def acquire(self):
        deadline = time.monotonic() + self.timeout
        started = time.monotonic()
        next_report = started
        attempts = 0
        logger.info(
            f"[multi-device] lease.wait run_id={self.session_id} devices={[lock.device_id for lock in self.locks]} timeout_s={self.timeout}"
        )
        while not self.cancelled.is_set():
            attempts += 1
            try:
                for lock in self.locks:
                    lock.acquire(blocking=False)
                    self.held.append(lock)
                logger.info(
                    f"[multi-device] lease.acquired run_id={self.session_id} device_count={len(self.held)} attempts={attempts} elapsed_ms={int((time.monotonic() - started) * 1000)}"
                )
                return
            except Exception as exc:
                self.release()
                # An inherited queue ticket is consumed/removed on the first
                # attempt. All-or-release retries need fresh tickets for every
                # device, including ones acquired before a later device failed.
                for lock in self.locks:
                    lock.queue_ticket = None
                    lock._initial_queue_ticket = None
                if time.monotonic() >= deadline:
                    logger.error(
                        f"[multi-device] lease.timeout run_id={self.session_id} attempts={attempts} error_type={type(exc).__name__}"
                    )
                    raise
                if time.monotonic() >= next_report:
                    logger.info(
                        f"[multi-device] lease.pending run_id={self.session_id} attempts={attempts} elapsed_ms={int((time.monotonic() - started) * 1000)} error_type={type(exc).__name__}"
                    )
                    next_report = time.monotonic() + 5
                self.cancelled.wait(min(0.2, max(0, deadline - time.monotonic())))
        logger.warning(
            f"[multi-device] lease.cancelled run_id={self.session_id} attempts={attempts}"
        )
        raise RuntimeError("Multi-device reservation cancelled")

    def release(self):
        errors = []
        count = len(self.held)
        while self.held:
            try:
                self.held.pop().release()
            except Exception as exc:
                errors.append(exc)
        if errors:
            logger.error(
                f"[multi-device] lease.release_failed run_id={self.session_id} count={count} errors={len(errors)}"
            )
            raise ExceptionGroup("Failed to release device leases", errors)
        if count:
            logger.info(f"[multi-device] lease.released run_id={self.session_id} count={count}")

    async def __aenter__(self):
        task = asyncio.create_task(asyncio.to_thread(self.acquire))
        try:
            await asyncio.shield(task)
        except BaseException:
            self.cancelled.set()
            try:
                # The worker owns any locks it has appended to `held`.
                # Additional cancellation must not detach that thread before
                # it finishes and all of its leases can be released.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        # The worker has settled with an error; the original
                        # cancellation/entry failure remains the caller result.
                        break
                try:
                    task.result()
                except (Exception, asyncio.CancelledError) as exc:
                    logger.debug(
                        f"[multi-device] lease.worker_settled run_id={self.session_id} "
                        f"error_type={type(exc).__name__}"
                    )
            finally:
                self.release()
            raise
        return self

    async def __aexit__(self, *_):
        self.cancelled.set()
        self.release()
