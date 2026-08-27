"""Spool protocol — the note plane's transport.

The MCP server writes a command file; an executor inside FL Studio picks it up,
performs the edit, and writes a result file back. Two generations of executor
live in this module side by side:

- **v0** (functions below, unchanged): a piano roll ``.pyscript`` the user
  triggers from FL's Tools > Scripting menu, so exactly one command is ever
  pending and a single ``pending.json`` is enough. It lives inside FL Studio's
  own Settings tree because FL's embedded Python cannot read files outside it
  (see flpaths). We write each command to every candidate root and read from
  whichever the script could reach, so a single unreadable location can't
  stall the pipeline. This is the live, interim note plane (docs/14/15 fix
  item 4): fl_mcp.server's write_notes/clear_notes route here whenever no
  explicit pattern/channel target is given.
- **v1** (``SpoolSession``, below): the sequenced ``commands/``/``responses/``
  queue described in docs/03 Protocol B, meant for the native Bridge plugin
  (docs/04) once it loads cleanly. No plugin is wired into a live server yet
  (docs/12: gated on a probe that hasn't passed), so this path is honest about
  that — an explicitly-targeted write blocks for a real confirmation and
  raises a structured ``note_plane_unavailable`` error rather than claiming
  success nothing has actually verified. Kept for the kernel's per-channel
  ``Track.write``/``flush`` surface (docs/11); not a new trigger, and not
  built out further by this merge.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import errors, paths, schemas
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


# ---------------------------------------------------------------------------
# v1: session spool (docs/03 Protocol B) — the native Bridge plugin's queue.
# ---------------------------------------------------------------------------

_RESPONSE_POLL_INTERVAL = 0.05
_STALE_SESSION_AGE = 60 * 60  # 1 hour — matches docs/02's GC-on-start failure mode


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_session_id() -> str:
    return f"{os.getpid()}-{_now().strftime('%Y%m%dT%H%M%S%f')}"


class _ResponseArrived(FileSystemEventHandler):
    """Wakes the waiting thread whenever the responses dir changes at all.

    Filtering to the exact filename happens in the waiter's own poll, not
    here — this only needs to short-circuit the poll interval, so any event
    is worth a wakeup.
    """

    def __init__(self, wake: threading.Event) -> None:
        self._wake = wake

    def on_created(self, event: Any) -> None:  # noqa: D102
        self._wake.set()

    def on_moved(self, event: Any) -> None:  # noqa: D102
        self._wake.set()

    def on_modified(self, event: Any) -> None:  # noqa: D102
        self._wake.set()


class SpoolSession:
    """One server session's command/response queue for the Bridge plugin.

    Directory layout, per docs/03:

        <root>/<session-id>/
        +-- commands/000001.json      server writes, plugin consumes
        +-- commands/done/            plugin moves processed commands here
        +-- responses/000001.json     plugin writes, server consumes
        +-- session.json              {proto_ver, server_pid, started_at, heartbeat_at}
    """

    def __init__(self, root: Path | None = None, session_id: str | None = None) -> None:
        self.root = root if root is not None else paths.spool_dir()
        self.session_id = session_id or _new_session_id()
        self.dir = self.root / self.session_id
        self.commands_dir = self.dir / "commands"
        self.responses_dir = self.dir / "responses"
        self.done_dir = self.commands_dir / "done"
        for d in (self.commands_dir, self.responses_dir, self.done_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._next_id = self._resume_next_id()
        self._lock = threading.Lock()
        self._write_session_file(started=True)

    def _resume_next_id(self) -> int:
        """Continue a prior counter rather than reusing ids after a restart."""
        seqs = [
            int(p.stem)
            for d in (self.commands_dir, self.done_dir, self.responses_dir)
            for p in d.glob("*.json")
            if p.stem.isdigit()
        ]
        return max(seqs, default=0) + 1

    def _write_session_file(self, *, started: bool = False) -> None:
        existing = read_json(self.dir / "session.json") if not started else None
        started_at = (existing or {}).get("started_at") or _now().isoformat()
        write_json_atomic(
            self.dir / "session.json",
            {
                "proto_ver": PROTO_VER,
                "server_pid": os.getpid(),
                "started_at": started_at,
                "heartbeat_at": _now().isoformat(),
            },
        )

    def heartbeat(self) -> None:
        self._write_session_file()

    def send_command(self, op: str, **args: Any) -> schemas.SpoolCommand:
        """Validate and queue a command; returns immediately (does not wait)."""
        args_model = schemas.OP_ARGS.get(op)
        if args_model is not None:
            args_model(**args)  # raises pydantic.ValidationError on bad shape
        with self._lock:
            cmd_id = self._next_id
            self._next_id += 1
        cmd = schemas.SpoolCommand(id=cmd_id, op=op, args=args)
        path = self.commands_dir / f"{cmd_id:06d}.json"
        write_json_atomic(path, json.loads(cmd.model_dump_json()))
        return cmd

    def _read_response(self, command_id: int) -> schemas.SpoolResponse | None:
        payload = read_json(self.responses_dir / f"{command_id:06d}.json")
        return schemas.SpoolResponse.model_validate(payload) if payload is not None else None

    def wait_for_response(self, command_id: int, timeout: float = 2.0) -> schemas.SpoolResponse:
        """Block for the plugin's response, watching the responses dir.

        Raises a note_plane_unavailable ToolError (docs/06) if nothing claims
        the command within ``timeout`` seconds — the plugin isn't loaded, or
        isn't polling, and the model needs to know which fix to suggest.
        """
        existing = self._read_response(command_id)
        if existing is not None:
            return existing

        wake = threading.Event()
        observer = Observer()
        observer.schedule(_ResponseArrived(wake), str(self.responses_dir), recursive=False)
        observer.start()
        try:
            deadline = time.monotonic() + timeout
            while True:
                response = self._read_response(command_id)
                if response is not None:
                    return response
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wake.wait(timeout=min(_RESPONSE_POLL_INTERVAL, remaining))
                wake.clear()
        finally:
            observer.stop()
            observer.join(timeout=1.0)
        raise errors.note_plane_unavailable(command_id, timeout)

    def close(self) -> None:
        """Best-effort cleanup; commands/done/ is left for debugging (server GC's it)."""
        try:
            (self.dir / "session.json").unlink(missing_ok=True)
        except OSError:
            pass


def gc_stale_sessions(root: Path | None = None, max_age: float = _STALE_SESSION_AGE) -> list[str]:
    """Remove session dirs whose heartbeat is stale (docs/02: dead-session GC on start).

    Returns the ids of the sessions removed.
    """
    import shutil

    base = root if root is not None else paths.spool_dir()
    if not base.is_dir():
        return []

    removed = []
    now = _now()
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        session = read_json(entry / "session.json")
        heartbeat_at = (session or {}).get("heartbeat_at")
        stale = True
        if heartbeat_at:
            try:
                age = (now - datetime.fromisoformat(heartbeat_at)).total_seconds()
                stale = age > max_age
            except ValueError:
                stale = True
        if stale:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed
