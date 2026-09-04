"""Regression proofs for machine-wide FL boot serialization."""

from __future__ import annotations

import json
import os
import signal
import threading
from pathlib import Path

import pytest

from fl_mcp import errors
from fl_mcp.fl import process
from fl_mcp.session import lifecycle


class _Link:
    def reconnect(self) -> None:
        pass

    def settle(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _temporary_launch_lock_root(monkeypatch, tmp_path):
    """Lifecycle tests must never lock the user's real app-data directory."""
    monkeypatch.setattr(process, "app_data_root", lambda: tmp_path / "app-data")


def test_reload_releases_launch_lock_after_a_launch_error(monkeypatch, tmp_path):
    """A failed launcher never strands later session reloads behind its lock."""
    monkeypatch.setattr(lifecycle, "fl_process_running", lambda: False)
    monkeypatch.setattr(lifecycle, "FL_OPEN_ATTEMPTS", 1)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _: None)
    calls = 0

    def launch(project: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("launcher failed")

    monkeypatch.setattr(lifecycle, "launch_fl", launch)
    with pytest.raises(errors.ToolError):
        lifecycle.reload_fl(tmp_path / "a.flp", _Link())
    later = threading.Thread(target=lifecycle.reload_fl, args=(tmp_path / "b.flp", _Link()))
    later.start()
    later.join(2)
    assert not later.is_alive(), "a launch error must release the lock for another caller"
    assert calls == 2


def test_session_reload_is_refused_before_lifecycle_touches_fl(monkeypatch, tmp_path):
    """The lifecycle seam itself preserves the no-session-reboot invariant."""
    launched: list[bool] = []
    monkeypatch.setattr(lifecycle, "launch_fl", lambda *args, **kwargs: launched.append(True))

    with pytest.raises(errors.ToolError) as excinfo:
        lifecycle.reload_fl(tmp_path / "agent.flp", _Link(), spool_root=tmp_path / "session")

    assert excinfo.value.category == "file_lane_requires_reload"
    assert launched == []


def test_launch_spawns_the_executable_and_returns_its_bridge_pid(monkeypatch, tmp_path):
    """A launch owns a new FL process; the port file supplies its real PID."""
    monkeypatch.setattr(process.sys, "platform", "darwin")
    executable = _fake_fl_executable(tmp_path)
    monkeypatch.setattr(process, "fl_app", lambda: executable.parents[2])
    monkeypatch.setattr(process, "fl_executable_name", lambda: executable.name)
    spool = tmp_path / "session"
    project = tmp_path / "song.flp"

    pid = process.launch_fl(project, spool_root=spool)

    try:
        recorded = json.loads((spool / "launch.json").read_text())
        assert pid == recorded["pid"]
        assert recorded == {
            "pid": pid,
            "project": str(project),
            "spool_root": str(spool),
            "agent_project": str(project),
        }
        assert os.getsid(pid) == pid
    finally:
        os.killpg(pid, signal.SIGKILL)


def test_two_session_launches_spawn_two_fl_processes(monkeypatch, tmp_path):
    """Direct executable launches cannot focus another session's FL instead."""
    monkeypatch.setattr(process.sys, "platform", "darwin")
    executable = _fake_fl_executable(tmp_path)
    monkeypatch.setattr(process, "fl_app", lambda: executable.parents[2])
    monkeypatch.setattr(process, "fl_executable_name", lambda: executable.name)
    first_spool, second_spool = tmp_path / "first", tmp_path / "second"

    first_pid = process.launch_fl(tmp_path / "first.flp", spool_root=first_spool)
    second_pid = process.launch_fl(tmp_path / "second.flp", spool_root=second_spool)

    try:
        assert first_pid != second_pid
        assert process.pid_alive(first_pid)
        assert process.pid_alive(second_pid)
    finally:
        os.killpg(first_pid, signal.SIGKILL)
        os.killpg(second_pid, signal.SIGKILL)


def _fake_fl_executable(tmp_path: Path) -> Path:
    """A tiny stand-in that reports the child environment through the port file."""
    executable = tmp_path / "FL Studio.app" / "Contents" / "MacOS" / "OsxFL"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/bin/sh\n"
        "mkdir -p \"$FLMCP_SPOOL_ROOT\"\n"
        "printf '{\"pid\":%s}' \"$$\" > \"$FLMCP_SPOOL_ROOT/bridge_udp.json\"\n"
        "printf '{\"pid\":%s,\"project\":\"%s\",\"spool_root\":\"%s\",\"agent_project\":\"%s\"}' "
        "\"$$\" \"$1\" \"$FLMCP_SPOOL_ROOT\" \"$FLMCP_AGENT_PROJECT\" > \"$FLMCP_SPOOL_ROOT/launch.json\"\n"
        "sleep 30\n"
    )
    executable.chmod(0o755)
    return executable
