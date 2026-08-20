"""Spool protocol — the note plane's transport.

The MCP server writes a command file; an executor inside FL Studio picks it up,
performs the edit, and writes a result file back.

In v0 the executor is a piano roll ``.pyscript`` the user triggers from FL's
Tools > Scripting menu, so exactly one command is ever pending and a single
``pending.json`` is enough. When the native Bridge plugin replaces the pyscript
(see docs/04), this module grows into the sequenced ``commands/``/``responses/``
queue described in docs/03 — the *command payload* below is already the shape
that queue will carry, so only the file layout changes.

The spool lives inside FL Studio's own Settings tree because FL's embedded
Python cannot read files outside it (see flpaths). We write each command to
every candidate root and read from whichever the script could reach, so a
single unreadable location can't stall the pipeline.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .flpaths import SPOOL_SUBDIRS, fl_settings_dirs

PROTO_VER = 1


def spool_roots() -> list[Path]:
    """Every location the in-FL script might read from, preferred first."""
    return [settings / sub for settings in fl_settings_dirs() for sub in SPOOL_SUBDIRS]


def spool_root() -> Path | None:
    """The preferred spool location, or None if FL Studio isn't installed."""
    roots = spool_roots()
    return roots[0] if roots else None


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write via temp file + rename so a reader never sees partial JSON.

    ``os.replace`` is atomic on both macOS and Windows when source and
    destination share a filesystem, which they do here (same directory).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        return None


def _newest(name: str) -> dict[str, Any] | None:
    """Read `name` from whichever spool root has the most recent copy."""
    best: tuple[float, dict[str, Any]] | None = None
    for root in spool_roots():
        path = root / name
        payload = read_json(path)
        if payload is None:
            continue
        mtime = path.stat().st_mtime
        if best is None or mtime > best[0]:
            best = (mtime, payload)
    return best[1] if best is not None else None


def next_command_id() -> int:
    """Monotonic command id, used to correlate a result with its command."""
    root = spool_root()
    if root is None:
        return 1
    counter = root / "counter"
    try:
        current = int(counter.read_text())
    except (FileNotFoundError, ValueError, OSError):
        current = 0
    nxt = current + 1
    try:
        counter.parent.mkdir(parents=True, exist_ok=True)
        counter.write_text(str(nxt))
    except OSError:
        pass
    return nxt


class SpoolUnavailable(RuntimeError):
    """FL Studio's settings folder could not be found."""


def put_command(op: str, **fields: Any) -> dict[str, Any]:
    """Queue a command for the in-FL executor, in every candidate spool root."""
    roots = spool_roots()
    if not roots:
        raise SpoolUnavailable(
            "Could not find FL Studio's settings folder "
            "(~/Documents/Image-Line/FL Studio/Settings). Is FL Studio installed?"
        )

    cmd = {"proto": PROTO_VER, "id": next_command_id(), "op": op, **fields}
    written = []
    errors = []
    for root in roots:
        try:
            write_json_atomic(root / "pending.json", cmd)
            written.append(root)
        except OSError as exc:
            errors.append(f"{root}: {exc}")
    if not written:
        raise SpoolUnavailable("Could not write to any spool location:\n  " + "\n  ".join(errors))
    return cmd


def clear_stale_results() -> None:
    """Drop old result files so a new command's result can't be confused with them."""
    for root in spool_roots():
        try:
            (root / "result.json").unlink(missing_ok=True)
        except OSError:
            pass


def read_result() -> dict[str, Any] | None:
    return _newest("result.json")


def consumed_ids() -> list[int]:
    """Command ids the in-FL script reports as already applied."""
    ids: list[int] = []
    for root in spool_roots():
        payload = read_json(root / "consumed.json")
        if not payload:
            continue
        for value in payload.get("ids", []):
            if value not in ids:
                ids.append(value)
    return ids


def pending_command() -> dict[str, Any] | None:
    """The command awaiting execution, or None if it has been applied.

    FL blocks os.remove, so the script cannot delete a consumed command. It
    overwrites it with a tombstone where it can, and always records the id in a
    ledger — the ledger is authoritative, because the folder holding the command
    may be readable but not writable.
    """
    payload = _newest("pending.json")
    if payload is None or payload.get("consumed") or not payload.get("op"):
        return None
    if payload.get("id") in consumed_ids():
        return None
    return payload
