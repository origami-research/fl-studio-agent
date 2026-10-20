"""Step-1 proofs for the unconnected platform boundary (docs/35)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from fl_mcp.kernel.launch import Launcher, PosixLauncher
from fl_mcp.kernel.windows import AppContainerLauncher
from fl_mcp.platform import (
    BrokerEndpoint,
    FLInstall,
    MacPlatform,
    Platform,
    PlatformUnsupportedError,
    ProcessSweep,
    UnsupportedPlatform,
    WindowsPlatform,
    platform_for,
)
from fl_mcp.session.link import ControlCarrier


@pytest.mark.parametrize(
    ("sys_platform", "expected"),
    [("darwin", MacPlatform), ("win32", WindowsPlatform), ("linux", UnsupportedPlatform)],
)
def test_platform_for_dispatches_only_supported_platforms(sys_platform, expected):
    assert isinstance(platform_for(sys_platform), expected)


def test_unsupported_platform_names_the_platform_and_remedy():
    with pytest.raises(PlatformUnsupportedError, match="'linux'; use darwin or win32"):
        platform_for("linux").fl_install()


def test_kernel_launcher_is_confined_only_where_the_platform_can_confine_it(monkeypatch):
    """Windows (production) is AppContainer; macOS is a dev host with no OS boundary (docs/38)."""
    assert MacPlatform().kernel_launcher().confined is False
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setattr("fl_mcp.platform.runtime.CtypesWin32", lambda icacls: ("win32-api", icacls.name))
    launcher = WindowsPlatform().kernel_launcher()
    assert isinstance(launcher, AppContainerLauncher)
    assert launcher.confined is True
    assert launcher.api == ("win32-api", "icacls.exe")
    with pytest.raises(PlatformUnsupportedError):
        platform_for("linux").kernel_launcher()


class _FakeSweep:
    def pids(self) -> list[int]:
        return []

    def is_running(self) -> bool:
        return False

    def kill(self) -> None:
        return None

    def kill_pid(self, pid: int) -> bool:
        return False

    def launch(self, project: Path, *, spool_root: Path | None = None) -> None:
        return None


class _FakePlatform:
    def control_factory(self) -> Callable[[], ControlCarrier]:
        raise AssertionError("The fake is a type-checking proof, not a live carrier.")

    def broker_endpoint(self) -> BrokerEndpoint:
        return BrokerEndpoint("127.0.0.1", 1234, "a" * 32, Path("broker.json"))

    def render_command(self, project: Path) -> Sequence[str]:
        return ("fl", "-R", str(project))

    def fl_install(self) -> FLInstall:
        return FLInstall(Path("FL"), (), Path("data"))

    def sweep_fl(self) -> ProcessSweep:
        return _FakeSweep()

    def kernel_launcher(self) -> Launcher:
        return PosixLauncher()


def test_fake_platform_satisfies_the_protocol():
    fake: Platform = _FakePlatform()
    assert isinstance(fake, Platform)
    assert fake.render_command(Path("song.flp")) == ("fl", "-R", "song.flp")


@pytest.mark.platform("darwin")
def test_darwin_platform_marker_runs_only_on_darwin():
    assert isinstance(platform_for("darwin"), MacPlatform)


@pytest.mark.platform("win32")
def test_windows_platform_marker_runs_only_on_windows():
    assert isinstance(platform_for("win32"), WindowsPlatform)


def test_windows_render_command_is_documented_cli_argv(monkeypatch, tmp_path):
    from fl_mcp.fl import paths

    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("FL_APP", r"C:\\FL Studio\\FL64.exe")
    project = tmp_path / "song.flp"
    assert WindowsPlatform().render_command(project) == (
        r"C:\\FL Studio\\FL64.exe",
        "/R",
        "/Ewav",
        f"/F{tmp_path}",
        str(project),
    )


def test_windows_sweep_kills_only_the_project_marked_fl_tree(monkeypatch, tmp_path):
    import subprocess

    import fl_mcp.platform.runtime as runtime

    project = tmp_path / "ours.flp"
    calls: list[list[str]] = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[0] == "powershell":
            return subprocess.CompletedProcess(
                args,
                0,
                '[{"ProcessId":11,"CommandLine":"FL64.exe '
                + str(project)
                + '"},{"ProcessId":12,"CommandLine":"FL64.exe other.flp"}]',
            )
        return subprocess.CompletedProcess(args, 0, "")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(runtime.process, "launch_fl", lambda path, spool_root=None: 11)
    sweep = WindowsPlatform().sweep_fl()
    assert sweep.launch(project) == 11
    sweep.kill()
    assert ["taskkill", "/PID", "11", "/F", "/T"] in calls
    assert ["taskkill", "/PID", "12", "/F", "/T"] not in calls
