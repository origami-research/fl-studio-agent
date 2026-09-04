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
import csv
import io
import json
import logging
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from functools import cache
from pathlib import Path

from fl_mcp.contract import ENV_AGENT_PROJECT, ENV_SPOOL_ROOT

from .paths import app_data_root, fl_app

log = logging.getLogger(__name__)

# Warned fallback only (docs/29 §6): used when the bundle's Info.plist cannot
# be read, which on a machine without FL just makes the pgrep find nothing.
_FALLBACK_EXECUTABLE = "OsxFL"
_WINDOWS_EXECUTABLE = "FL64.exe"
FL_BOOT_TIMEOUT_SECONDS = 45.0
_launch_lock = threading.RLock()
_launch_lock_state = threading.local()


@contextlib.contextmanager
def fl_launch_lock() -> object:
    """Serialize FL boots across this machine.

    macOS uses an advisory file lock; the in-process lock makes the same
    invariant hold for concurrent callers before they become separate engine
    processes. Windows deliberately remains a no-file-lock platform seam.
    """
    with _launch_lock:
        depth = getattr(_launch_lock_state, "depth", 0)
        _launch_lock_state.depth = depth + 1
        if depth:
            try:
                yield
            finally:
                _launch_lock_state.depth -= 1
            return
        lock_file = None
        try:
            if sys.platform == "darwin":
                import fcntl

                path = app_data_root() / "fl-launch.lock"
                path.parent.mkdir(parents=True, exist_ok=True)
                lock_file = path.open("a+")
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    log.info("waiting for machine-wide FL launch lock")
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                log.info("acquired machine-wide FL launch lock")
            yield
        finally:
            _launch_lock_state.depth -= 1
            if lock_file is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()


def pid_alive(pid: int | None) -> bool:
    """Whether a concrete process PID is still alive, without naming its image."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def bridge_pid(spool_root: Path) -> int | None:
    """Read the current Bridge owner PID from one session's port file."""
    try:
        payload = json.loads((spool_root / "bridge_udp.json").read_text())
    except (OSError, ValueError):
        return None
    pid = payload.get("pid")
    return pid if isinstance(pid, int) and pid > 0 else None


def wait_for_bridge_pid(spool_root: Path, killed_pid: int | None, deadline: float) -> int | None:
    """Wait for this session's live Bridge owner, replacing ``killed_pid`` if set."""
    while time.monotonic() < deadline:
        pid = bridge_pid(spool_root)
        if pid_alive(pid) and (killed_pid is None or pid != killed_pid):
            return pid
        time.sleep(0.1)
    return None


@cache
def fl_executable_name() -> str:
    """CFBundleExecutable from ``fl_app()``'s Info.plist (r4 B2-4)."""
    if sys.platform == "win32":
        return fl_app().name or _WINDOWS_EXECUTABLE
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
    if sys.platform == "win32":
        return _windows_pids()
    if sys.platform != "darwin":
        return []
    try:
        result = subprocess.run(
            ["pgrep", "-x", fl_executable_name()], capture_output=True, timeout=2
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(line) for line in result.stdout.split() if line.isdigit()]


def _windows_pids() -> list[int]:
    """Exact FL image-name lookup through tasklist's machine-readable CSV."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {fl_executable_name()}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows = csv.reader(io.StringIO(result.stdout))
    return [int(row[1]) for row in rows if len(row) >= 2 and row[1].isdigit()]


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
    if sys.platform == "win32":
        for pid in fl_pids():
            if pid != os.getpid():
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], timeout=5)
        return
    me = os.getpid()
    for pid in fl_pids():
        if pid == me:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def fl_kill_pid(pid: int) -> bool:
    """Kill one confirmed FL process, returning whether it was still running.

    Callers that own one marked FL session must not use ``fl_kill()``, which
    intentionally stops every FL process on the machine for reload workflows.
    """
    if pid == os.getpid() or pid not in fl_pids():
        return False
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def launch_fl(
    project: Path,
    *,
    spool_root: Path | None = None,
    killed_pid: int | None = None,
    deadline: float | None = None,
) -> int | None:
    """Ask the platform launcher to open ``project`` without waiting for FL to exit.

    ``spool_root`` is passed only to the child process, so concurrent sessions
    never mutate the server's ambient environment to select their mailbox.
    """
    with fl_launch_lock():
        env = os.environ.copy()
        env[ENV_AGENT_PROJECT] = str(project)
        if spool_root is not None:
            env[ENV_SPOOL_ROOT] = str(spool_root)
        if sys.platform == "darwin":
            subprocess.run(["open", "-a", str(fl_app()), str(project)], check=True, env=env)
            if spool_root is None:
                return None
            # ``open -a`` only focuses an already-running FL. Its port-file PID
            # is success unless this reload explicitly killed that PID.
            pid = bridge_pid(spool_root)
            if killed_pid is None and pid_alive(pid):
                return pid
            pid = wait_for_bridge_pid(
                spool_root,
                killed_pid,
                deadline if deadline is not None else time.monotonic() + FL_BOOT_TIMEOUT_SECONDS,
            )
            if pid is None:
                raise OSError("this session's Bridge port file did not publish a fresh live PID")
            return pid
        if sys.platform == "win32":
            subprocess.Popen([str(fl_app()), str(project)], env=env)
            return None
        raise OSError(f"FL launch is unsupported on {sys.platform}")
