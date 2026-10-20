"""Single-writer append journal with a detectable torn final record."""

from pathlib import Path
import json
import os
from artemis.runtime.session_orchestration.artifacts import canonical


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event):
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical(event) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read(self):
        if not self.path.exists():
            return []
        result = []
        for line in self.path.read_text(encoding="utf-8").splitlines(keepends=True):
            if not line.endswith("\n"):
                break
            result.append(json.loads(line))
        return result
