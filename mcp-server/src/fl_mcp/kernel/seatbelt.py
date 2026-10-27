"""macOS kernel launch: a Seatbelt sandbox, the run kernel's OS boundary.

The kernel and every workspace-tool call start under ``sandbox-exec`` with a
profile generated here. Like the Windows AppContainer (kernel.windows) it
denies by default and allows what the kernel needs:

- files: read and write its workspace (``work``), read ``renders``, read and
  run the engine's own Python (found by asking that interpreter, so a venv,
  uv's managed Pythons and an editable checkout all work) and the system's
  software folders. The user's home, other kernels' workspaces, the canary
  and everything else are denied.
- network: outbound internet (pip), but nothing on this machine: Seatbelt's
  ``localhost`` covers loopback and the machine's own addresses, so the FL
  control broker, the Bridge ports and every local service are unreachable.
  The wire to the server is the inherited stdio pipes, so it needs no rule.
  Seatbelt cannot name other address ranges, so the local network and the
  metadata address are not blocked here (docs/38).
- processes: it may start programs it can read, and see and signal only
  processes in its own sandbox; Mach services are denied except user lookup
  and certificate checks, so it cannot send Apple Events, MIDI or keychain
  requests to other apps.

Every path reaches the profile as a ``-D`` parameter, never as profile text,
so no path can change the profile's meaning.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from fl_mcp.kernel.launch import KERNEL_JOB, JobLimits, KernelProcess, Workspace, spawn_group

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")

# The operating system's own software and the folders package managers
# install software into, minus the folders where their services keep data.
SYSTEM_READABLE = (
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/Library/Frameworks",  # python.org's framework Pythons
    # The developer tools /usr/bin/git, clang and the other shims run: the
    # folder xcode-select chose (resolved through its link), else the default.
    "/var/select/developer_dir",
    "/Library/Developer/CommandLineTools",
    "/Library/Preferences/com.apple.dt.Xcode.plist",  # whether the Xcode license was accepted
    "/etc",  # hosts, certificates, localtime
    "/var/db/timezone",
    "/var/select",  # which shell /bin/sh runs
    "/opt/homebrew",
)
SYSTEM_DATA = ("/opt/homebrew/var", "/usr/local/var")
# Devices any program opens; /dev/fd is the process's own descriptors.
READ_WRITE_DEVICES = ("/dev/null", "/dev/zero", "/dev/dtracehelper")
READ_DEVICES = ("/dev/random", "/dev/urandom")
# System facts programs read (CPU, OS version, limits). Not every name: with
# sysctl-read open, any process's arguments and environment (kern.procargs2)
# were readable, and the server's environment can hold credentials.
SYSCTL_PREFIXES = ("hw.", "machdep.")
SYSCTL_NAMES = (
    "kern.argmax",
    "kern.hostname",
    "kern.maxfilesperproc",
    "kern.maxproc",
    "kern.ngroups",
    "kern.osproductversion",
    "kern.osrelease",
    "kern.ostype",
    "kern.osvariant_status",
    "kern.osversion",
    "kern.secure_kernel",
    "kern.usrstack64",
    "kern.version",
    "sysctl.proc_cputype",
)
# Mach services the kernel may reach: user and group lookup (pwd, getpass) and
# certificate verification (pip checks HTTPS with the system trust store).
MACH_SERVICES = ("com.apple.system.opendirectoryd.libinfo", "com.apple.trustd.agent")
# The system DNS resolver's socket; every other Unix socket is denied.
DNS_SOCKET = "/private/var/run/mDNSResponder"

# What the engine's interpreter reads when it starts with -I: the kernel's own view.
_RUNTIME_QUERY = "import json, sys; print(json.dumps([sys.executable, sys.prefix, sys.base_prefix, *sys.path]))"
_MAX_LINK_DEPTH = 40


@dataclass
class _Reach:
    """What resolving a set of paths passes through, as the kernel's own lookups will."""

    trees: set[Path] = field(default_factory=set)
    links: set[Path] = field(default_factory=set)
    dirs: set[Path] = field(default_factory=set)

    def add(self, path: Path) -> None:
        real = self._walk(path, 0)
        if real.exists():
            # A tool inside an app bundle reads the bundle's Info.plist (git in Xcode does).
            self.trees.add(next((p for p in real.parents if p.suffix == ".app"), real))

    def roots(self) -> set[Path]:
        """The trees not already inside another one."""
        return {tree for tree in self.trees if not any(other in tree.parents for other in self.trees)}

    def _walk(self, path: Path, depth: int) -> Path:
        """The real path, recording every directory and symlink on the way to it."""
        if depth > _MAX_LINK_DEPTH:
            raise OSError(f"{path}: too many levels of symbolic links")
        current = Path(path.anchor or "/")
        for part in path.parts[1:]:
            if part == "..":
                current = current.parent
                continue
            self.dirs.add(current)
            candidate = current / part
            if candidate.is_symlink():
                self.links.add(candidate)
                target = candidate.readlink()
                current = self._walk(target if target.is_absolute() else current / target, depth + 1)
            else:
                current = candidate
        return current


@dataclass(frozen=True)
class Profile:
    """A Seatbelt profile and the parameters its paths arrive in."""

    text: str
    params: tuple[tuple[str, str], ...]

    def command(self, argv: Sequence[str]) -> list[str]:
        defines = [arg for name, value in self.params for arg in ("-D", f"{name}={value}")]
        return [str(SANDBOX_EXEC), *defines, "-p", self.text, "--", *argv]


class _Params:
    def __init__(self) -> None:
        self._names: dict[str, str] = {}

    def ref(self, path: Path | str) -> str:
        name = self._names.setdefault(str(path), f"P{len(self._names)}")
        return f'(param "{name}")'

    def literals(self, paths: Iterable[Path | str]) -> str:
        return " ".join(f"(literal {self.ref(p)})" for p in sorted(map(str, paths)))

    def subpaths(self, paths: Iterable[Path | str]) -> str:
        return " ".join(f"(subpath {self.ref(p)})" for p in sorted(map(str, paths)))

    def items(self) -> tuple[tuple[str, str], ...]:
        return tuple((name, path) for path, name in self._names.items())


def build_profile(workspace: Workspace, runtime: Iterable[Path]) -> Profile:
    """The kernel's profile: deny by default, then exactly the access the module docstring lists."""
    readable = _Reach()
    for path in (*map(Path, SYSTEM_READABLE), *runtime):
        readable.add(path)
    own = _Reach()
    own.add(workspace.work)
    own.add(workspace.renders)
    p = _Params()
    work = p.subpaths([workspace.work])
    programs = f"{p.subpaths(readable.roots())} {work}"
    devices = " ".join(f'(literal "{d}")' for d in READ_WRITE_DEVICES)
    sysctls = " ".join(
        [*(f'(sysctl-name-prefix "{x}")' for x in SYSCTL_PREFIXES), *(f'(sysctl-name "{x}")' for x in SYSCTL_NAMES)]
    )
    text = f"""(version 1)
; Seatbelt applies the last rule that matches, so each deny follows the allow it narrows.
(deny default)

; Processes: start what it can read; see and signal only its own sandbox.
; Reading another process's arguments and environment needs both this deny
; and the short sysctl list below; either alone let it through.
(allow process-fork)
(allow process-exec {programs})
(deny process-info*)
(allow process-info* (target same-sandbox))
(allow signal (target same-sandbox))

; Files: the system, the engine's Python and its workspace; write only the workspace.
(allow file-read* (literal "/") {programs} {p.subpaths([workspace.renders])})
(allow file-read-metadata {p.literals(readable.links | readable.dirs | own.links | own.dirs)})
(allow file-write* {work})
(allow file-read* file-write-data {devices} (subpath "/dev/fd"))
(allow file-read* {" ".join(f'(literal "{d}")' for d in READ_DEVICES)})
(allow file-read-metadata (literal "/dev"))
(deny file-read* process-exec {p.subpaths(SYSTEM_DATA)})

; System services.
(allow sysctl-read {sysctls})
(allow ipc-posix-sem) ; multiprocessing's locks and queues
(allow mach-lookup {" ".join(f'(global-name "{s}")' for s in MACH_SERVICES)})

; Network: the internet, never this machine; DNS through the system resolver.
(allow network-outbound (remote ip "*:*"))
(deny network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket (path-literal "{DNS_SOCKET}")))
"""
    return Profile(text=text, params=p.items())


def runtime_paths(python: Path = Path(sys.executable)) -> tuple[Path, ...]:
    """What the engine's interpreter reads at startup: itself, its venv, its base Python, its sys.path."""
    out = subprocess.run(
        [str(python), "-I", "-c", _RUNTIME_QUERY], capture_output=True, text=True, check=True, timeout=60
    ).stdout
    return tuple(Path(entry) for entry in json.loads(out) if entry)


@dataclass
class SeatbeltLauncher:
    """macOS: every kernel and workspace call starts in its own Seatbelt sandbox.

    No job limits: macOS has no per-tree memory or process cap a user can set.
    The tree is the process group, killed whole like PosixLauncher's.
    """

    confined: ClassVar[bool] = True
    _runtime: tuple[Path, ...] | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __call__(
        self,
        argv: Sequence[str],
        *,
        workspace: Workspace,
        env: dict[str, str],
        limits: JobLimits = KERNEL_JOB,
    ) -> KernelProcess:
        if not SANDBOX_EXEC.is_file():
            raise OSError(f"{SANDBOX_EXEC} is missing, so model code cannot be confined on this Mac")
        profile = build_profile(workspace, self._runtime_paths())
        return spawn_group(profile.command(argv), workspace=workspace, env=env)

    def _runtime_paths(self) -> tuple[Path, ...]:
        with self._lock:
            if self._runtime is None:
                self._runtime = runtime_paths()
            return self._runtime

    def release(self, workspace: Workspace) -> None:
        return None  # each sandbox ends with its process; nothing was granted
