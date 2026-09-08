"""The note lanes (docs/30 Surface 3 concrete Lanes).

- **FileLane ("project-write")**: raw surgery via flpkit + an FL reload. The
  only lane that honors a pattern/channel target by construction - the
  pyscript/spool lane is TARGET-BLIND (a piano-roll script edits whichever
  score is open), so routing notes through it stamped target-claiming
  receipts on writes that all landed in one roll (dogfood, docs/21 A12).
- **BridgeLane ("bridge")**: live targeted notes via the native Bridge plugin
  over the transport. Eligible per call from evidence only - its ``can_serve``
  is the one-liner over ``core.router.bridge_provides``; when a readable project
  exists it saves FL's live state before parsing its receipt, never trusting a
  lagging file or echo.

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
from fl_mcp.fl.process import bridge_pid, fl_process_running, pid_alive
from fl_mcp.schemas import Note, NoteDelta, NoteWriteMode, Target, WriteReceipt

from . import controls
from .detect import (
    BRIDGE_HEARTBEAT_FILE,
    BRIDGE_HEARTBEAT_FRESH_SECONDS,
    _selected_channel,
    read_heartbeat,
)
from .link import LIVE_SET_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from .facade import FLSession

log = logging.getLogger(__name__)

# ActionEntry.id for writes that never went through the spool.
NO_COMMAND_ID = 0

# The bridge applies on its next tick; its spool poll answers well within this.
BRIDGE_RESPONSE_TIMEOUT_SECONDS = 10.0

# The op kinds these lanes execute (fl_mcp/ops/notes.py declares them).
NOTE_KINDS = ("write_notes", "clear_notes")


def _refuse_if_session_bridge_stalled(session: FLSession) -> None:
    """A session note write reached the file lane: say WHY the bridge was skipped.

    In session mode the file lane is refused anyway (``file_lane_requires_reload``),
    but when the reason the router skipped the bridge is a loaded bridge whose
    GUI-thread tick has stopped (a locked Mac, a modal dialog), the producer must
    hear that diagnosis, not "requires reopening the project file".
    """
    root = session.session_spool_root
    if root is None:
        return
    heartbeat = read_heartbeat(root / BRIDGE_HEARTBEAT_FILE)
    if heartbeat.at is None or heartbeat.age <= BRIDGE_HEARTBEAT_FRESH_SECONDS:
        return
    if not pid_alive(bridge_pid(root)):
        return  # FL itself is gone; the liveness path owns that story
    age = int(heartbeat.age)
    raise errors.ToolError(
        "note_plane_unavailable",
        f"The FL MCP Bridge is loaded but has not ticked for {age} s: FL's GUI thread "
        "is not pumping, so this session's notes cannot be written right now.",
        "On macOS this happens while the screen is locked; unlock the Mac, or bring FL "
        "out of a modal dialog, then retry. Run fl_doctor to confirm the note plane.",
        retryable=True,
    )


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
        fix = "The project file was restored by the readback check failing; inspect the .flp and retry."
        _refuse_if_session_bridge_stalled(session)
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
        notes = _project_notes(session, target)
        session.store.mark_ground_truth(target, notes)
        return notes


class BridgeLane:
    """Live targeted notes via the native Bridge plugin, which consumes the
    same v2 spool as the pyscript but is not target-blind: as an effect it can
    set PatNum on FHD_AddNotesToPR (licensed SDK declaration; PROBE_VERDICT Q2)."""

    name: Literal["bridge"] = "bridge"

    def __init__(self, session: FLSession) -> None:
        self._session = session
        self._warned_missing_file_readback = False

    def can_serve(self, op: Operation, caps: Capabilities) -> bool:
        return op.kind in NOTE_KINDS and satisfied(op.requires, bridge_provides(op, caps))

    def apply(self, op: Operation, data: Sequence[Note]) -> WriteReceipt:
        target, notes, mode = op.target, list(data), op.mode
        self._select_target_channel(target.channel)
        if op.kind == "write_notes":
            # add_notes is the bridge wire's write vocabulary; clear_notes is
            # its own wire op (the consumer replaces with empty).
            command_id = self._send("add_notes", target=target, notes=notes, mode=mode, ppq=self._ppq())
        else:
            command_id = self._send("clear_notes", target=target)
        readback = self._live_readback(target, command_id)
        if readback is None:
            return WriteReceipt(
                backend=self.name,
                target=target,
                confirmed_notes=None,
                unconfirmed_write=True,
                delta=NoteDelta(),
            )
        delta = _record(self._session, target, notes, mode, self.name, command_id)
        # The persisted post-write state is authoritative over the shadow
        # update above, whose purpose is the durable action log and delta.
        self._session.store.mark_ground_truth(target, readback)
        self._session.observer.record_agent_write(target, readback)
        return WriteReceipt(
            backend=self.name,
            target=target,
            confirmed_notes=len(readback),
            delta=delta,
        )

    def _select_target_channel(self, target_channel: int | None) -> None:
        """Make the Bridge's selected-channel side effect match the target.

        The router admitted this lane because the profile proves this exact
        select-then-verify sequence. The readback is mandatory: a Bridge
        command is never sent unless FL reports the requested channel.
        Selection intentionally remains changed so consecutive channel writes
        do not thrash FL's visible selection.
        """
        if target_channel is None:
            return
        session = self._session
        if session.caps.bridge_channel == target_channel:
            return
        response = session.link.request(
            "select_channels", args=[[target_channel]], timeout=LIVE_SET_TIMEOUT_SECONDS
        )
        if not response.ok:
            detail = response.err or "no detail"
            if errors.is_operation_unsafe(detail):
                raise errors.operation_unsafe("select_channels")
            raise errors.ToolError(
                "fl_api_error",
                f"FL Studio refused select_channels for channel {target_channel}: {detail}",
                "Retry after FL finishes its current operation. No notes were sent to the Bridge.",
                retryable=True,
            )
        selected = _selected_channel(session.link)
        if selected == target_channel:
            return
        actual = "an unknown channel" if selected is None else f"channel {selected}"
        raise errors.ToolError(
            "fl_api_error",
            f"FL Studio is on {actual} after selecting channel {target_channel}; no notes were sent.",
            "Retry after FL settles. The requested notes were not sent to the Bridge.",
            retryable=True,
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
        with session._transport_use():
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
        if session.project is not None and session.project.is_file():
            try:
                return flp.read(session.project).ppq
            except flp.FlpError:
                log.exception("could not read ppq from %s; trying the live hello", session.project)
        hello = session._ensure_hello()
        return hello.ppq if hello is not None else None

    def _live_readback(self, target: Target, command_id: int) -> list[Note] | None:
        """Persist FL's live state, then parse that post-save state.

        The Bridge response only proves that it dispatched its write; its
        ``notes_written`` is an echo.  Saving through FL closes the live-to-
        file boundary without a reload, so the parsed file is a readback of
        the live session rather than the stale pre-write project.  A failure
        here is a missing measurement after an applied command, not a failed
        write, and must therefore produce an honest unconfirmed receipt.
        """
        project = self._session.project
        if project is None or not project.is_file():
            if not self._warned_missing_file_readback:
                self._warned_missing_file_readback = True
                log.warning(
                    "bridge commands are applied but receipts are unconfirmed: no readable project file is available for note readback"
                )
            return None
        try:
            controls.save_project(self._session)
            return _project_notes(self._session, target)
        except Exception:
            log.exception("bridge command %d applied but live note readback is unavailable", command_id)
            return None


def _project_notes(session: FLSession, target: Target) -> list[Note]:
    """Decode one target from a project that is known current by its caller."""
    ppq = flp.read(session.project).ppq
    return [
        Note(
            key=n.key,
            start=n.position / ppq,
            length=n.length / ppq,
            velocity=min(n.velocity / 127, 1.0),
            pan=n.pan / 128,
        )
        for n in flp.notes_at(session.project, target.pattern, target.channel)
    ]
