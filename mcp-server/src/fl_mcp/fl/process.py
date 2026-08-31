"""FL process detection and teardown - THE canonical implementation.

Every "is FL running / stop FL" question is answered here, so the answer
cannot drift between subtly different pgrep invocations again (r4 B2-4:
``pgrep -fi "FL Studio"`` and ``pgrep -x OsxFL`` disagreed, and the ``-f``
substring match fires on any command line that merely mentions FL). The
executable name is derived from the app bundle's Info.plist, never assumed,
so a renamed binary in a future FL version is handled without a code change.
"""

from __future__ import annotations

import contextlib
import logging
import os
import plistlib
import signal
import subprocess
import sys
from functools import cache

from .paths import fl_app

log = logging.getLogger(__name__)

# Warned fallback only (docs/29 §6): used when the bundle's Info.plist cannot
# be read, which on a machine without FL just makes the pgrep find nothing.
_FALLBACK_EXECUTABLE = "OsxFL"


@cache
def fl_executable_name() -> str:
    """CFBundleExecutable from ``fl_app()``'s Info.plist (r4 B2-4)."""
    plist = fl_app() / "Contents" / "Info.plist"
    try:
        with plist.open("rb") as f:
            name = plistlib.load(f).get("CFBundleExecutable")
    except (OSError, plistlib.InvalidFileException):
        name = None
    if not name:
        log.warning(
            "cannot read CFBundleExecutable from %s; falling back to %r",
            plist,
            _FALLBACK_EXECUTABLE,
        )
        return _FALLBACK_EXECUTABLE
    return str(name)


def fl_pids() -> list[int]:
    """PIDs of running FL processes, by EXACT executable match (``pgrep -x``).

    An empty list just means "none found / can't tell" (unsupported platform,
    or no process-listing tool), never a proof that FL isn't running.
    """
    if sys.platform != "darwin":
        return []  # Windows/other: process check not implemented yet (docs/08 roadmap)
    try:
        result = subprocess.run(
            ["pgrep", "-x", fl_executable_name()], capture_output=True, timeout=2
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(line) for line in result.stdout.split() if line.isdigit()]


def fl_process_running() -> bool:
    """Best-effort check for a running FL Studio process (docs/15 fix item 5)."""
    return bool(fl_pids())


def fl_kill() -> None:
    """SIGKILL exactly the FL processes and nothing else.

    Replaces ``pkill -9 -f "FL Studio"`` (r4 B2-3), whose full-command-line
    match also killed a ``tail -f "FL Studio.log"``, an editor holding an
    FL-named file, or any other bystander. Killing by the exact pids from
    ``fl_pids()`` scopes the signal; our own pid is excluded defensively.
    A pid that exits between listing and signalling is not an error.
    """
    me = os.getpid()
    for pid in fl_pids():
        if pid == me:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
