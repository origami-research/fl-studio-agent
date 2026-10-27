"""Typed platform seam, deliberately unconnected to session behavior."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from fl_mcp.core.carriers.wire import MidiControl, windows_midi_control
from fl_mcp.fl import paths, process
from fl_mcp.kernel.launch import Launcher, PosixLauncher
from fl_mcp.kernel.seatbelt import SeatbeltLauncher
from fl_mcp.kernel.windows import AppContainerLauncher, CtypesWin32, icacls_path

# macOS only: "1" runs model code unconfined, with the developer's own access,
# for working on the engine where the sandbox gets in the way (docs/38).
ENV_KERNEL_UNCONFINED = "FLMCP_KERNEL_UNCONFINED"

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

    def kernel_launcher(self) -> Launcher:
        """How the run kernel starts; on a production OS this is its security boundary (docs/38)."""
        ...


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
        from fl_mcp.core.carriers.control_broker import default_endpoint

        return default_endpoint()

    def render_command(self, project: Path) -> Sequence[str]:
        executable = paths.fl_app() / "Contents" / "MacOS" / process.fl_executable_name()
        return (str(executable), "-R", str(project))

    def fl_install(self) -> FLInstall:
        return _existing_install()

    def sweep_fl(self) -> ProcessSweep:
        return _ExistingProcessSweep()

    def kernel_launcher(self) -> Launcher:
        value = os.environ.get(ENV_KERNEL_UNCONFINED, "0")
        if value not in ("0", "1"):
            raise ValueError(f"{ENV_KERNEL_UNCONFINED} must be 0 or 1; got {value!r}")
        return PosixLauncher() if value == "1" else SeatbeltLauncher()


class WindowsPlatform:
    """Windows adapter; helpers delegate now, host-only seams land later."""

    def control_factory(self) -> Callable[[], "ControlCarrier"]:
        return windows_midi_control

    def broker_endpoint(self) -> BrokerEndpoint:
        from fl_mcp.core.carriers.control_broker import default_endpoint

        return default_endpoint()

    def render_command(self, project: Path) -> Sequence[str]:
        # VERIFY-ON-WINDOWS: FL64.exe CLI render and output naming on a Windows host image.
        return (str(paths.fl_app()), "/R", "/Ewav", f"/F{project.parent}", str(project))

    def fl_install(self) -> FLInstall:
        return _existing_install()

    def sweep_fl(self) -> ProcessSweep:
        return _WindowsProcessSweep()

    def kernel_launcher(self) -> Launcher:
        return AppContainerLauncher(CtypesWin32(icacls_path(os.environ)))


class _WindowsProcessSweep:
    """Windows ownership is exact FL64.exe plus the launched project marker."""

    def __init__(self) -> None:
        self._project: Path | None = None

    def pids(self) -> list[int]:
        if self._project is None:
            return []
        # VERIFY-ON-WINDOWS: Get-CimInstance availability and CommandLine access
        # on the hosting account. The marker is never used without exact image name.
        script = "Get-CimInstance Win32_Process -Filter \"Name='FL64.exe'\" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=3
            )
            rows = json.loads(result.stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            return []
        if isinstance(rows, dict):
            rows = [rows]
        marker = str(self._project)
        return [
            int(row["ProcessId"])
            for row in rows
            if marker in str(row.get("CommandLine", "")) and str(row.get("ProcessId", "")).isdigit()
        ]

    def is_running(self) -> bool:
        return bool(self.pids())

    def kill(self) -> None:
        for pid in self.pids():
            self.kill_pid(pid)

    def kill_pid(self, pid: int) -> bool:
        if pid not in self.pids():
            return False
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return True

    def launch(self, project: Path, *, spool_root: Path | None = None) -> int | None:
        self._project = project
        return process.launch_fl(project, spool_root=spool_root)


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

    def kernel_launcher(self) -> Launcher:
        self._unsupported()


def platform_for(sys_platform: str) -> Platform:
    """Select the sole concrete adapter from the caller's one platform read."""
    if sys_platform == "darwin":
        return MacPlatform()
    if sys_platform == "win32":
        return WindowsPlatform()
    return UnsupportedPlatform(sys_platform)
