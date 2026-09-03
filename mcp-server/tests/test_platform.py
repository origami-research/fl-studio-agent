"""Step-1 proofs for the unconnected platform boundary (docs/35)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

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


def test_no_production_module_imports_the_unconnected_platform_package():
    source_root = Path(__file__).parents[1] / "src" / "fl_mcp"
    offenders = [
        path
        for path in source_root.rglob("*.py")
        if path.parent.name != "platform" and "fl_mcp.platform" in path.read_text()
    ]
    assert offenders == [], "platform imports would change behavior before a later seam step"
