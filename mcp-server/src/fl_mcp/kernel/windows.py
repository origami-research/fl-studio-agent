"""Windows kernel launch: an AppContainer process, the run kernel's OS boundary.

An AppContainer (LowBox) token denies every securable object that does not
grant its SID, so the kernel reaches only what is granted: its workspace
(read/write), the renders folder (read), and the engine's own Python runtime
(read), which a box grants at provisioning and a user's own install gets from
the launcher on first use (docs/38). It has no loopback and, with only the internetClient capability, no
private-network access; the box adds a firewall rule for the metadata address.
It needs no account or password. The wire is the inherited stdio pipe trio, so
no named-pipe ACL or loopback exemption is involved. A job object kills the
kernel's whole tree on kill or when the server exits.

``AppContainerLauncher`` is the orchestration; ``CtypesWin32`` the real API
calls, injected so the orchestration is tested off Windows.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, ClassVar, Protocol

from fl_mcp.kernel.launch import KERNEL_JOB, JobLimits, Workspace

if sys.platform == "win32":
    import msvcrt

# The AppContainer profile name. Its SID is derived from this name alone, so
# the box computes the same SID with `fl-mcp-kernel-sid`.
APPCONTAINER_NAME = "FLMCP.Kernel"
# internetClient: outbound internet (pip). Deliberately not
# privateNetworkClientServer, so the VPC and the host's own services stay unreachable.
INTERNET_CLIENT_SID = "S-1-15-3-1"
# icacls grants; (OI)(CI) makes files created later inherit them.
WORK_GRANT = "(OI)(CI)M"
RENDERS_GRANT = "(OI)(CI)RX"
RUNTIME_GRANT = "(OI)(CI)RX"
# Every AppContainer token carries this group: Program Files grants it read and execute.
ALL_APPLICATION_PACKAGES_SID = "S-1-15-2-1"

_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0
_STILL_ACTIVE = 259
_ERROR_ALREADY_EXISTS_HRESULT = 0x800700B7
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESTDHANDLES = 0x00000100
_SE_GROUP_ENABLED = 0x00000004
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WAIT_TIMEOUT = 0x00000102
_LIMIT_ACTIVE_PROCESS = 0x00000008
_LIMIT_PRIORITY_CLASS = 0x00000020
_LIMIT_JOB_MEMORY = 0x00000200
_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_ACCESS_ALLOWED_ACE_TYPE = 0
_INHERIT_ONLY_ACE = 0x08
_FILE_READ_EXECUTE = 0x001200A9
_GENERIC_ALL = 0x10000000
_GENERIC_EXECUTE = 0x20000000
_GENERIC_READ = 0x80000000
# Every launched tree shares the box with FL's real-time audio: it runs below
# normal priority, is capped by its JobLimits, dies when the job handle closes,
# and a crash never parks it behind an error-reporting dialog on the
# interactive FL desktop.


class Win32(Protocol):
    def appcontainer_sid(self, name: str) -> str:
        """Create the AppContainer profile if missing; return its SID string."""
        ...

    def readable(self, path: Path, sid: str) -> bool:
        """Whether ``path``'s own ACL lets the AppContainer ``sid`` read and execute it."""
        ...

    def grant(self, path: Path, sid: str, rights: str) -> None: ...

    def revoke(self, path: Path, sid: str) -> None: ...

    def inheritable_handle(self, fd: int) -> int: ...

    def create_job(self, limits: JobLimits) -> int: ...

    def create_process(
        self,
        command_line: str,
        *,
        cwd: Path,
        env_block: str,
        appcontainer_sid: str,
        capabilities: Sequence[str],
        std_handles: tuple[int, int, int],
    ) -> tuple[int, int, int]:
        """Start suspended in the AppContainer; return (process, thread, pid)."""
        ...

    def assign(self, job: int, process: int) -> None: ...

    def resume(self, thread: int) -> None: ...

    def terminate_process(self, process: int) -> None: ...

    def terminate_job(self, job: int) -> None: ...

    def wait(self, handle: int, timeout_ms: int) -> bool:
        """True when signaled, False on timeout; a failed wait raises."""
        ...

    def exit_code(self, process: int) -> int: ...

    def close(self, handle: int) -> None: ...


def runtime_dirs() -> tuple[Path, ...]:
    """The folders the kernel's interpreter reads: this venv and the base Python it points at."""
    dirs: list[Path] = []
    for prefix in (sys.prefix, sys.base_prefix):
        path = Path(prefix).resolve()
        if path not in dirs:
            dirs.append(path)
    return tuple(dirs)


def env_block(env: Mapping[str, str]) -> str:
    """CreateProcessW's environment: sorted NAME=value strings, each NUL-terminated."""
    return "".join(f"{name}={value}\0" for name, value in sorted(env.items(), key=lambda kv: kv[0].upper()))


class Win32KernelProcess:
    """The KernelProcess protocol over Win32 handles, closed once the kernel is reaped."""

    def __init__(
        self, api: Win32, *, process: int, job: int, pid: int, pipes: tuple[IO[bytes], IO[bytes], IO[bytes]]
    ) -> None:
        self._api = api
        self._process = process
        self._job = job
        self._exit_code: int | None = None
        # kill (a shutdown thread) and reap (the waiting thread) must not race on the handles.
        self._lock = threading.Lock()
        self.pid = pid
        self.stdin, self.stdout, self.stderr = pipes

    def poll(self) -> int | None:
        if self._exit_code is None and self._api.wait(self._process, 0):
            self._reap()
        return self._exit_code

    def wait(self, timeout: float | None = None) -> int:
        if self._exit_code is None:
            if not self._api.wait(self._process, _INFINITE if timeout is None else int(timeout * 1000)):
                raise subprocess.TimeoutExpired("fl_mcp.kernel.child", timeout or 0.0)
            self._reap()
        return self._exit_code or 0

    def kill(self) -> None:
        with self._lock:
            if self._exit_code is None:
                self._api.terminate_job(self._job)

    def _reap(self) -> None:
        with self._lock:
            if self._exit_code is not None:
                return
            self._exit_code = self._api.exit_code(self._process)
            self._api.terminate_job(self._job)  # the kernel is gone; take any orphaned child with it
            self._api.close(self._process)
            self._api.close(self._job)


@dataclass(frozen=True)
class AppContainerLauncher:
    api: Win32
    confined: ClassVar[bool] = True
    # Workspaces granted, and those released. icacls rewrites the ACL of every
    # file below a folder it grants, so a workspace is granted once, not on
    # every launch: the workspace tools launch one process per call. A release
    # is final, so a launch racing shutdown cannot grant a finished session again.
    _granted: set[Path] = field(default_factory=set, compare=False, repr=False)
    _released: set[Path] = field(default_factory=set, compare=False, repr=False)
    _grants_lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)
    # The engine's interpreter (runtime_dirs). A box grants it at provisioning
    # (docs/38 "Box contract"); a user's own install lives in folders that user
    # owns (uv's cache and managed Pythons, under the profile), which no
    # AppContainer can read, so the first launch grants what is not readable yet.
    runtime: tuple[Path, ...] = field(default_factory=runtime_dirs)
    _runtime_ready: set[Path] = field(default_factory=set, compare=False, repr=False)

    def __call__(
        self,
        argv: Sequence[str],
        *,
        workspace: Workspace,
        env: dict[str, str],
        limits: JobLimits = KERNEL_JOB,
    ) -> Win32KernelProcess:
        sid = self.api.appcontainer_sid(APPCONTAINER_NAME)
        with self._grants_lock:
            if workspace.root in self._released:
                raise OSError(f"{workspace.root}: its session is over and its grants were withdrawn")
            self._grant_runtime(sid)
            if workspace.root not in self._granted:
                self.api.grant(workspace.work, sid, WORK_GRANT)
                self.api.grant(workspace.renders, sid, RENDERS_GRANT)
                self._granted.add(workspace.root)
        job = self.api.create_job(limits)
        pipes = [os.pipe() for _ in range(3)]
        child_ends = (pipes[0][0], pipes[1][1], pipes[2][1])
        parent_ends = (pipes[0][1], pipes[1][0], pipes[2][0])
        process = None
        try:
            process, thread, pid = self.api.create_process(
                subprocess.list2cmdline(argv),
                cwd=workspace.work,
                env_block=env_block(env),
                appcontainer_sid=sid,
                capabilities=(INTERNET_CLIENT_SID,),
                std_handles=(
                    self.api.inheritable_handle(child_ends[0]),
                    self.api.inheritable_handle(child_ends[1]),
                    self.api.inheritable_handle(child_ends[2]),
                ),
            )
            try:
                # Suspended until it is in the job, so no child it starts can escape it.
                self.api.assign(job, process)
                self.api.resume(thread)
            finally:
                self.api.close(thread)
        except BaseException:
            if process is not None:
                self.api.terminate_process(process)
                self.api.close(process)
            self.api.close(job)
            for fd in parent_ends:
                os.close(fd)
            raise
        finally:
            for fd in child_ends:
                os.close(fd)
        streams = (
            os.fdopen(parent_ends[0], "wb"),  # buffered: a write completes in full
            os.fdopen(parent_ends[1], "rb"),
            os.fdopen(parent_ends[2], "rb"),
        )
        return Win32KernelProcess(self.api, process=process, job=job, pid=pid, pipes=streams)

    def _grant_runtime(self, sid: str) -> None:
        """Make the interpreter readable by the container, once per folder. Called under the grants lock."""
        for directory in self.runtime:
            if directory in self._runtime_ready:
                continue
            if not self.api.readable(directory, sid):
                try:
                    self.api.grant(directory, sid, RUNTIME_GRANT)
                except (OSError, subprocess.SubprocessError) as exc:
                    raise OSError(
                        f"the kernel's Python runtime {directory} is not readable by its AppContainer and "
                        f"this user cannot grant it ({exc}); grant it read and execute as docs/38 "
                        "'Box contract' says"
                    ) from exc
            self._runtime_ready.add(directory)

    def release(self, workspace: Workspace) -> None:
        """Every session's kernel shares the one SID, so a finished session's grants go."""
        sid = self.api.appcontainer_sid(APPCONTAINER_NAME)
        with self._grants_lock:
            self._released.add(workspace.root)
            self._granted.discard(workspace.root)
            try:
                self.api.revoke(workspace.work, sid)
            finally:
                self.api.revoke(workspace.renders, sid)  # attempted even when the first failed


def job_limits(job: JobLimits) -> _ExtendedLimitInformation:
    limits = _ExtendedLimitInformation()
    basic = limits.BasicLimitInformation
    basic.LimitFlags = (
        _LIMIT_KILL_ON_JOB_CLOSE
        | _LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        | _LIMIT_JOB_MEMORY
        | _LIMIT_ACTIVE_PROCESS
        | _LIMIT_PRIORITY_CLASS
    )
    basic.ActiveProcessLimit = job.processes
    basic.PriorityClass = _BELOW_NORMAL_PRIORITY_CLASS
    limits.JobMemoryLimit = job.memory_bytes
    return limits


# -- the real API (Windows only; constructed by WindowsPlatform) --------------------

_PVOID = ctypes.c_void_p


class _SidAndAttributes(ctypes.Structure):
    _fields_ = (("Sid", _PVOID), ("Attributes", ctypes.c_uint32))


class _SecurityCapabilities(ctypes.Structure):
    _fields_ = (
        ("AppContainerSid", _PVOID),
        ("Capabilities", ctypes.POINTER(_SidAndAttributes)),
        ("CapabilityCount", ctypes.c_uint32),
        ("Reserved", ctypes.c_uint32),
    )


class _StartupInfo(ctypes.Structure):
    _fields_ = (
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", _PVOID),
        ("hStdInput", _PVOID),
        ("hStdOutput", _PVOID),
        ("hStdError", _PVOID),
    )


class _StartupInfoEx(ctypes.Structure):
    _fields_ = (("StartupInfo", _StartupInfo), ("lpAttributeList", _PVOID))


class _ProcessInformation(ctypes.Structure):
    _fields_ = (
        ("hProcess", _PVOID),
        ("hThread", _PVOID),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    )


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    )


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


class _Acl(ctypes.Structure):
    _fields_ = (
        ("AclRevision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("AclSize", ctypes.c_uint16),
        ("AceCount", ctypes.c_uint16),
        ("Sbz2", ctypes.c_uint16),
    )


class _AceHeader(ctypes.Structure):
    _fields_ = (("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", ctypes.c_uint16))


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = (("Header", _AceHeader), ("Mask", ctypes.c_uint32), ("SidStart", ctypes.c_uint32))


def grants_read_execute(mask: int) -> bool:
    """Whether an allow entry's access mask covers reading and running files."""
    if mask & _GENERIC_ALL or (mask & _GENERIC_READ and mask & _GENERIC_EXECUTE):
        return True
    return mask & _FILE_READ_EXECUTE == _FILE_READ_EXECUTE


class CtypesWin32:
    """The Win32 calls behind AppContainerLauncher, bound with explicit signatures."""

    def __init__(self, icacls: Path) -> None:
        self._icacls = icacls
        dll = ctypes.WinDLL  # exists only on Windows, where WindowsPlatform constructs this class
        self._k32 = dll("kernel32", use_last_error=True)
        self._adv = dll("advapi32", use_last_error=True)
        self._env = dll("userenv", use_last_error=True)
        u32, b, hr, p = ctypes.c_uint32, ctypes.c_int, ctypes.c_long, _PVOID
        pp = ctypes.POINTER(_PVOID)
        self._bind(
            self._env,
            "CreateAppContainerProfile",
            hr,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            p,
            u32,
            pp,
        )
        self._bind(self._env, "DeriveAppContainerSidFromAppContainerName", hr, ctypes.c_wchar_p, pp)
        self._bind(self._adv, "ConvertSidToStringSidW", b, p, ctypes.POINTER(ctypes.c_wchar_p))
        self._bind(self._adv, "ConvertStringSidToSidW", b, ctypes.c_wchar_p, pp)
        self._bind(self._adv, "FreeSid", p, p)
        self._bind(self._adv, "GetNamedSecurityInfoW", u32, ctypes.c_wchar_p, ctypes.c_int, u32, pp, pp, pp, pp, pp)
        self._bind(self._adv, "GetAce", b, p, u32, pp)
        self._bind(self._adv, "EqualSid", b, p, p)
        self._bind(self._k32, "LocalFree", p, p)
        self._bind(self._k32, "CreateJobObjectW", p, p, ctypes.c_wchar_p)
        self._bind(self._k32, "SetInformationJobObject", b, p, ctypes.c_int, p, u32)
        self._bind(self._k32, "AssignProcessToJobObject", b, p, p)
        self._bind(self._k32, "TerminateJobObject", b, p, u32)
        self._bind(self._k32, "TerminateProcess", b, p, u32)
        self._bind(self._k32, "ResumeThread", u32, p)
        self._bind(self._k32, "WaitForSingleObject", u32, p, u32)
        self._bind(self._k32, "GetExitCodeProcess", b, p, ctypes.POINTER(u32))
        self._bind(self._k32, "CloseHandle", b, p)
        self._bind(
            self._k32, "InitializeProcThreadAttributeList", b, p, u32, u32, ctypes.POINTER(ctypes.c_size_t)
        )
        self._bind(
            self._k32, "UpdateProcThreadAttribute", b, p, u32, ctypes.c_size_t, p, ctypes.c_size_t, p, p
        )
        self._bind(self._k32, "DeleteProcThreadAttributeList", None, p)
        self._bind(
            self._k32, "CreateProcessW", b,
            ctypes.c_wchar_p, p, p, p, b, u32, p, ctypes.c_wchar_p,
            ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInformation),
        )  # fmt: skip

    @staticmethod
    def _bind(dll: Any, name: str, restype: Any, *argtypes: Any) -> None:
        function = getattr(dll, name)
        function.restype = restype
        function.argtypes = argtypes

    @staticmethod
    def _check(ok: object, call: str) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error(), call)

    def appcontainer_sid(self, name: str) -> str:
        sid = _PVOID()
        result = self._env.CreateAppContainerProfile(
            name, name, "FL MCP run kernel", None, 0, ctypes.byref(sid)
        )
        if result & 0xFFFFFFFF == _ERROR_ALREADY_EXISTS_HRESULT:
            return self.derive_sid(name)
        if result != 0:
            raise OSError(f"CreateAppContainerProfile failed: HRESULT {result & 0xFFFFFFFF:#010x}")
        try:
            return self._sid_string(sid)
        finally:
            self._adv.FreeSid(sid)

    def derive_sid(self, name: str) -> str:
        sid = _PVOID()
        result = self._env.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
        if result != 0:
            raise OSError(
                f"DeriveAppContainerSidFromAppContainerName failed: HRESULT {result & 0xFFFFFFFF:#010x}"
            )
        try:
            return self._sid_string(sid)
        finally:
            self._adv.FreeSid(sid)

    def _sid_string(self, sid: _PVOID) -> str:
        text = ctypes.c_wchar_p()
        self._check(self._adv.ConvertSidToStringSidW(sid, ctypes.byref(text)), "ConvertSidToStringSidW")
        try:
            return str(text.value)
        finally:
            self._k32.LocalFree(text)

    def readable(self, path: Path, sid: str) -> bool:
        """An allow entry for ``sid`` or ALL APPLICATION PACKAGES that covers ``path`` itself.

        Deny entries are not weighed: a runtime denied to the container still fails
        the kernel's start, with the launcher's error."""
        dacl, descriptor = _PVOID(), _PVOID()
        result = self._adv.GetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
        )  # fmt: skip
        if result != 0:
            raise ctypes.WinError(result, f"GetNamedSecurityInfoW {path}")
        trustees = [self._sid(sid), self._sid(ALL_APPLICATION_PACKAGES_SID)]
        try:
            if not dacl.value:
                return True  # a NULL DACL grants everyone everything
            for index in range(ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents.AceCount):
                ace = _PVOID()
                self._check(self._adv.GetAce(dacl, index, ctypes.byref(ace)), "GetAce")
                entry = ctypes.cast(ace, ctypes.POINTER(_AccessAllowedAce)).contents
                if entry.Header.AceType != _ACCESS_ALLOWED_ACE_TYPE or entry.Header.AceFlags & _INHERIT_ONLY_ACE:
                    continue
                entry_sid = (ace.value or 0) + _AccessAllowedAce.SidStart.offset
                if grants_read_execute(entry.Mask) and any(self._adv.EqualSid(entry_sid, t) for t in trustees):
                    return True
            return False
        finally:
            for trustee in trustees:
                self._k32.LocalFree(trustee)
            self._k32.LocalFree(descriptor)

    def grant(self, path: Path, sid: str, rights: str) -> None:
        subprocess.run(
            [str(self._icacls), str(path), "/grant", f"*{sid}:{rights}", "/Q"],
            check=True,
            capture_output=True,
        )

    def revoke(self, path: Path, sid: str) -> None:
        subprocess.run(
            [str(self._icacls), str(path), "/remove:g", f"*{sid}", "/Q"], check=True, capture_output=True
        )

    def inheritable_handle(self, fd: int) -> int:
        handle = msvcrt.get_osfhandle(fd)
        os.set_handle_inheritable(handle, True)
        return handle

    def create_job(self, limits: JobLimits) -> int:
        job = self._k32.CreateJobObjectW(None, None)
        self._check(job, "CreateJobObjectW")
        info = job_limits(limits)
        self._check(
            self._k32.SetInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
            ),
            "SetInformationJobObject",
        )
        return job

    def create_process(
        self,
        command_line: str,
        *,
        cwd: Path,
        env_block: str,
        appcontainer_sid: str,
        capabilities: Sequence[str],
        std_handles: tuple[int, int, int],
    ) -> tuple[int, int, int]:
        sids = [self._sid(appcontainer_sid), *(self._sid(c) for c in capabilities)]
        attributes = None
        try:
            granted = (_SidAndAttributes * len(capabilities))(
                *(_SidAndAttributes(sid, _SE_GROUP_ENABLED) for sid in sids[1:])
            )
            security = _SecurityCapabilities(sids[0], granted, len(capabilities), 0)
            handles = (_PVOID * 3)(*std_handles)
            size = ctypes.c_size_t(0)
            self._k32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            attributes = ctypes.create_string_buffer(size.value)
            self._check(
                self._k32.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)),
                "InitializeProcThreadAttributeList",
            )
            for attribute, value in (
                (_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, security),
                (_PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles),
            ):
                self._check(
                    self._k32.UpdateProcThreadAttribute(
                        attributes, 0, attribute, ctypes.byref(value), ctypes.sizeof(value), None, None
                    ),
                    "UpdateProcThreadAttribute",
                )
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
            startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = (
                std_handles
            )
            startup.lpAttributeList = ctypes.cast(attributes, _PVOID)
            info = _ProcessInformation()
            flags = (
                _CREATE_SUSPENDED
                | _CREATE_UNICODE_ENVIRONMENT
                | _EXTENDED_STARTUPINFO_PRESENT
                | _CREATE_NO_WINDOW
            )
            self._check(
                self._k32.CreateProcessW(
                    None,
                    ctypes.create_unicode_buffer(command_line),
                    None,
                    None,
                    True,
                    flags,
                    # Element-wise: the block's embedded NULs must survive the copy.
                    (ctypes.c_wchar * (len(env_block) + 1))(*env_block),
                    str(cwd),
                    ctypes.byref(startup),
                    ctypes.byref(info),
                ),
                "CreateProcessW",
            )
            return info.hProcess, info.hThread, info.dwProcessId
        finally:
            if attributes is not None:
                self._k32.DeleteProcThreadAttributeList(attributes)
            for sid in sids:
                self._k32.LocalFree(sid)

    def _sid(self, text: str) -> _PVOID:
        sid = _PVOID()
        self._check(self._adv.ConvertStringSidToSidW(text, ctypes.byref(sid)), "ConvertStringSidToSidW")
        return sid

    def assign(self, job: int, process: int) -> None:
        self._check(self._k32.AssignProcessToJobObject(job, process), "AssignProcessToJobObject")

    def resume(self, thread: int) -> None:
        self._check(self._k32.ResumeThread(thread) != _INFINITE, "ResumeThread")

    def terminate_process(self, process: int) -> None:
        self._check(self._k32.TerminateProcess(process, 1), "TerminateProcess")

    def terminate_job(self, job: int) -> None:
        self._check(self._k32.TerminateJobObject(job, 1), "TerminateJobObject")

    def wait(self, handle: int, timeout_ms: int) -> bool:
        result = self._k32.WaitForSingleObject(handle, timeout_ms)
        if result not in (_WAIT_OBJECT_0, _WAIT_TIMEOUT):
            self._check(False, "WaitForSingleObject")
        return result == _WAIT_OBJECT_0

    def exit_code(self, process: int) -> int:
        code = ctypes.c_uint32()
        self._check(self._k32.GetExitCodeProcess(process, ctypes.byref(code)), "GetExitCodeProcess")
        return code.value

    def close(self, handle: int) -> None:
        self._check(self._k32.CloseHandle(handle), "CloseHandle")


def icacls_path(environ: Mapping[str, str]) -> Path:
    return Path(environ["SYSTEMROOT"]) / "System32" / "icacls.exe"


def print_sid() -> None:
    """``fl-mcp-kernel-sid``: the kernel AppContainer SID, for box ACLs and firewall rules."""
    print(CtypesWin32(icacls_path(os.environ)).derive_sid(APPCONTAINER_NAME))
