"""Bounded JSONL frames and serialized writes over loopback streams."""

import asyncio
import json
from artemis.runtime.session_orchestration.models import Envelope

MAX_FRAME_BYTES = 1024 * 1024


class Connection:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.lock = asyncio.Lock()

    async def receive(self):
        try:
            raw = await self.reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise ValueError("Protocol frame too large") from exc
        if not raw:
            raise EOFError("Control connection closed")
        if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
            raise ValueError("Invalid protocol frame")
        return Envelope.model_validate_json(raw)

    async def send(self, message):
        data = message.model_dump_json().encode("utf-8") + b"\n"
        if len(data) > MAX_FRAME_BYTES:
            raise ValueError("Protocol frame too large")
        async with self.lock:
            self.writer.write(data)
            await asyncio.wait_for(self.writer.drain(), timeout=10)

    async def close(self):
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), timeout=5)
        except (ConnectionError, OSError, TimeoutError):
            pass
