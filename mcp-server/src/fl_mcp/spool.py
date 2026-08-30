"""File mailbox between the server and the code running inside FL.

LANE S implements this module (docs/21). ONE protocol - the sequenced
command/response directories; v1's single-pending-file generation dies at
cutover, and the piano-roll pyscript is updated in lockstep (that update is
part of Lane S's deliverable, preserving the fail-closed applied-ids ledger).

UGLY(U2), quarantined: FL's embedded-Python sandbox. ``open()`` works only
inside FL's data tree and only 'Piano roll scripts' accepts writes;
``os.remove`` raises SystemError - so consumption is tombstones + a ledger,
never deletion. Every rule here was measured, not guessed (docs/10).

Wire layout, per spool root (the server is the only writer of ``commands/``,
the in-FL consumer the only writer of ``responses/`` and the ledger):

    <root>/commands/000001.json   SpoolCommand, atomically written
    <root>/commands/queue.json    {"proto": 2, "ids": [...]} - pending ids;
                                  the consumer iterates THIS, never listdir
                                  (directory scanning is unmeasured in FL's
                                  sandbox; open-read and exists are measured)
    <root>/responses/000001.json  SpoolResponse from the consumer
    <root>/consumed.json          the consumer's fail-closed applied-ids ledger

Commands are mirrored to every candidate root because FL's read grants are
wider than its write grants and differ per folder (measured, flpaths) - the
consumer reads from whichever root it can and records consumption in
whichever root accepts a write.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, cast

from fl_mcp import errors
from fl_mcp.flpaths import SPOOL_SUBDIRS, fl_settings_dirs

from .schemas import OP_ARGS, SCHEMA_VERSION, SpoolCommand, SpoolOp, SpoolResponse

log = logging.getLogger(__name__)

QUEUE_FILE = "queue.json"
LEDGER_FILE = "consumed.json"
_RESPONSE_POLL_INTERVAL = 0.05


class SpoolUnavailable(RuntimeError):
    """FL Studio's settings folder could not be found."""


class Spool:
    """A session-scoped mailbox under FL's settings tree."""

    def __init__(self, root: Path | None = None) -> None:
        """``root=None`` discovers the measured writable location; an explicit
        root is the test seam (tmp_path)."""
        self._roots = [root] if root is not None else spool_roots()
        # The lock serializes id allocation AND the queue.json read-modify-write:
        # MCP dispatches tool calls on concurrent threads, and two sends sharing
        # an id (or losing a queue update) correlates the wrong FL response to
        # the wrong command.
        self._lock = threading.Lock()
        for r in self._roots:
            try:
                (r / "commands").mkdir(parents=True, exist_ok=True)
                (r / "responses").mkdir(parents=True, exist_ok=True)
            except OSError:
                # A root that refuses mkdir may still be readable by FL; keep it
                # for reads, and let send() report if no root accepts writes.
                log.exception("could not create spool directories under %s", r)
        self._next_id = resume_next_id(self._roots)

    def send(self, op: str, **args: Any) -> SpoolCommand:
        """Schema-validated (schemas.OP_ARGS), sequence-numbered (lock-guarded
        - concurrent tool calls must never share an id), atomically written."""
        if not self._roots:
            raise SpoolUnavailable(
                "Could not find FL Studio's settings folder "
                "(~/Documents/Image-Line/FL Studio/Settings). Is FL Studio installed?"
            )
        if op not in OP_ARGS:
            raise ValueError(f"unknown spool op {op!r}; expected one of {sorted(OP_ARGS)}")
        # The membership check above is what makes this cast honest.
        op_checked = cast(SpoolOp, op)
        # Resolve unions at the boundary: the consumer sees the validated,
        # normalized form ("C4" is already 60), never the caller's raw input.
        validated = OP_ARGS[op](**args).model_dump(mode="json")

        with self._lock:
            cmd = SpoolCommand(id=self._next_id, op=op_checked, args=validated)
            self._next_id += 1
            written: list[Path] = []
            failures: list[str] = []
            payload = cmd.model_dump(mode="json")
            for root in self._roots:
                try:
                    write_json_atomic(root / "commands" / f"{cmd.id:06d}.json", payload)
                    self._update_queue(root, add=cmd.id)
                    written.append(root)
                except OSError as exc:
                    failures.append(f"{root}: {exc}")
        if not written:
            raise SpoolUnavailable(
                "Could not write to any spool location:\n  " + "\n  ".join(failures)
            )
        return cmd

    def await_response(self, command: SpoolCommand, timeout: float) -> SpoolResponse:
        """Block for the in-FL consumer's confirmation; raises ToolError
        (note_plane_unavailable) on timeout - never claims unverified success.

        On timeout the command is first CANCELLED (tombstoned in every root and
        dropped from the queue), so a late manual trigger can never apply a
        write this call already reported as failed."""
        deadline = time.monotonic() + timeout
        while True:
            response = self._read_response(command.id)
            if response is not None:
                self._forget(command.id, tombstone=False)
                return response
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_RESPONSE_POLL_INTERVAL, remaining))
        self._forget(command.id, tombstone=True)
        raise errors.ToolError(
            "note_plane_unavailable",
            f"No response to spool command {command.id} within {timeout:g}s; "
            "the command was cancelled and will not apply later.",
            "In FL Studio: open the piano roll and run Tools > Scripting > "
            "'MCP Write Notes', then retry. Run fl_doctor if it keeps failing.",
            retryable=True,
        )

    # -- wire-format jobs -------------------------------------------------------

    def _read_response(self, command_id: int) -> SpoolResponse | None:
        for root in self._roots:
            payload = read_json(root / "responses" / f"{command_id:06d}.json")
            if payload is not None:
                return SpoolResponse.model_validate(payload)
        return None

    def _forget(self, command_id: int, *, tombstone: bool) -> None:
        """Drop a settled command from every queue; tombstone a cancelled one so
        the consumer treats it exactly like an applied-and-consumed command."""
        with self._lock:
            for root in self._roots:
                try:
                    if tombstone:
                        write_json_atomic(
                            root / "commands" / f"{command_id:06d}.json",
                            {"consumed": True, "id": command_id},
                        )
                    self._update_queue(root, remove=command_id)
                except OSError:
                    # Best-effort: the consumer's ledger still holds the line
                    # against replays; a stale queue entry only costs a skip.
                    log.exception("could not settle command %d under %s", command_id, root)

    def _update_queue(self, root: Path, *, add: int | None = None, remove: int | None = None) -> None:
        """Read-modify-write of one root's pending-ids index (caller holds the lock)."""
        path = root / "commands" / QUEUE_FILE
        payload = read_json(path)
        ids = [i for i in (payload or {}).get("ids", []) if isinstance(i, int)]
        if add is not None and add not in ids:
            ids.append(add)
        if remove is not None and remove in ids:
            ids.remove(remove)
        write_json_atomic(path, {"proto": SCHEMA_VERSION, "ids": sorted(ids)})


# -- helpers --------------------------------------------------------------------


def resume_next_id(roots: list[Path]) -> int:
    """Never reuse an id: the consumer's ledger is keyed by id, and a reused
    id would be refused as already applied (or worse, correlated wrongly).
    Resume past everything any root has seen - files, queue, and ledger.

    THE id allocator resume for every carrier (this file spool and the
    Bridge's UDP client, bridge_wire) - they share one consumer ledger, so
    they must share one id space."""
    seen = 0
    for root in roots:
        for d in (root / "commands", root / "responses"):
            if d.is_dir():
                seen = max(seen, *(int(p.stem) for p in d.glob("*.json") if p.stem.isdigit()), 0)
        for name in (Path("commands") / QUEUE_FILE, Path(LEDGER_FILE)):
            payload = read_json(root / name)
            if payload is not None:
                ids = [i for i in payload.get("ids", []) if isinstance(i, int)]
                seen = max(seen, *ids, 0)
    return seen + 1


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """tmp + rename so a reader never sees partial JSON; the temp file is
    cleaned up even on KeyboardInterrupt (the one legitimate
    ``except BaseException: ... raise``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    """None for a missing file; CORRUPTION IS LOGGED, never silently read as
    'empty' - a torn ledger read as empty replays applied commands."""
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        log.exception("unreadable spool file %s; refusing to treat it as empty", path)
        return None


def spool_roots() -> list[Path]:
    """The measured writable locations under FL's settings tree, best first
    (Settings/Piano roll scripts/FLMCP, then Settings/Piano roll scripts)."""
    return [settings / sub for settings in fl_settings_dirs() for sub in SPOOL_SUBDIRS]
