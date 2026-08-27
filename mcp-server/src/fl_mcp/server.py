"""FL Studio MCP server — v0 vertical slice, growing toward the docs/06 catalog.

Two note-plane generations coexist here (see spool.py):

- **v0**: commands go out over a single-pending-command spool; a piano roll
  script inside FL applies them when the user triggers it manually. No
  targeting — notes land in whichever piano roll is open. This is
  `write_notes`/`clear_notes` called with no explicit ``pattern``/``channel``
  (both default to -1, "current"), and is the live, interim note plane
  (docs/14/15 fix item 4): tool descriptions state the workflow explicitly,
  because the model needs to know the user must trigger the script.
- **v1**: an explicit ``pattern``/``channel`` target routes through
  `FLSession`/`SpoolSession` instead — the sequenced spool a native Bridge
  plugin polls automatically (docs/03, docs/04). No plugin is wired into a
  live server yet (docs/12: gated on a probe that hasn't passed), so this path
  is just as honest as v0: it waits for a real confirmation and reports a
  structured `note_plane_unavailable` error rather than claiming success
  nothing has actually verified.

The control plane (`get_session_state`, `fl_doctor`'s control_plane check,
and everything the notebook kernel does) rides `control_midi.py` — the
server-hosted, live-verified MIDI transport (docs/14) — via
`control_midi.get_control()`. `main()` opens it eagerly at process start so
the ports exist before FL Studio launches (docs/14's ordering requirement).
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import control_midi, errors, schemas, spool
from .notes import NoteError, describe, normalize_notes
from .session import FLSession

SCRIPT_NAME = "MCP Write Notes"

_TRIGGER_HINT = (
    f"In FL Studio, open the piano roll on the channel you want, then run "
    f"Tools > Scripting > '{SCRIPT_NAME}'."
)

mcp = MCPServer("fl-studio")

_session: FLSession | None = None


def get_session() -> FLSession:
    global _session
    if _session is None:
        control = None
        try:
            control = control_midi.get_control()
        except control_midi.ControlUnavailable:
            pass  # python-rtmidi missing or ports couldn't open; control-plane tools degrade honestly
        _session = FLSession(control=control)
    return _session


def set_session(session: FLSession | None) -> None:
    """Test seam: inject an FLSession wired to tests/fake_fl.py stand-ins."""
    global _session
    _session = session


def _queued(cmd: dict[str, Any], summary: str) -> dict[str, Any]:
    return {
        "status": "queued",
        "command_id": cmd["id"],
        "queued": summary,
        "next_step": _TRIGGER_HINT,
        "note": (
            "Nothing has changed in FL Studio yet. The edit applies when the user "
            "runs the script. Call fl_status to confirm it was applied."
        ),
    }


@mcp.tool()
def write_notes(notes: list[dict], mode: str = "merge", pattern: int | str = -1, channel: int | str = -1) -> dict:
    """Write notes into FL Studio's piano roll.

    With no pattern/channel (the default), this queues the v0 workflow: notes
    land in whichever piano roll the user has open when they next run the
    in-FL script. Ask the user which instrument they want before writing if
    it isn't obvious.

    Passing an explicit pattern and/or channel targets that pattern/channel
    directly through the Bridge plugin's spool instead, and the tool call
    blocks until the plugin confirms the write (or reports why it couldn't) -
    no manual trigger needed. This requires the Bridge plugin to be loaded and
    connected; if it isn't, the error names the fix (run fl_doctor).

    Args:
        notes: List of note objects. Each requires:
            - key: note name ("C4", "F#3", "Bb5") or MIDI number 0-127.
                   C4 = MIDI 60 = middle C. FL's piano roll labels this "C5"
                   because it displays an octave higher; the pitch is correct.
            - start: position in beats from the start of the pattern (0 = beat 1).
            - length: duration in beats (1.0 = a quarter note).
          Optional per note:
            - velocity: 0.0-1.0, default 0.8
            - pan: 0.0-1.0, default 0.5 (centered)
            - color: 0-15, default 0. Maps to FL's note colors / MIDI channel.
        mode: "merge" keeps existing notes (default), "replace" clears the target
            first. Use "replace" when rewriting a part, "merge" when adding a
            layer such as a countermelody.
        pattern: pattern number, or -1 (default) for "whatever's open".
        channel: channel number, or -1 (default) for "whatever's open".

    Returns:
        v0 (no target): confirmation the command is queued, plus the step the
        user must take. v1 (explicit target): confirmation the plugin actually
        wrote the notes, or a structured error if it didn't.
    """
    if mode not in ("merge", "replace"):
        return {
            "status": "error",
            "error": f"mode must be 'merge' or 'replace', got {mode!r}",
        }

    try:
        parsed = normalize_notes(notes)
    except NoteError as exc:
        return {
            "status": "error",
            "error": str(exc),
            "hint": (
                'Each note looks like {"key": "C4", "start": 0, "length": 1}. '
                "start and length are in beats."
            ),
        }

    if pattern == -1 and channel == -1:
        spool.clear_stale_results()
        try:
            cmd = spool.put_command("add_notes", mode=mode, notes=parsed)
        except spool.SpoolUnavailable as exc:
            return {"status": "error", "error": str(exc)}
        summary = describe(parsed) + (", replacing existing notes" if mode == "replace" else "")
        return _queued(cmd, summary)

    target = schemas.Target(pattern=pattern, channel=channel)
    parsed_notes = [schemas.Note.model_validate(n) for n in parsed]
    try:
        resp, cmd = get_session().write_notes(target, parsed_notes, mode)
    except errors.ToolError as exc:
        return exc.to_dict()
    if not resp.ok:
        return errors.fl_api_error("add_notes", resp.err or "unknown error").to_dict()
    return {
        "status": "ok",
        "command_id": cmd.id,
        "notes_written": resp.notes_written,
        "target": {"pattern": target.pattern, "channel": target.channel},
    }


@mcp.tool()
def clear_notes(pattern: int | str = -1, channel: int | str = -1) -> dict:
    """Delete notes from FL Studio's piano roll.

    With no pattern/channel (the default), queues the v0 workflow: it clears
    whichever piano roll the user has open when they next run the in-FL
    script. An explicit target clears that pattern/channel directly through
    the Bridge plugin and blocks for confirmation - see write_notes for the
    same v0/v1 split.
    """
    if pattern == -1 and channel == -1:
        spool.clear_stale_results()
        try:
            cmd = spool.put_command("clear_notes")
        except spool.SpoolUnavailable as exc:
            return {"status": "error", "error": str(exc)}
        return _queued(cmd, "clear all notes in the open piano roll")

    target = schemas.Target(pattern=pattern, channel=channel)
    try:
        resp, cmd = get_session().clear_notes(target)
    except errors.ToolError as exc:
        return exc.to_dict()
    if not resp.ok:
        return errors.fl_api_error("clear_notes", resp.err or "unknown error").to_dict()
    return {
        "status": "ok",
        "command_id": cmd.id,
        "notes_cleared": resp.notes_written,
        "target": {"pattern": target.pattern, "channel": target.channel},
    }


@mcp.tool()
def get_notes(pattern: int | str, channel: int | str, key_min: int | None = None, key_max: int | None = None) -> dict:
    """Read notes for one pattern/channel from the shadow score (docs/06).

    Always labeled with provenance: "shadow" (the server's own record of what
    it wrote, may be stale if a human edited by hand) vs "project" (a fresh
    read - not implemented yet, see sync_from_project in docs/11).

    Args:
        pattern: pattern number.
        channel: channel number.
        key_min / key_max: optional inclusive MIDI range to filter by.
    """
    target = schemas.Target(pattern=pattern, channel=channel)
    session = get_session()
    ref = session.shadow.ref_at(target)
    if ref is None:
        return {
            "status": "ok",
            "source": "shadow",
            "notes": [],
            "note": "nothing recorded for this target yet - write_notes or sync_from_project first",
        }
    notes = session.shadow.notes_at(target) or []
    if key_min is not None:
        notes = [n for n in notes if n.key >= key_min]  # type: ignore[operator]
    if key_max is not None:
        notes = [n for n in notes if n.key <= key_max]  # type: ignore[operator]
    return {
        "status": "ok",
        "source": ref.provenance,
        "as_of": ref.as_of.isoformat(),
        "stale": ref.stale,
        "notes": [json.loads(n.model_dump_json()) for n in notes],
    }


@mcp.tool()
def get_session_state() -> dict:
    """Tempo, playing?, current pattern, channels, mixer tracks (docs/06).

    Requires the control plane (the FLMCP controller script, over control_midi's
    server-hosted MIDI transport) to be connected. Run fl_doctor if this
    reports control_plane_unavailable.
    """
    session = get_session()
    if session.control is None:
        return errors.control_plane_unavailable("state.snapshot", 0.0).to_dict()
    try:
        resp = session.control.send_request("state.snapshot", timeout=2.0)
    except errors.ToolError as exc:
        return exc.to_dict()
    if not resp.ok:
        return errors.fl_api_error("state.snapshot", resp.err or "unknown error").to_dict()
    return {"status": "ok", **(resp.ret or {})}


@mcp.tool()
def fl_doctor() -> dict:
    """Is everything connected? What exactly is broken and how to fix it (docs/06)."""
    return get_session().doctor()


@mcp.tool()
def undo_last() -> dict:
    """Identify the model's last recorded action (docs/11 action log).

    Stub: does not yet call the control plane's undo (docs/05 general.undo).
    Use FL Studio's own Ctrl+Z for now; this just tells you what it would undo.
    """
    session = get_session()
    last = session.actions.last()
    if last is None:
        return {
            "status": "error",
            "error": "nothing to undo",
            "fix": "No actions have been recorded for this project yet.",
        }
    return {
        "status": "not_implemented",
        "would_undo": {"id": last.id, "op": last.op, "undo_step": last.undo_step},
        "note": (
            "undo_last is a stub: it identifies the action but does not yet call "
            "the control plane's undo. Use FL's own Ctrl+Z for now."
        ),
    }


@mcp.tool()
def fl_status() -> dict:
    """Check the setup and whether the last queued v0 command was applied.

    Call this after asking the user to run the script, to confirm the edit
    landed and see how many notes the piano roll ended up with. Also reports
    whether the piano roll script is installed.
    """
    from .install import script_installed_path  # local import: optional dependency on FL paths

    installed = script_installed_path()
    pending = spool.pending_command()
    result = spool.read_result()
    roots = spool.spool_roots()

    status: dict[str, Any] = {
        "script_installed": installed is not None,
        "spool": [str(r) for r in roots] or "FL Studio settings folder not found",
    }
    if installed is None:
        status["fix"] = (
            "The piano roll script isn't installed. Run: python -m fl_mcp.install"
        )
    else:
        status["script_path"] = str(installed)

    if pending is not None:
        status["pending_command"] = {"id": pending.get("id"), "op": pending.get("op")}
        status["waiting_on"] = _TRIGGER_HINT
    else:
        status["pending_command"] = None

    if result is None:
        status["last_result"] = None
    elif result.get("ok"):
        status["last_result"] = {
            "id": result.get("id"),
            "ok": True,
            "summary": result.get("summary"),
            "notes_in_piano_roll": result.get("note_count"),
            "spool_used": result.get("spool"),
        }
    else:
        status["last_result"] = {
            "id": result.get("id"),
            "ok": False,
            "error": result.get("error"),
        }

    return status


def main() -> None:
    get_session()  # open the MIDI ports now, before the first tool call (docs/14 ordering)
    mcp.run()
