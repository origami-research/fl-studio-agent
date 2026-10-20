"""Development-mode FL lifecycle: the quarantined SIGKILL + reopen reload."""

from __future__ import annotations

import time
from pathlib import Path

from fl_mcp import errors
from fl_mcp.fl.process import (
    fl_kill,
    fl_launch_lock,
    fl_process_running,
    launch_fl,
)

from .link import ControlLink

FL_STOP_TIMEOUT_SECONDS = 20.0
FL_OPEN_ATTEMPTS = 6


def reload_fl(
    project: Path | None, link: ControlLink, *, spool_root: Path | None = None
) -> int | None:
    """UGLY(U5), quarantined: FL has no 'reopen this file' API and a polite
    quit blocks on save dialogs. SIGKILL, then directly spawn FL with bounded
    retries."""
    if project is None:
        raise ValueError("_reload_fl requires a resolved project file")
    if spool_root is not None:
        # A session FL must stay alive for its bridge, watcher, and peer
        # sessions. No verified in-place project reload exists, so this path
        # is refused rather than turning a file edit into a process restart.
        raise errors.file_lane_requires_reload()

    with fl_launch_lock():
        if fl_process_running():
            # SIGKILL, because a polite quit prompts to save and would hang
            # unattended. Only used for the project-write backend, where the
            # file on disk - not the in-memory session - is the source of truth.
            # fl_kill signals exact pids, never a pkill pattern (r4 B2-3).
            fl_kill()
            deadline = time.monotonic() + FL_STOP_TIMEOUT_SECONDS
            while time.monotonic() < deadline and fl_process_running():
                time.sleep(0.2)

        last = ""
        for attempt in range(FL_OPEN_ATTEMPTS):
            try:
                launch_fl(project)
            except OSError as exc:
                last = str(exc)
            else:
                link.reconnect()
                link.settle()
                return None
            time.sleep(1.0 + attempt)
    # The edit already landed in the file and was recorded; only the
    # relaunch failed - say exactly that, never "the write failed".
    raise errors.ToolError(
        "fl_api_error",
        f"The change is saved in the song, but FL Studio could not be "
        f"reopened to show it after {FL_OPEN_ATTEMPTS} attempts: {last}",
        "Reopen the song in FL Studio, then continue.",
        retryable=True,
    )
