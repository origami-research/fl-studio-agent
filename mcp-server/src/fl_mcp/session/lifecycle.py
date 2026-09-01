"""FL process lifecycle: the quarantined SIGKILL + reopen reload."""

from __future__ import annotations

import time
from pathlib import Path

from fl_mcp import errors
from fl_mcp.fl.process import fl_kill, fl_process_running, launch_fl

from .link import ControlLink

# Right after a SIGKILL, LaunchServices still has the dead process registered;
# it clears in a second or two, so the relaunch waits before retrying.
RELAUNCH_SETTLE_SECONDS = 2.0

FL_STOP_TIMEOUT_SECONDS = 20.0
FL_OPEN_ATTEMPTS = 6


def reload_fl(project: Path | None, link: ControlLink) -> None:
    """UGLY(U5), quarantined: FL has no 'reopen this file' API and a polite
    quit blocks on save dialogs. SIGKILL, wait RELAUNCH_SETTLE_SECONDS,
    then ``open`` with bounded retries."""
    if project is None:
        raise ValueError("_reload_fl requires a resolved project file")

    if fl_process_running():
        # SIGKILL, because a polite quit prompts to save and would hang
        # unattended. Only used for the project-write backend, where the
        # file on disk - not the in-memory session - is the source of truth.
        # fl_kill signals exact pids, never a pkill pattern (r4 B2-3).
        fl_kill()
        deadline = time.monotonic() + FL_STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline and fl_process_running():
            time.sleep(0.2)
        time.sleep(RELAUNCH_SETTLE_SECONDS)

    last = ""
    for attempt in range(FL_OPEN_ATTEMPTS):
        try:
            launch_fl(project)
        except OSError as exc:
            last = str(exc)
        else:
            link.reconnect()
            link.settle()
            return
        time.sleep(1.0 + attempt)
    # The edit already landed in the file and was recorded; only the
    # relaunch failed - say exactly that, never "the write failed".
    raise errors.ToolError(
        "fl_api_error",
        f"The change IS in {project.name}, but FL Studio could not be "
        f"relaunched to show it after {FL_OPEN_ATTEMPTS} attempts: {last}",
        "Open the project in FL Studio manually, then run fl_doctor.",
        retryable=True,
    )
