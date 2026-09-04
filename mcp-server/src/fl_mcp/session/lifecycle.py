"""FL process lifecycle: the quarantined SIGKILL + reopen reload."""

from __future__ import annotations

import time
from pathlib import Path

from fl_mcp import errors
from fl_mcp.fl.process import (
    FL_BOOT_TIMEOUT_SECONDS,
    bridge_pid,
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

    with fl_launch_lock():
        killed_pid = None
        if fl_process_running():
            # SIGKILL, because a polite quit prompts to save and would hang
            # unattended. Only used for the project-write backend, where the
            # file on disk - not the in-memory session - is the source of truth.
            # fl_kill signals exact pids, never a pkill pattern (r4 B2-3).
            killed_pid = bridge_pid(spool_root) if spool_root is not None else None
            fl_kill()
            deadline = time.monotonic() + FL_STOP_TIMEOUT_SECONDS
            while time.monotonic() < deadline and fl_process_running():
                time.sleep(0.2)

        last = ""
        boot_deadline = time.monotonic() + FL_BOOT_TIMEOUT_SECONDS if spool_root is not None else None
        for attempt in range(FL_OPEN_ATTEMPTS):
            if boot_deadline is not None and time.monotonic() >= boot_deadline:
                break
            try:
                if spool_root is None:
                    launch_fl(project)
                else:
                    launched_pid = launch_fl(
                        project,
                        spool_root=spool_root,
                        killed_pid=killed_pid,
                        deadline=boot_deadline,
                    )
            except OSError as exc:
                last = str(exc)
            else:
                if spool_root is None:
                    link.reconnect()
                    link.settle()
                    return None
                if launched_pid is not None:
                    link.reconnect()
                    link.settle()
                    return launched_pid
                last = "this session's Bridge port file did not publish a fresh live PID"
            if boot_deadline is None:
                time.sleep(1.0 + attempt)
            else:
                time.sleep(min(1.0 + attempt, max(0.0, boot_deadline - time.monotonic())))
    # The edit already landed in the file and was recorded; only the
    # relaunch failed - say exactly that, never "the write failed".
    raise errors.ToolError(
        "fl_api_error",
        f"The change IS in {project.name}, but FL Studio could not be "
        f"relaunched to show it after {FL_OPEN_ATTEMPTS} attempts: {last}",
        "Open the project in FL Studio manually, then run fl_doctor.",
        retryable=True,
    )
