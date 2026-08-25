"""The live backend: edit the piano roll that is already open, in place.

This is the one path where notes appear in the session the user is looking at —
nothing reloads, nothing is lost, and edits land while FL stays open on their
work. Everything else either reloads the project (see `autonomous.py`) or waits
for a menu click.

It works by queueing a command to the spool and then pressing the FL shortcut
bound to the piano roll script, which applies it. Two things have to be true,
and neither can be automated:

1. macOS must permit synthetic keystrokes (Accessibility + Automation).
2. FL must have a keyboard shortcut bound to "MCP Write Notes". FL assigns none
   automatically and stores the binding somewhere we cannot read, so this is
   recorded by a marker file the user creates once, after confirming the
   shortcut actually runs the script.

`readiness()` reports precisely which of those is missing, so a failure is never
a silent no-op.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import spool

# Written by `harness.enable_autonomy` (or by hand) once the user has confirmed
# the shortcut runs the script. Its absence means "unverified", not "broken".
HOTKEY_MARKER = Path(__file__).resolve().parents[3] / "harness" / ".hotkey_bound"


@dataclass
class Readiness:
    ready: bool
    permission: bool
    hotkey: bool
    fl_running: bool
    detail: str

    def blockers(self) -> list[str]:
        out = []
        if not self.fl_running:
            out.append("FL Studio is not running")
        if not self.permission:
            out.append(f"macOS automation blocked: {self.detail}")
        if not self.hotkey:
            out.append("no confirmed FL shortcut bound to 'MCP Write Notes'")
        return out


def readiness() -> Readiness:
    try:
        from harness import trigger
    except ImportError:
        return Readiness(False, False, False, False, "harness package not importable")

    fl_running = trigger.is_fl_running()
    permission = trigger.check_permission()
    hotkey = HOTKEY_MARKER.exists()
    return Readiness(
        ready=fl_running and permission.granted and hotkey,
        permission=permission.granted,
        hotkey=hotkey,
        fl_running=fl_running,
        detail=permission.detail,
    )


def available() -> bool:
    return readiness().ready


def apply(command_id: int, timeout: float = 15.0) -> dict[str, Any] | None:
    """Press the shortcut and wait for the in-FL script to report back.

    Returns the result, or None if the trigger could not be sent or the script
    never answered — the caller then falls back to another backend rather than
    claiming an edit that did not happen.
    """
    try:
        from harness import trigger
    except ImportError:
        return None

    try:
        trigger.run_piano_roll_script()
    except Exception:  # noqa: BLE001 - a failed trigger is a fallback, not a crash
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        result = spool.read_result()
        if result is not None and result.get("id") == command_id:
            return result
        time.sleep(0.25)
    return None
