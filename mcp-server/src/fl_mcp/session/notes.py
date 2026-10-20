"""The note lanes (docs/30 Surface 3 concrete Lanes) and the session's note state.

- **FileLane ("project-write")**: raw surgery via flpkit + an FL reload. The
  only lane that honors a pattern/channel target by construction - the
  pyscript/spool lane is TARGET-BLIND (a piano-roll script edits whichever
  score is open), so routing notes through it stamped target-claiming
  receipts on writes that all landed in one roll (dogfood, docs/21 A12).
- **BridgeLane ("bridge")**: live targeted notes via the native Bridge plugin
  over the transport. Eligible per call from evidence only - its ``can_serve``
  is the one-liner over ``core.router.bridge_provides``.

Note state (docs/27 "Note state"): FL has no live note getter, so a lane
returns the target's notes as FL now has them - the file lane re-reads the
bytes it wrote; the bridge lane takes FL's acknowledgement of the command as
the confirmation and, when FL can save, re-reads the saved project instead.
That state is recorded in the store, and ``read_notes``/``all_notes`` overlay
it on the project FL loaded, so a read always returns what FL has now.

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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from fl_mcp import errors, flp
from fl_mcp.contract import FLLicense
from fl_mcp.core.router import (
    FILE_PROVIDES,
    Capabilities,
    Operation,
    bridge_provides,
    satisfied,
)
from fl_mcp.errors import NotePlaneUnavailable, WriteRejected
from fl_mcp.fl.process import bridge_pid, fl_process_running, pid_alive
from fl_mcp.schemas import Note, Target

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
    log.warning("the Bridge is loaded but has not ticked for %d s; FL's GUI thread is not pumping", age)
    raise errors.ToolError(
        "note_plane_unavailable",
        f"FL Studio has stopped responding to note edits for {age} s, so these notes were not written.",
        "FL is blocked, usually by a dialog or a locked screen. Wait a moment and retry the write.",
        retryable=True,
    )


def _saved_at(session: FLSession) -> datetime | None:
    """When the project was last written. FL saving its live state (or the
    file lane writing it) makes the file current for every target, so it
    supersedes every record it postdates - including a person's Cmd+S."""
    if session.project is None:
        return None
    try:
        return datetime.fromtimestamp(session.project.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def known_notes(session: FLSession, target: Target) -> list[Note] | None:
    """The target's notes as FL has them now: this session's confirmed write,
    else the project (FL loaded it, or saved it since, so it holds the rest).
    None when neither exists: the session never saw that track's notes."""
    written = session.store.notes_at(target, _saved_at(session))
    if written is not None:
        return written
    if session.project is None or not session.project.is_file():
        return None
    try:
        return _project_notes(session, target)
    except (flp.FlpError, OSError):
        # A project caught mid-save by FL reads torn; that is "unknown", not a crash.
        log.exception("could not read pattern %d channel %d from %s", target.pattern, target.channel, session.project)
        return None


def read_notes(session: FLSession, target: Target) -> list[Note]:
    notes = known_notes(session, target)
    if notes is None:
        raise NotePlaneUnavailable(
            f"The notes on pattern {target.pattern}, channel {target.channel} cannot be read: "
            "this session has no copy of the song FL opened.",
            fix="Write this track with mode='replace'; reading it back afterwards works.",
            retryable=False,
        )
    return notes


def all_notes(session: FLSession) -> dict[tuple[int, int], list[Note]]:
    """Every (pattern, channel)'s notes as FL has them now: the project FL
    loaded, with each target this session wrote replaced by its confirmed state."""
    by_target: dict[tuple[int, int], list[Note]] = {}
    if session.project is not None and session.project.is_file():
        project = flp.read(session.project)
        for note in project.notes:
            by_target.setdefault((note.pattern, note.channel), []).append(_tool_note(note, project.ppq))
    by_target.update(session.store.written(_saved_at(session)))
    return by_target


def _commit(
    session: FLSession,
    op: Operation,
    before: list[Note],
    after: list[Note],
    backend: str,
    command_id: int,
) -> list[Note]:
    """Make ``after`` - the state FL confirmed - this session's state of the target.

    The store sets the state before it appends to the durable action log, so
    a failing log append cannot leave reads stale; that failure must not fail
    an edit FL already applied, and it is logged with its traceback (found
    live: a store-path bug made every applied write report a traceback)."""
    try:
        session.store.record_write(op.target, before, after, op.mode, backend, command_id)
    except Exception:
        log.exception("action-log append failed after the %s backend applied", backend)
    # The agent-echo suppressant (docs/27 §5): report the confirmed state so
    # the observer's next diff never attributes this write to the user.
    session.observer.record_agent_write(op.target, after)
    return after


class FileLane:
    """Project-write: the file on disk is the source of truth; FL reloads to
    show it. write_notes verifies by re-reading the bytes it wrote."""

    name: Literal["project-write"] = "project-write"

    def __init__(self, session: FLSession) -> None:
        self._session = session

    def can_serve(self, op: Operation, caps: Capabilities) -> bool:
        return op.kind in NOTE_KINDS and satisfied(op.requires, FILE_PROVIDES)

    def apply(self, op: Operation, data: Sequence[Note]) -> list[Note]:
        # clear_notes IS a replace-with-empty write (ops/notes.py sends
        # data=[] and mode="replace"); one path serves both kinds.
        session = self._session
        target = op.target
        _refuse_if_session_bridge_stalled(session)
        session._prepare_file_write()
        before = known_notes(session, target) or []
        try:
            flp.write_notes(session.project, list(data), pattern=target.pattern, channel=target.channel, mode=op.mode)
        except flp.FlpError as exc:
            log.warning("project-file %s did not verify: %s", op.kind, exc)
            raise WriteRejected(
                f"The notes could not be written: {exc}",
                fix="The song was left unchanged. Retry the write.",
            ) from exc
        after = _commit(session, op, before, _project_notes(session, target), self.name, NO_COMMAND_ID)
        session._reload_fl()
        return after


class BridgeLane:
    """Live targeted notes via the native Bridge plugin, which consumes the
    same v2 spool as the pyscript but is not target-blind: as an effect it can
    set PatNum on FHD_AddNotesToPR (licensed SDK declaration; PROBE_VERDICT Q2)."""

    name: Literal["bridge"] = "bridge"

    def __init__(self, session: FLSession) -> None:
        self._session = session

    def can_serve(self, op: Operation, caps: Capabilities) -> bool:
        return op.kind in NOTE_KINDS and satisfied(op.requires, bridge_provides(op, caps))

    def apply(self, op: Operation, data: Sequence[Note]) -> list[Note] | None:
        session = self._session
        target, notes, mode = op.target, list(data), op.mode
        before = known_notes(session, target)
        self._select_target_channel(target.channel)
        if op.kind == "write_notes":
            # add_notes is the bridge wire's write vocabulary; clear_notes is
            # its own wire op (the consumer replaces with empty).
            command_id = self._send("add_notes", target=target, notes=notes, mode=mode, ppq=self._ppq())
        else:
            command_id = self._send("clear_notes", target=target)
        # FL acknowledged the command, so it applied it: the acknowledged edit
        # IS the confirmed state at this boundary (FHD_AddNotesToPR has no
        # getter). Where FL can save, its re-read save is stronger evidence.
        after = self._saved_state(target, command_id)
        if after is None and mode == "replace":
            after = notes
        elif after is None and before is not None:
            after = [*before, *notes]
        if after is None:
            # A merge onto a track this session never saw: FL has the notes,
            # but what else is on the track is unknown, so nothing is recorded.
            log.info("bridge command %d merged onto an unknown baseline; state not recorded", command_id)
            return None
        return _commit(session, op, before or [], after, self.name, command_id)

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
                f"FL Studio would not select channel {target_channel}: {detail}. No notes were written.",
                "Retry the write after FL finishes its current operation.",
                retryable=True,
            )
        selected = _selected_channel(session.link)
        if selected == target_channel:
            return
        actual = "an unknown channel" if selected is None else f"channel {selected}"
        raise errors.ToolError(
            "fl_api_error",
            f"FL Studio stayed on {actual} instead of channel {target_channel}, so no notes were written.",
            "Retry the write after FL settles.",
            retryable=True,
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
            log.warning("the Bridge refused spool command %d (%s): %s", cmd.id, op, resp.err)
            raise WriteRejected(
                "FL Studio refused the note edit, so nothing was written.",
                fix="Retry the write.",
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

    def _saved_state(self, target: Target, command_id: int) -> list[Note] | None:
        """FL's saved post-write state of the target, when FL can save.

        Saving through FL closes the live-to-file boundary without a reload,
        so the parsed file is a readback of the live session. A trial FL
        cannot save, and a session without a readable project has nothing to
        save to; both, and a failed save, leave FL's acknowledgement as the
        confirmation (None here).
        """
        session = self._session
        project = session.project
        if session.fl_license is FLLicense.TRIAL or project is None or not project.is_file():
            return None
        try:
            controls.save_project(session)
            return _project_notes(session, target)
        except (errors.ToolError, flp.FlpError, OSError):
            log.exception("bridge command %d applied; its saved-state readback failed", command_id)
            return None


def _tool_note(note: flp.FlpNote, ppq: int) -> Note:
    """One decoded project note in tool units."""
    return Note(
        key=note.key,
        start=note.position / ppq,
        length=note.length / ppq,
        velocity=min(note.velocity / 127, 1.0),
        pan=note.pan / 128,
    )


def _project_notes(session: FLSession, target: Target) -> list[Note]:
    """Decode one target from the project file."""
    ppq = flp.read(session.project).ppq
    return [_tool_note(n, ppq) for n in flp.notes_at(session.project, target.pattern, target.channel)]
