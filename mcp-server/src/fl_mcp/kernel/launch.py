"""The kernel's workspace and how its process is launched.

The launcher is the security boundary, chosen by the Platform: on Windows an
AppContainer that can reach only its workspace (kernel.windows), on macOS a
Seatbelt sandbox with the same reach (kernel.seatbelt). ``PosixLauncher`` is
a plain subprocess that confines nothing: tests use it, and a macOS developer
gets it only by opting out (docs/38).
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, ClassVar, Protocol

from fl_mcp import errors

# Server environment the kernel needs to run programs and reach the network;
# everything else (credentials, FLMCP_* session wiring) stays with the server.
_INHERITED_ENV = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "LANG",
    "LC_ALL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


# Holds the sitecustomize that applies kernel.private_dirs in pip and !python.
KERNEL_SITE = Path(__file__).resolve().parent / "site"
_RENDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}\.wav", re.IGNORECASE)
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(10)), *(f"LPT{n}" for n in range(10))}
)


@dataclass(frozen=True)
class Workspace:
    """One session's kernel directories under the root the host provides.

    ``work`` is the kernel's cwd and home and the only place it may write.
    ``renders`` is written by the server (render outputs) and only read by the
    kernel, so the kernel cannot plant a link there that redirects a server
    write. ``canary`` sits outside both for the boundary probe.

    Every file the server writes for the kernel lands directly in ``renders``
    (``output_path``, ``fresh_render``), never in a folder made inside it: on
    Windows the kernel reads a render only through the grant it inherits from
    ``renders``, and a private folder in between (``tempfile.mkdtemp`` is mode
    0o700, an owner-only ACL since Python 3.12.4) would cut that inheritance.
    """

    root: Path

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def renders(self) -> Path:
        return self.root / "renders"

    @property
    def packages(self) -> Path:
        return self.work / ".packages"

    @property
    def tmp(self) -> Path:
        return self.work / ".tmp"

    @property
    def canary(self) -> Path:
        return self.root / "boundary-canary"

    @classmethod
    def create(cls, root: Path) -> Workspace:
        workspace = cls(root.resolve())
        for directory in (workspace.work, workspace.renders, workspace.packages, workspace.tmp):
            directory.mkdir(parents=True, exist_ok=True)
        workspace.canary.write_text("the kernel must not be able to read this file\n", encoding="utf-8")
        return workspace

    def output_path(self, requested: Path) -> Path:
        """Where a server-side write the kernel asked for lands: its file name in renders.

        The name must be a plain ``.wav`` file name, which also rules out Windows
        device names (``NUL.wav``) and alternate data streams (``a:b.wav``).
        """
        name = requested.name
        if not _RENDER_NAME.fullmatch(name) or name.split(".")[0].upper() in _WINDOWS_DEVICES:
            raise errors.invalid_argument(
                "fl.render", f"{str(requested)!r}: use a plain file name like 'mix.wav'"
            )
        return self.renders / name

    def fresh_render(self) -> Path:
        """A new file name in renders for a render the server starts itself."""
        return self.renders / f"render-{uuid.uuid4().hex[:12]}.wav"

    def kernel_env(self, environ: Mapping[str, str], python: Path) -> dict[str, str]:
        """The kernel's whole environment: every per-user location is in the workspace."""
        env = {name: environ[name] for name in _INHERITED_ENV if name in environ}
        # `!pip` and `!python` mean the engine's interpreter.
        env["PATH"] = os.pathsep.join(filter(None, (str(python.parent), env.get("PATH"))))
        # The server never touches a path inside work after create (a planted file
        # could break or redirect it): the processes recreate .tmp themselves.
        home, tmp = self.work, self.tmp
        env.update(
            HOME=str(home),
            USERPROFILE=str(home),
            APPDATA=str(home / ".appdata"),
            LOCALAPPDATA=str(home / ".appdata"),
            TMPDIR=str(tmp),
            TMP=str(tmp),
            TEMP=str(tmp),
            PIP_TARGET=str(self.packages),
            PIP_CACHE_DIR=str(home / ".cache" / "pip"),
            PIP_DISABLE_PIP_VERSION_CHECK="1",
            # macOS: the cache /usr/bin/git and the other developer-tool shims keep.
            xcrun_db=str(home / ".cache" / "xcrun_db"),
            # Every Python the kernel starts imports KERNEL_SITE's sitecustomize.
            PYTHONPATH=str(KERNEL_SITE),
        )
        return env


@dataclass(frozen=True)
class JobLimits:
    """What one launched process tree may use; enforced on Windows by its job object."""

    memory_bytes: int
    processes: int


# The run kernel shares the box with FL's real-time audio: its whole tree (pip,
# !cmd) is capped in memory and process count.
KERNEL_JOB = JobLimits(memory_bytes=4 << 30, processes=32)


class KernelProcess(Protocol):
    """What the host needs from a launched kernel; subprocess.Popen satisfies it."""

    stdin: IO[bytes]
    stdout: IO[bytes]
    stderr: IO[bytes]
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class Launcher(Protocol):
    # Whether the OS confines this kernel to its workspace. A confined kernel
    # must pass the boundary probe before it runs any cell.
    confined: ClassVar[bool]

    def __call__(
        self,
        argv: Sequence[str],
        *,
        workspace: Workspace,
        env: dict[str, str],
        limits: JobLimits = KERNEL_JOB,
    ) -> KernelProcess: ...

    def release(self, workspace: Workspace) -> None:
        """Withdraw what launching granted on ``workspace``; its session is over."""
        ...


class _GroupPopen(subprocess.Popen[bytes]):
    """A kernel in its own session; kill takes its subprocesses (pip, !cmd) with it."""

    def kill(self) -> None:
        # Gone already: Linux says ESRCH; macOS says EPERM when only the
        # unreaped leader is left.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, signal.SIGKILL)


def spawn_group(argv: Sequence[str], *, workspace: Workspace, env: dict[str, str]) -> KernelProcess:
    """A POSIX process in its own session, in the workspace, wired by its stdio pipes."""
    return _GroupPopen(
        list(argv),
        cwd=workspace.work,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


class PosixLauncher:
    """A plain subprocess: no OS boundary and no job limits (docs/38)."""

    confined: ClassVar[bool] = False

    def __call__(
        self,
        argv: Sequence[str],
        *,
        workspace: Workspace,
        env: dict[str, str],
        limits: JobLimits = KERNEL_JOB,
    ) -> KernelProcess:
        return spawn_group(argv, workspace=workspace, env=env)

    def release(self, workspace: Workspace) -> None:
        return None  # nothing was granted
