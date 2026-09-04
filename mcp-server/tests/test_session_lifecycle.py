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


def test_concurrent_session_reloads_wait_for_the_first_port_file(monkeypatch, tmp_path):
    """The second kill/boot cannot begin before the first session publishes."""
    first_launch = threading.Event()
    release_first = threading.Event()
    second_launcher = threading.Event()
    launches: list[int] = []

    monkeypatch.setattr(lifecycle, "fl_process_running", lambda: False)
    monkeypatch.setattr(lifecycle, "bridge_pid", lambda root: None)

    def launch(
        project: Path, *, spool_root: Path, killed_pid: int | None, deadline: float
    ) -> int:
        launches.append(len(launches) + 1)
        if len(launches) == 1:
            first_launch.set()
            assert release_first.wait(2)
        else:
            second_launcher.set()
        return 100 + len(launches)

    monkeypatch.setattr(lifecycle, "launch_fl", launch)
    root = tmp_path / "session"
    threads = [
        threading.Thread(target=lifecycle.reload_fl, args=(tmp_path / "a.flp", _Link()), kwargs={"spool_root": root}),
        threading.Thread(target=lifecycle.reload_fl, args=(tmp_path / "b.flp", _Link()), kwargs={"spool_root": root}),
    ]
    threads[0].start()
    assert first_launch.wait(2)
    threads[1].start()
    assert not second_launcher.is_set(), "the second reload waits for the first port-file writer"
    release_first.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert launches == [1, 2]


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


def test_reload_releases_launch_lock_after_port_file_deadline(monkeypatch, tmp_path):
    """A missing fresh Bridge PID also releases the machine-wide boot lock."""
    monkeypatch.setattr(lifecycle, "fl_process_running", lambda: False)
    monkeypatch.setattr(lifecycle, "FL_OPEN_ATTEMPTS", 1)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _: None)
    monkeypatch.setattr(lifecycle, "bridge_pid", lambda root: None)
    def no_bridge_pid(
        project: Path, *, spool_root: Path, killed_pid: int | None, deadline: float
    ) -> int:
        raise OSError("no Bridge PID")

    monkeypatch.setattr(lifecycle, "launch_fl", no_bridge_pid)

    with pytest.raises(errors.ToolError):
        lifecycle.reload_fl(tmp_path / "a.flp", _Link(), spool_root=tmp_path / "session")
    monkeypatch.setattr(
        lifecycle, "launch_fl", lambda project, *, spool_root, killed_pid, deadline: 99
    )
    later = threading.Thread(
        target=lifecycle.reload_fl,
        args=(tmp_path / "b.flp", _Link()),
        kwargs={"spool_root": tmp_path / "session"},
    )
    later.start()
    later.join(2)
    assert not later.is_alive(), "a port-file deadline must release the lock for another caller"


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
