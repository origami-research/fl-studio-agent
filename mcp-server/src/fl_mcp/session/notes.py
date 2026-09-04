"""The note lanes (docs/30 Surface 3 concrete Lanes).

- **FileLane ("project-write")**: raw surgery via flpkit + an FL reload. The
  only lane that honors a pattern/channel target by construction - the
  pyscript/spool lane is TARGET-BLIND (a piano-roll script edits whichever
  score is open), so routing notes through it stamped target-claiming
  receipts on writes that all landed in one roll (dogfood, docs/21 A12).
- **BridgeLane ("bridge")**: live targeted notes via the native Bridge plugin
  over the transport. Eligible per call from evidence only - its ``can_serve``
  is the one-liner over ``core.router.bridge_provides``; its receipts are
  file readback, never the bridge's echo.

Routing itself is ``core.router.route`` via ``fl_mcp.ops.notes`` - no note
branch lives here. A lane failure RAISES; it never silently falls through - a
bridge timeout is ambiguous (the command may still apply on a later tick), so
retrying the same notes on the file lane inside the same call could
double-apply. The breaker records the failure and the NEXT call routes around
the bridge.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from fl_mcp import errors, flp
from fl_mcp.core.router import (
    FILE_PROVIDES,
    Capabilities,
    Operation,
    bridge_provides,
    satisfied,
)
from fl_mcp.errors import NotePlaneUnavailable, WriteRejected
from fl_mcp.fl.process import fl_process_running
from fl_mcp.schemas import Note, NoteDelta, NoteWriteMode, Target, WriteReceipt

if TYPE_CHECKING:
    from .facade import FLSession

log = logging.getLogger(__name__)

# ActionEntry.id for writes that never went through the spool.
NO_COMMAND_ID = 0

# The bridge applies on its next tick; its spool poll answers well within this.
BRIDGE_RESPONSE_TIMEOUT_SECONDS = 10.0

# The op kinds these lanes execute (fl_mcp/ops/notes.py declares them).
NOTE_KINDS = ("write_notes", "clear_notes")


def read_notes(session: FLSession, target: Target) -> list[Note]:
    """Store first (with provenance); project-file readback on demand."""
    stored = session.store.notes_at(target)
    if stored is not None:
        return stored
    if session.project is not None:
        return session.file_lane.read(target)
    raise NotePlaneUnavailable(
        f"Nothing recorded for pattern {target.pattern} channel {target.channel} and "
        "no project file is configured to read from."
    )


def _record(
    session: FLSession,
    target: Target,
    notes: list[Note],
    mode: NoteWriteMode,
    backend: str,
    command_id: int,
) -> NoteDelta:
    """Write-through to the store for an edit that ALREADY landed.

    The edit was applied and verified before this runs, so a recording
    failure must not fail the write - but it must not be silent either
    (found live: a store-path bug made every applied write report a
    traceback). Logged with the traceback; the receipt then carries an
    empty delta, meaning "applied, history unavailable"."""
    try:
        return session.store.record_write(target, notes, mode, backend, command_id)
    except Exception:
        log.exception("write-through recording failed after the %s backend applied", backend)
        return NoteDelta()


class FileLane:
    """Project-write: the file on disk is the source of truth; FL reloads to
    show it. write_notes verifies by re-reading the bytes it wrote."""

    name: Literal["project-write"] = "project-write"

    def __init__(self, session: FLSession) -> None:
        self._session = session

    def can_serve(self, op: Operation, caps: Capabilities) -> bool:
        return op.kind in NOTE_KINDS and satisfied(op.requires, FILE_PROVIDES)

    def apply(self, op: Operation, data: Sequence[Note]) -> WriteReceipt:
        # clear_notes IS a replace-with-empty write (ops/notes.py sends
        # data=[] and mode="replace"); one path serves both kinds.
        session = self._session
        target, notes, mode = op.target, list(data), op.mode
        what = op.kind.replace("_", " ")
        fix = (
            "The project file was restored by the readback check failing; "
            "inspect the .flp and retry."
        )
        try:
            session._prepare_file_write()
            written = flp.write_notes(
                session.project, notes, pattern=target.pattern, channel=target.channel, mode=mode
            )
        except flp.FlpError as exc:
            raise WriteRejected(f"project-file {what} did not verify: {exc}", fix=fix) from exc
        delta = _record(session, target, notes, mode, self.name, NO_COMMAND_ID)
        # The agent-echo suppressant (docs/27 §5): report the READBACK so the
        # observer's next diff never attributes this write to the user.
        session.observer.record_agent_write(target, written)
        session._reload_fl()
        return WriteReceipt(
            backend=self.name,
            target=target,
            confirmed_notes=len(written),
            delta=delta,
        )

    def read(self, target: Target) -> list[Note]:
        session = self._session
        ppq = flp.read(session.project).ppq
        notes = [
            Note(
                key=n.key,
                start=n.position / ppq,
                length=n.length / ppq,
                # FL stores 0..128; this SDK exposes 0..1, where 127 is full.
                velocity=min(n.velocity / 127, 1.0),
                pan=n.pan / 128,
            )
            for n in flp.notes_at(session.project, target.pattern, target.channel)
        ]
        session.store.mark_ground_truth(target, notes)
        return notes


class BridgeLane:
    """Live targeted notes via the native Bridge plugin, which consumes the
    same v2 spool as the pyscript but is not target-blind: as an effect it can
    set PatNum on FHD_AddNotesToPR (fp_plugclass.h; PROBE_VERDICT Q2)."""

    name: Literal["bridge"] = "bridge"

    def __init__(self, session: FLSession) -> None:
        self._session = session

    def can_serve(self, op: Operation, caps: Capabilities) -> bool:
        return op.kind in NOTE_KINDS and satisfied(op.requires, bridge_provides(op, caps))

    def apply(self, op: Operation, data: Sequence[Note]) -> WriteReceipt:
        target, notes, mode = op.target, list(data), op.mode
        if op.kind == "write_notes":
            # add_notes is the bridge wire's write vocabulary; clear_notes is
            # its own wire op (the consumer replaces with empty).
            command_id = self._send(
                "add_notes", target=target, notes=notes, mode=mode, ppq=self._ppq()
            )
        else:
            command_id = self._send("clear_notes", target=target)
        delta = _record(self._session, target, notes, mode, self.name, command_id)
        readback = self._file_lane_readback(target, command_id)
        self._session.observer.record_agent_write(target, readback)
        return WriteReceipt(
            backend=self.name,
            target=target,
            confirmed_notes=len(readback),
            delta=delta,
        )

    def read(self, target: Target) -> list[Note]:
        raise NotePlaneUnavailable(
            "The bridge has no live note readback (PROBE_VERDICT Q1); notes are "
            "read from the store or the project file."
        )

    def _send(self, op: str, **args: Any) -> int:
        """Spool one command and block for the bridge's response; returns the
        command id. A refusal or timeout raises - never a maybe - and the
        breaker learns the outcome either way (docs/27 §3)."""
        session = self._session
        cmd = session.transport.send(op, **args)
        try:
            resp = session.transport.await_response(cmd, timeout=BRIDGE_RESPONSE_TIMEOUT_SECONDS)
        except errors.ToolError:
            # An unacked command with FL gone is the hard-failure signature
            # (the command may have been the trigger); a live FL that missed
            # the window is soft - re-ping before the next bridge use.
            if fl_process_running():
                session.breaker.record_soft_failure()
            else:
                session.breaker.record_hard_failure(f"{op} command {cmd.id} unacked; FL not running")
            raise
        if not resp.ok:
            session.breaker.record_soft_failure()
            raise WriteRejected(
                f"the bridge refused spool command {cmd.id} ({op}): {resp.err}",
                fix="Read bridge_status.txt / bridge_heartbeat.txt under the spool and "
                "retry; the router falls back to project-write while the bridge is "
                "unhealthy.",
            )
        session.breaker.record_success()
        return cmd.id

    def _ppq(self) -> int | None:
        """The PPQ stamp for spooled note commands: the target universe's ppq
        (the project file), else the live hello's. None lets the consumer fall
        back (the bridge assumes 96 and flags it in its response summary)."""
        session = self._session
        try:
            return flp.read(session.project).ppq
        except flp.FlpError:
            log.exception("could not read ppq from %s; trying the live hello", session.project)
        hello = session._ensure_hello()
        return hello.ppq if hello is not None else None

    def _file_lane_readback(self, target: Target, command_id: int) -> list[Note]:
        """Readback notes for a bridge receipt - never the bridge's echo
        (FHD_AddNotesToPR returns nothing, so its notes_written is what it
        SENT). The only read of record today is the project file, which LAGS
        the live session until FL saves - so this may exclude the very
        write it confirms. That gap is deliberate until a live note readback
        exists (the observer's poll-and-diff read, R2)."""
        try:
            return flp.notes_at(self._session.project, target.pattern, target.channel)
        except flp.FlpError as exc:
            raise WriteRejected(
                f"the bridge applied command {command_id} but the file readback "
                f"failed: {exc}; the write is UNCONFIRMED.",
                fix="Inspect the project file and the bridge status files, then retry.",
            ) from exc
