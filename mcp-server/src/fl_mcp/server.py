"""FL Studio MCP server — v0 vertical slice.

Exposes a small set of tools that let a model write notes into FL Studio's
piano roll. Commands go out over the spool (see spool.py); a piano roll script
inside FL applies them.

Tool descriptions state the v0 workflow explicitly, because the model needs to
know the user must trigger the script — otherwise it reports success the user
hasn't seen yet.
"""

from __future__ import annotations

import time
from typing import Any

import sys
from pathlib import Path

# The optional auto-trigger lives in the repo's harness package.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.mcpserver import MCPServer

from . import spool
from .notes import NoteError, describe, normalize_notes

SCRIPT_NAME = "MCP Write Notes"

_TRIGGER_HINT = (
    f"In FL Studio, open the piano roll on the channel you want, then run "
    f"Tools > Scripting > '{SCRIPT_NAME}'."
)

mcp = MCPServer("fl-studio")


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
    notes: list[dict], mode: str = "merge", channel: str | int | None = None
) -> dict:
    """Queue notes to be written into FL Studio's piano roll.

    The notes land in whichever piano roll the user has open when they run the
    script, so ask the user which instrument they want before writing if it
    isn't obvious. Targeting a specific channel or pattern is not supported yet.

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
        Confirmation that the command is queued, plus the step the user must
        take. The edit is NOT applied until the user runs the script in FL.
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


@mcp.tool()
def clear_notes() -> dict:
    """Queue a command to delete every note in the open piano roll.

    Like write_notes, this takes effect only once the user runs the script in
    FL Studio. It clears the piano roll the user has open at that moment.
    """
    spool.clear_stale_results()
    try:
        cmd = spool.put_command("clear_notes")
    except spool.SpoolUnavailable as exc:
        return {"status": "error", "error": str(exc)}
    return _queued(cmd, "clear all notes in the open piano roll")


@mcp.tool()
def set_tempo(bpm: float) -> dict:
    """Set the project tempo in beats per minute.

    Args:
        bpm: Tempo, 10-999. Fractional values are kept (e.g. 128.5).

    Genres sit in rough ranges if you need a default: house and bass house
    124-132, hip hop 80-95, drum and bass 172-176, trap 140-150 (often felt at
    half time).
    """
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
def fl_status() -> dict:
    """Check the setup and whether the last queued command was applied.

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
    mcp.run()
