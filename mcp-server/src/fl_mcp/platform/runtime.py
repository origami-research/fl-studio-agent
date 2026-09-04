"""Typed platform seam, deliberately unconnected to session behavior."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from fl_mcp.core.carriers.wire import MidiControl
from fl_mcp.fl import paths, process

if TYPE_CHECKING:
    from fl_mcp.session.link import ControlCarrier


class PlatformUnsupportedError(RuntimeError):
    """Raised when an OS-specific FL operation has no supported adapter."""


@dataclass(frozen=True)
class BrokerEndpoint:
    """One authenticated loopback-TCP broker endpoint, shared by every OS."""

    host: str
    port: int
    token: str
    port_file: Path


@dataclass(frozen=True)
class FLInstall:
    """Authoritative FL locations known to a concrete platform adapter."""

    executable: Path
    settings_dirs: tuple[Path, ...]
    app_data_root: Path
    templates_dir: Path | None = None
    data_dir: Path | None = None
    native_plugin_dir: Path | None = None
    engine_image: Path | None = None


class ProcessSweep(Protocol):
    """Exact-image FL process operations owned by one platform adapter."""

    def pids(self) -> list[int]: ...

    def is_running(self) -> bool: ...

    def kill(self) -> None: ...

    def kill_pid(self, pid: int) -> bool: ...

    def launch(self, project: Path, *, spool_root: Path | None = None) -> int | None: ...


@runtime_checkable
class Platform(Protocol):
    """OS-owned capabilities needed by the runtime boundary."""

    def control_factory(self) -> Callable[[], "ControlCarrier"]: ...

    def broker_endpoint(self) -> BrokerEndpoint: ...

    def render_command(self, project: Path) -> Sequence[str]: ...

    def fl_install(self) -> FLInstall: ...

    def sweep_fl(self) -> ProcessSweep: ...


@dataclass(frozen=True)
class _ExistingProcessSweep:
    """Compatibility-free adapter over the current canonical process helpers."""

    def pids(self) -> list[int]:
        return process.fl_pids()

    def is_running(self) -> bool:
        return process.fl_process_running()

    def kill(self) -> None:
        process.fl_kill()

    def kill_pid(self, pid: int) -> bool:
        return process.fl_kill_pid(pid)

    def launch(self, project: Path, *, spool_root: Path | None = None) -> int | None:
        return process.launch_fl(project, spool_root=spool_root)


def _existing_install() -> FLInstall:
    return FLInstall(
        executable=paths.fl_app(),
        settings_dirs=tuple(paths.fl_settings_dirs()),
        app_data_root=paths.app_data_root(),
    )


class MacPlatform:
    """macOS adapter backed by the existing, still-unmoved helpers."""

    def control_factory(self) -> Callable[[], "ControlCarrier"]:
        return MidiControl

    def broker_endpoint(self) -> BrokerEndpoint:
        raise NotImplementedError("Loopback-TCP broker endpoint is not wired into the macOS broker yet.")

    def render_command(self, project: Path) -> Sequence[str]:
        executable = paths.fl_app() / "Contents" / "MacOS" / process.fl_executable_name()
        return (str(executable), "-R", str(project))

    def fl_install(self) -> FLInstall:
        return _existing_install()

    def sweep_fl(self) -> ProcessSweep:
        return _ExistingProcessSweep()


class WindowsPlatform:
    """Windows adapter; helpers delegate now, fleet-only seams land later."""

    def control_factory(self) -> Callable[[], "ControlCarrier"]:
        raise NotImplementedError("Windows MIDI requires provisioned loopMIDI ports and is not wired yet.")

    def broker_endpoint(self) -> BrokerEndpoint:
        raise NotImplementedError("Loopback-TCP broker endpoint is not wired into the Windows broker yet.")

    def render_command(self, project: Path) -> Sequence[str]:
        raise NotImplementedError("Windows CLI render argv is VERIFY-ON-WINDOWS and is not enabled yet.")

    def fl_install(self) -> FLInstall:
        return _existing_install()

    def sweep_fl(self) -> ProcessSweep:
        return _ExistingProcessSweep()


class UnsupportedPlatform:
    """A fail-closed adapter that names the unsupported platform and remedy."""

    def __init__(self, sys_platform: str) -> None:
        self.sys_platform = sys_platform

    def _unsupported(self) -> None:
        raise PlatformUnsupportedError(
            f"FL MCP does not support platform {self.sys_platform!r}; use darwin or win32."
        )

    def control_factory(self) -> Callable[[], "ControlCarrier"]:
        self._unsupported()

    def broker_endpoint(self) -> BrokerEndpoint:
        self._unsupported()

    def render_command(self, project: Path) -> Sequence[str]:
        self._unsupported()

    def fl_install(self) -> FLInstall:
        self._unsupported()

    def sweep_fl(self) -> ProcessSweep:
        self._unsupported()


def platform_for(sys_platform: str) -> Platform:
    """Select the sole concrete adapter from the caller's one platform read."""
    if sys_platform == "darwin":
        return MacPlatform()
    if sys_platform == "win32":
        return WindowsPlatform()
    return UnsupportedPlatform(sys_platform)
