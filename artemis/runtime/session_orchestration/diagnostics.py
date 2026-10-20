"""Immutable, bounded diagnostic pages with credential filtering."""

import copy
import re
from artemis.runtime.session_orchestration.models import new_id, now

SENSITIVE = re.compile(r"password|token|secret|api[_-]?key|credential", re.I)


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SENSITIVE.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        if value.startswith("data:image"):
            return "[image omitted; use an evidence artifact]"
        if len(value) > 8000:
            return value[:8000] + "[truncated]"
        return re.sub(
            r"(?i)(password|token|api[_-]?key|secret)(\s*[:=]\s*)(\S+)", r"\1\2[REDACTED]", value
        )
    return value


class Diagnostics:
    def __init__(self):
        self.snapshots = {}

    async def inspect(self, session, query):
        if set(query) - {"level", "offset", "limit", "snapshot_id"}:
            raise ValueError("Unsupported inspection query")
        limit = query.get("limit", 20)
        offset = query.get("offset", 0)
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Invalid page")
        snapshot_id = query.get("snapshot_id")
        if snapshot_id is None:
            snapshot_id = new_id()
            data = await session.inspect({"offset": 0, "limit": 100})
            transcript = data.get("transcript")
            if transcript:
                # Each call is synchronous up to its return in PersistentSession,
                # so the ledger is captured without yielding to the executor.
                # Bound capture and explicitly identify an omitted tail.
                items = transcript["messages"]
                while transcript.get("next_offset") is not None and len(items) < 2000:
                    page = await session.inspect(
                        {"offset": transcript["next_offset"], "limit": 100}
                    )
                    next_page = page["transcript"]
                    items.extend(next_page["messages"])
                    transcript["next_offset"] = next_page["next_offset"]
                transcript["capture_truncated"] = transcript.get("next_offset") is not None
                transcript["captured_messages"] = len(items)
            self.snapshots[snapshot_id] = {
                "snapshot_id": snapshot_id,
                "captured_at": now(),
                "consistency": "settled" if session.state == "READY" else "stream_checkpoint",
                "sections": redact(copy.deepcopy(data)),
            }
            # Diagnostic snapshots are a bounded window, not the session archive.
            while len(self.snapshots) > 20:
                self.snapshots.pop(next(iter(self.snapshots)))
        snapshot = copy.deepcopy(self.snapshots[snapshot_id])
        transcript = snapshot["sections"].get("transcript")
        if transcript:
            items = transcript["messages"]
            transcript["messages"] = items[offset : offset + limit]
            transcript["offset"] = offset
            transcript["next_offset"] = offset + limit if offset + limit < len(items) else None
            snapshot["next_cursor"] = (
                {"snapshot_id": snapshot_id, "offset": offset + limit}
                if offset + limit < len(items)
                else None
            )
        return snapshot
