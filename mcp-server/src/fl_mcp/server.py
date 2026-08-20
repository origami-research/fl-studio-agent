"""FL Studio MCP server — v0 vertical slice.

Exposes a small set of tools that let a model write notes into FL Studio's
piano roll. Commands go out over the spool (see spool.py); a piano roll script
inside FL applies them.

Tool descriptions state the v0 workflow explicitly, because the model needs to
know the user must trigger the script — otherwise it reports success the user
hasn't seen yet.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from . import spool
from .notes import NoteError, describe, normalize_notes

SCRIPT_NAME = "MCP Write Notes"

_TRIGGER_HINT = (
    f"In FL Studio, open the piano roll on the channel you want, then run "
    f"Tools > Scripting > '{SCRIPT_NAME}'."
)

mcp = MCPServer("fl-studio")


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
def write_notes(notes: list[dict], mode: str = "merge") -> dict:
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

    spool.clear_stale_results()
    try:
        cmd = spool.put_command("add_notes", mode=mode, notes=parsed)
    except spool.SpoolUnavailable as exc:
        return {"status": "error", "error": str(exc)}
    summary = describe(parsed) + (", replacing existing notes" if mode == "replace" else "")
    return _queued(cmd, summary)


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
def fl_status() -> dict:
    """Check the setup and whether the last queued command was applied.

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
    mcp.run()
