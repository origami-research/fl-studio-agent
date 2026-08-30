"""The note lanes and their routing.

- **FileLane ("project-write")**: raw surgery via flpkit + an FL reload. The
  only lane that honors a pattern/channel target by construction - the
  pyscript/spool lane is TARGET-BLIND (a piano-roll script edits whichever
  score is open), so routing notes through it stamped target-claiming
  receipts on writes that all landed in one roll (dogfood, docs/21 A12).
- **BridgeLane ("bridge")**: live targeted notes via the native Bridge plugin
  over the same spool. Eligible per call from evidence only (router.detect +
  pick_note_lane); its receipts are file readback, never the bridge's echo.

A lane failure RAISES; it never silently falls through - a bridge timeout is
ambiguous (the command may still apply on a later tick), so retrying the same
notes on the file lane inside the same call could double-apply. The breaker
records the failure and the NEXT call routes around the bridge.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from fl_mcp import errors, flp
from fl_mcp.errors import NotePlaneUnavailable, WriteRejected
from fl_mcp.flpaths import fl_process_running
from fl_mcp.schemas import Note, NoteDelta, NoteWriteMode, Target, WriteReceipt

from .router import NoteLane, detect, pick_note_lane

if TYPE_CHECKING:
    from .facade import FLSession

log = logging.getLogger(__name__)

# ActionEntry.id for writes that never went through the spool.
NO_COMMAND_ID = 0

# The bridge applies on its next tick; its spool poll answers well within this.
BRIDGE_RESPONSE_TIMEOUT_SECONDS = 10.0


def write_notes(
    session: FLSession, target: Target, notes: list[Note], mode: NoteWriteMode
) -> WriteReceipt:
    """Apply via the lane the evidence picks, verify by readback, record the
    delta. Raises (NotePlaneUnavailable / WriteRejected) on failure - never
    returns a maybe."""
    if session.project is None:
        raise NotePlaneUnavailable(
            "No backend can apply targeted notes: no project file is configured. "
            "(The pyscript lane cannot target a channel - see write_notes.)"
        )
    return _route(session, target).write(target, notes, mode)


def clear_notes(session: FLSession, target: Target) -> WriteReceipt:
    """Same routing as write_notes."""
    if session.project is None:
        raise NotePlaneUnavailable(
            "No backend can clear notes: the control plane is not connected and no "
            "project file is configured."
        )
    return _route(session, target).clear(target)


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


def _route(session: FLSession, target: Target) -> NoteLane:
    caps = detect(session.link, session.spool, session.project, session.profile)
    return pick_note_lane(
        caps, target, session.breaker, bridge=session.bridge_lane, file=session.file_lane
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

    def write(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteReceipt:
        return self._apply(
            target,
            notes,
            mode,
            what="note write",
            fix="The project file was restored by the readback check failing; "
            "inspect the .flp and retry.",
        )

    def clear(self, target: Target) -> WriteReceipt:
        return self._apply(target, [], "replace", what="clear", fix="Inspect the .flp and retry.")

    def _apply(
        self,
        target: Target,
        notes: list[Note],
        mode: NoteWriteMode,
        *,
        what: str,
        fix: str,
    ) -> WriteReceipt:
        session = self._session
        try:
            written = flp.write_notes(
                session.project, notes, pattern=target.pattern, channel=target.channel, mode=mode
            )
        except flp.FlpError as exc:
            raise WriteRejected(f"project-file {what} did not verify: {exc}", fix=fix) from exc
        delta = _record(session, target, notes, mode, self.name, NO_COMMAND_ID)
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
                velocity=n.velocity / 127,
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

    def write(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteReceipt:
        command_id = self._send("add_notes", target=target, notes=notes, mode=mode, ppq=self._ppq())
        delta = _record(self._session, target, notes, mode, self.name, command_id)
        return WriteReceipt(
            backend=self.name,
            target=target,
            confirmed_notes=self._file_lane_note_count(target, command_id),
            delta=delta,
        )

    def clear(self, target: Target) -> WriteReceipt:
        command_id = self._send("clear_notes", target=target)
        delta = _record(self._session, target, [], "replace", self.name, command_id)
        return WriteReceipt(
            backend=self.name,
            target=target,
            confirmed_notes=self._file_lane_note_count(target, command_id),
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
        cmd = session.spool.send(op, **args)
        try:
            resp = session.spool.await_response(cmd, timeout=BRIDGE_RESPONSE_TIMEOUT_SECONDS)
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

    def _file_lane_note_count(self, target: Target, command_id: int) -> int:
        """Readback-only count for a bridge receipt - never the bridge's echo
        (FHD_AddNotesToPR returns nothing, so its notes_written is what it
        SENT). The only read of record today is the project file, which LAGS
        the live session until FL saves - so this count may exclude the very
        write it confirms. That gap is deliberate until a live note readback
        exists (the observer's poll-and-diff read, R2)."""
        try:
            return len(flp.notes_at(self._session.project, target.pattern, target.channel))
        except flp.FlpError as exc:
            raise WriteRejected(
                f"the bridge applied command {command_id} but the file readback "
                f"failed: {exc}; the write is UNCONFIRMED.",
                fix="Inspect the project file and the bridge status files, then retry.",
            ) from exc
