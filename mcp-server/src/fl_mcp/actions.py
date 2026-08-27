"""The action log — actions.jsonl (docs/11 "History: undo and the action log").

Append-only, oldest first, one ``schemas.ActionEntry`` per line. Read the file
bottom-up to answer "what actions built this?"; together with the starting
snapshot, replaying the log rebuilds the whole project. Every successful
write op appends here (write-through, same as the shadow score).
"""

from __future__ import annotations

import threading
from pathlib import Path

from . import paths, schemas


class ActionLog:
    def __init__(self, project_key: str, root: Path | None = None) -> None:
        self.project_key = project_key
        self.root = root if root is not None else paths.project_dir(project_key)
        self.path = self.root / "actions.jsonl"
        self._lock = threading.Lock()

    def append(self, entry: schemas.ActionEntry) -> None:
        # An MCP server dispatches tool calls concurrently; without a lock,
        # two threads' write() calls could interleave mid-line and corrupt
        # the file (each entry is two separate write() calls: json + "\n").
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(entry.model_dump_json())
                f.write("\n")

    def read_all(self) -> list[schemas.ActionEntry]:
        """Oldest first, as stored."""
        if not self.path.is_file():
            return []
        entries = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(schemas.ActionEntry.model_validate_json(line))
        return entries

    def read_reverse(self) -> list[schemas.ActionEntry]:
        """Newest first — how the doc says to read it: "what actions built this?"."""
        return list(reversed(self.read_all()))

    def last(self) -> schemas.ActionEntry | None:
        entries = self.read_reverse()
        return entries[0] if entries else None
