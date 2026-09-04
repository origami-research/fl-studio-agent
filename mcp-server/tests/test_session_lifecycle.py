"""Regression proofs for machine-wide FL boot serialization."""

from __future__ import annotations

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
    monkeypatch.setattr(lifecycle, "RELAUNCH_SETTLE_SECONDS", 0.0)

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


def test_launch_returns_the_live_session_pid_without_waiting(monkeypatch, tmp_path):
    """Direct session launch treats an already-live Bridge owner as ready."""
    monkeypatch.setattr(process.sys, "platform", "darwin")
    monkeypatch.setattr(process, "fl_app", lambda: tmp_path / "FL Studio.app")
    monkeypatch.setattr(process.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "bridge_pid", lambda root: 1234)
    monkeypatch.setattr(process, "pid_alive", lambda pid: pid == 1234)
    monkeypatch.setattr(
        process,
        "wait_for_bridge_pid",
        lambda *args: pytest.fail("a live session PID must not wait for a replacement"),
    )

    assert process.launch_fl(tmp_path / "song.flp", spool_root=tmp_path / "session") == 1234
