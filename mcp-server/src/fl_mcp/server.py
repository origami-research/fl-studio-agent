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
import time
from typing import Any

import sys
from pathlib import Path

# The optional auto-trigger lives in the repo's harness package.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.mcpserver import MCPServer

from . import control_midi, errors, router, schemas, spool
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


def _try_live(command_id: int) -> dict[str, Any] | None:
    """Edit the open piano roll in place — preferred when it is usable.

    Ranked above the project-write backend because it is the only one that does
    not reload FL: the user keeps their session, and sees notes appear in the
    piano roll they are looking at. It needs two one-time grants, so it is not
    always available; see live.readiness().
    """
    try:
        from . import live
    except ImportError:
        return None
    if not live.available():
        return None
    result = live.apply(command_id)
    return result if result is not None and result.get("ok") else None


def _try_project_write(
    notes: list[dict[str, Any]] | None,
    mode: str,
    channel: str | int | None = None,
) -> dict[str, Any] | None:
    """Write straight into the agent project — the fully autonomous path.

    Preferred when an agent project is configured, because it needs no user
    action and no macOS permission. Returns None when unavailable so the caller
    falls back to the spool, which is non-destructive to a live session.
    """
    try:
        from . import autonomous
    except ImportError:
        return None
    if not autonomous.available():
        return None
    result = autonomous.apply_notes(notes or [], mode=mode, channel=channel)
    return result if result.get("ok") else None


def _queued(
    cmd: dict[str, Any], summary: str, tried_live: bool = False
) -> dict[str, Any]:
    """Build the response for a command that could not be applied.

    `tried_live` must be True when the caller already attempted the live
    backend. Retrying here would press FL's shortcut a second time and apply
    the same notes twice.
    """
    applied = None if tried_live else _try_live(cmd["id"])
    if applied is not None:
        return {
            "status": "applied",
            "backend": "live",
            "command_id": cmd["id"],
            "applied": summary,
            "notes_in_piano_roll": applied.get("note_count"),
            "note": (
                "Applied in place to the piano roll you have open. FL was not "
                "reloaded and nothing was lost."
            ),
        }

    from . import live

    state = live.readiness()
    return {
        "status": "queued",
        "command_id": cmd["id"],
        "queued": summary,
        "next_step": _TRIGGER_HINT,
        "live_backend_blocked_by": state.blockers(),
        "note": (
            "Nothing has changed in FL Studio yet. Live editing is unavailable, "
            "so the user must run the script. Run `python -m harness.doctor` for "
            "the fix, then call fl_status to confirm."
        ),
    }


@mcp.tool()
def write_notes(
    notes: list[dict],
    mode: str = "merge",
    channel: str | int | None = None,
    pattern: int | str = -1,
) -> dict:
    """Write notes into FL Studio's piano roll.

    Two routes (docs/17 backend router):
    - Default (no explicit `pattern`): the backend cascade - live in-place edit
      if the grants are set up, else the project is rewritten and FL reopened
      (the permission-free default), else queued for the user to trigger. The
      result reports which backend ran; `channel` targets an instrument by name
      or index.
    - Explicit `pattern` (and optional `channel`): targets that pattern/channel
      directly through the Bridge plugin and blocks until it confirms the write.
      Requires the Bridge plugin loaded and connected (run fl_doctor).

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
        mode: "merge" keeps existing notes (default), "replace" clears the piano
            roll first. Use "replace" when rewriting a part, "merge" when adding
            a layer such as a countermelody.
        channel: Which instrument to write to — a name ("Lead", "Bass",
            "Chords") or a rack index. Defaults to the most melody-appropriate
            channel, preferring a lead over a bass and never a drum. Call
            list_channels to see what this project has.

    Returns:
        Confirmation with the backend that ran (live / project-write / queued),
        or a structured error.
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

    summary = describe(parsed) + (", replacing existing notes" if mode == "replace" else "")

    # Explicit target -> the Bridge plugin path: targets an arbitrary
    # pattern/channel and blocks for confirmation (docs/17 router follow-up).
    if pattern != -1:
        target = schemas.Target(pattern=pattern, channel=channel if channel is not None else -1)
        parsed_notes = [schemas.Note.model_validate(n) for n in parsed]
        try:
            resp, cmd = get_session().write_notes(target, parsed_notes, mode)
        except errors.ToolError as exc:
            return exc.to_dict()
        if not resp.ok:
            return errors.fl_api_error("add_notes", resp.err or "unknown error").to_dict()
        return {
            "status": "ok",
            "backend": "bridge",
            "command_id": cmd.id,
            "notes_written": resp.notes_written,
            "target": {"pattern": target.pattern, "channel": target.channel},
        }

    # Live first: it is the only backend that edits the session the user is
    # looking at. The alternative reloads FL, which is correct but disruptive,
    # so it is a fallback rather than the default.
    spool.clear_stale_results()
    try:
        cmd = spool.put_command("add_notes", mode=mode, notes=parsed)
    except spool.SpoolUnavailable as exc:
        return {"status": "error", "error": str(exc)}

    applied = _try_live(cmd["id"])
    if applied is not None:
        # Backend-agnostic write-through: the live backend edits "whatever's
        # open", so record against the current target (docs/17).
        _record_cascade_write(schemas.Target(pattern=-1, channel=-1), parsed, mode, "live", cmd["id"])
        return {
            "status": "applied",
            "backend": "live",
            "applied": summary,
            "notes_in_piano_roll": applied.get("note_count"),
            "note": (
                "Applied in place to the piano roll you have open. FL was not "
                "reloaded and nothing was lost."
            ),
        }

    written = _try_project_write(parsed, mode, channel)
    if written is not None:
        _record_cascade_write(
            schemas.Target(pattern=1, channel=written.get("channel") if written.get("channel") is not None else -1),
            parsed, mode, "project-write", cmd["id"],
        )
        return {
            "status": "applied",
            "applied": summary,
            "backend": written["backend"],
            "project": written["project"],
            "notes_in_pattern": written["notes_in_pattern"],
            "channel": written.get("channel"),
            "channel_name": written.get("channel_name"),
            "where_to_look": (
                f"FL Studio reopened on the project. Double-click channel "
                f"{written.get('channel')} ('{written.get('channel_name')}') in the "
                f"Channel Rack to open its piano roll and see the notes."
            ),
            "note": (
                "Live in-place editing was unavailable, so the project file was "
                "rewritten and FL reopened on it. Run `python -m harness.doctor` "
                "to enable live editing."
            ),
        }

    return _queued(cmd, summary, tried_live=True)


def _record_cascade_write(
    target: schemas.Target, parsed: list[dict[str, Any]], mode: str, backend: str, command_id: int
) -> None:
    """Record a cascade-backend write into the shadow score + action log.

    Side effect only: a recording failure must never break the user's edit,
    which already landed (router.record_note_write swallows its own errors).
    """
    session = get_session()
    router.record_note_write(session.shadow, session.actions, target, parsed, mode, backend, command_id)


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
    it wrote, may be stale if a human edited by hand) vs "project" (ground
    truth parsed from the saved .flp - call sync_from_project to refresh it).

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
    # Note.key is "C4"-or-MIDI; resolve to MIDI before an integer range compare
    # (comparing a note-name string to an int would raise, not filter).
    if key_min is not None:
        notes = [n for n in notes if schemas._resolve_key(n.key) >= key_min]
    if key_max is not None:
        notes = [n for n in notes if schemas._resolve_key(n.key) <= key_max]
    return {
        "status": "ok",
        "source": ref.provenance,
        "as_of": ref.as_of.isoformat(),
        "stale": ref.stale,
        "notes": [json.loads(n.model_dump_json()) for n in notes],
    }


@mcp.tool()
def sync_from_project(pattern: int = 1) -> dict:
    """Refresh the shadow score from ground truth: parse the saved .flp.

    FL can't be asked what notes exist live, so this reads them back out of the
    project file (docs/11 readback) and rebuilds the shadow score for that
    pattern with provenance "project" - the authoritative source after a human
    hand-edit. Requires the project-write (autonomous) backend; without it there
    is no saved project to parse.
    """
    try:
        from . import autonomous
    except ImportError:
        return {"status": "error", "error": "project read-back unavailable"}
    if not autonomous.available():
        return {
            "status": "error",
            "error": "No agent project to read. Run: python -m fl_mcp.make_agent_project",
        }

    verified = autonomous.verify(pattern=pattern)
    if not verified.get("ok"):
        return {"status": "error", "error": verified.get("error", "read failed")}

    # Group the parsed rows by channel and rebuild the shadow score per target.
    by_channel: dict[int, list[schemas.Note]] = {}
    for row in verified.get("notes", []):
        by_channel.setdefault(int(row["channel"]), []).append(
            schemas.Note(key=int(row["midi"]), start=float(row["start_beat"]), length=float(row["length_beats"]))
        )
    session = get_session()
    synced = []
    for channel, notes in by_channel.items():
        target = schemas.Target(pattern=pattern, channel=channel)
        ref = session.shadow.write(target, notes, mode="replace", provenance="project")
        synced.append({"channel": channel, "notes": len(notes), "id": ref.id})
    return {
        "status": "ok",
        "source": "project",
        "pattern": pattern,
        "note_count": verified.get("note_count", 0),
        "synced_targets": synced,
        "note": "Shadow score rebuilt from the saved project. get_notes now reports source 'project'.",
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
    """Undo the last step via the control plane, and report what it was.

    Invokes FL's own undo (`general.undo`) over the live control plane and names
    the model's last recorded action (docs/11 action log). Note the single-writer
    caveat: `general.undo` undoes FL's *last* step, which is the model's last
    action only if nobody hand-edited in between. Without a control plane it
    falls back to identifying the action so the user can Ctrl+Z it.
    """
    session = get_session()
    last = session.actions.last()
    identified = (
        {"id": last.id, "op": last.op, "undo_step": last.undo_step} if last is not None else None
    )
    resp = router.try_live_control(session, "general.undo")
    if resp is not None:
        return {
            "status": "applied",
            "backend": "live",
            "undone": identified,
            "note": (
                "Invoked FL's undo. This reverses FL's last step - the model's "
                "last action unless the user hand-edited since."
            ),
        }
    if identified is None:
        return {
            "status": "error",
            "error": "nothing to undo",
            "fix": "No actions recorded yet, and no live control plane to call FL's undo.",
        }
    return {
        "status": "identified",
        "would_undo": identified,
        "note": "No live control plane; use FL's own Ctrl+Z. Run fl_doctor to connect.",
    }


@mcp.tool()
def set_tempo(bpm: float) -> dict:
    """Set the project tempo in beats per minute.

    Args:
        bpm: Tempo, 10-999. Fractional values are kept (e.g. 128.5).

    Genres sit in rough ranges if you need a default: house and bass house
    124-132, hip hop 80-95, drum and bass 172-176, trap 140-150 (often felt at
    half time).
    """
    if not 10 <= bpm <= 999:
        return {"status": "error", "error": f"bpm must be 10-999, got {bpm}"}

    # Live tier first (docs/17 control cascade): a connected control plane sets
    # the tempo in place with no FL reopen. Falls through to the file backend.
    # 5s: FL can stay "unsafe at current time" for a few seconds after a load;
    # give the controller's OnIdle deferral (DEFER_MAX_TICKS ~4s) time to land.
    resp = router.try_live_control(get_session(), "mixer.setCurrentTempo", args=[bpm, 1], timeout=5.0)
    if resp is not None:
        return {
            "status": "applied",
            "backend": "live",
            "tempo": bpm,
            "note": f"Project tempo is now {bpm} BPM (live, FL was not reloaded).",
        }

    try:
        from . import autonomous
    except ImportError:
        return {"status": "error", "error": "tempo control unavailable"}

    if not autonomous.available():
        return {
            "status": "error",
            "error": (
                "No agent project configured, so there is no project to retempo. "
                "Run: python -m fl_mcp.make_agent_project"
            ),
        }

    result = autonomous.set_tempo(bpm)
    if not result.get("ok"):
        return {"status": "error", "error": result.get("error")}
    return {
        "status": "applied",
        "tempo": result["tempo"],
        "note": f"Project tempo is now {result['tempo']} BPM.",
    }


@mcp.tool()
def list_channels() -> dict:
    """List the instrument channels available to write notes to.

    Call this when the user names an instrument, or when placing different
    parts on different channels — a bassline on "Bass" and a melody on "Lead"
    rather than stacking everything in one place.
    """
    try:
        from . import autonomous
    except ImportError:
        return {"status": "error", "error": "channel listing unavailable"}
    channels = autonomous.list_channels()
    if not channels:
        return {
            "status": "error",
            "error": "No agent project. Run: python -m fl_mcp.make_agent_project",
        }
    return {
        "status": "ok",
        "channels": channels,
        "default": autonomous.default_channel(),
        "note": "Pass a name or index as write_notes(channel=...).",
    }


@mcp.tool()
def set_channel_mix(
    channel: str | int | None = None,
    volume: float | None = None,
    pan: float | None = None,
    pitch_semitones: int | None = None,
) -> dict:
    """Adjust a channel's volume, pan or pitch.

    Only the values you pass are changed, so setting pan leaves a volume the
    user already adjusted alone.

    Args:
        channel: Name ("Lead") or rack index. Defaults to the melodic channel
            write_notes would use.
        volume: 0.0 (silent) to 1.0 (maximum). FL's default is about 0.78.
        pan: -1.0 hard left, 0.0 centre, 1.0 hard right.
        pitch_semitones: -48 to 48. Use 12 or -12 to shift an octave.

    This controls the channel strip, not the instrument's own sound. Preset,
    filter cutoff, detune and envelope live inside the plugin's saved state,
    which is an opaque blob in the project file and cannot be edited from here.
    """
    if volume is None and pan is None and pitch_semitones is None:
        return {
            "status": "error",
            "error": "Nothing to change — pass volume, pan and/or pitch_semitones.",
        }

    # Live tier (docs/17): volume/pan in place when the control plane is up and
    # the channel is an explicit index. Pitch and name resolution stay on the
    # file path below (no simple live op / would need a resolve round trip).
    session = get_session()
    if session.control is not None and isinstance(channel, int) and pitch_semitones is None:
        applied: dict[str, Any] = {}
        ok = True
        if volume is not None:
            ok = router.try_live_control(session, "channels.setChannelVolume", [channel, volume]) is not None
            if ok:
                applied["volume"] = volume
        if ok and pan is not None:
            ok = router.try_live_control(session, "channels.setChannelPan", [channel, pan]) is not None
            if ok:
                applied["pan"] = pan
        if ok:
            return {"status": "applied", "backend": "live", "channel": channel, **applied}

    try:
        from . import autonomous
    except ImportError:
        return {"status": "error", "error": "mix control unavailable"}
    if not autonomous.available():
        return {
            "status": "error",
            "error": "No agent project. Run: python -m fl_mcp.make_agent_project",
        }

    result = autonomous.set_levels(
        channel=channel, volume=volume, pan=pan, pitch_semitones=pitch_semitones
    )
    if not result.get("ok"):
        return {"status": "error", "error": result.get("error")}
    return {
        "status": "applied",
        "channel": result["channel"],
        "channel_name": result.get("channel_name"),
        "volume": result["volume"],
        "pan": result["pan"],
        "pitch_semitones": result["pitch_semitones"],
    }


@mcp.tool()
def fl_status() -> dict:
    """Check the setup and whether the last queued v0 command was applied.

    Call this after asking the user to run the script, to confirm the edit
    landed and see how many notes the piano roll ended up with. Also reports
    whether the piano roll script is installed.
    """
    from .install import script_installed_path  # local import: optional dependency on FL paths

    # Autonomous backend first: when configured it is what write_notes uses.
    try:
        from . import autonomous

        if autonomous.available():
            verified = autonomous.verify()
            return {
                "backend": "project-write (autonomous, no user action needed)",
                "project": str(autonomous.agent_project()),
                "fl_running": autonomous.fl_is_running(),
                "notes_in_project": verified.get("note_count"),
                "notes": verified.get("notes", [])[:16],
            }
    except ImportError:
        pass

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
