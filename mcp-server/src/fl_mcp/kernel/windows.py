"""Windows kernel launch: an AppContainer process, the run kernel's OS boundary.

An AppContainer (LowBox) token denies every securable object that does not
grant its SID, so the kernel reaches only what is granted: its workspace
(read/write), the renders folder (read), and the Python runtime the box grants
(docs/38). It has no loopback and, with only the internetClient capability, no
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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, ClassVar, Protocol

from fl_mcp.kernel.launch import Workspace

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
# The kernel shares the box with FL's real-time audio: it runs below normal
# priority, its whole tree (pip, !cmd) is capped in memory and process count,
# it dies when the job handle closes, and a crash never parks it behind an
# error-reporting dialog on the interactive FL desktop.
KERNEL_JOB_MEMORY_BYTES = 4 << 30
KERNEL_JOB_PROCESSES = 32


class Win32(Protocol):
    def appcontainer_sid(self, name: str) -> str:
        """Create the AppContainer profile if missing; return its SID string."""
        ...

    def grant(self, path: Path, sid: str, rights: str) -> None: ...

    def revoke(self, path: Path, sid: str) -> None: ...

    def inheritable_handle(self, fd: int) -> int: ...

    def create_job(self) -> int: ...

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
        if self._exit_code is None:
            self._api.terminate_job(self._job)

    def _reap(self) -> None:
        self._exit_code = self._api.exit_code(self._process)
        self._api.terminate_job(self._job)  # the kernel is gone; take any orphaned child with it
        self._api.close(self._process)
        self._api.close(self._job)


@dataclass(frozen=True)
class AppContainerLauncher:
    api: Win32
    confined: ClassVar[bool] = True

    def __call__(
        self, argv: Sequence[str], *, workspace: Workspace, env: dict[str, str]
    ) -> Win32KernelProcess:
        sid = self.api.appcontainer_sid(APPCONTAINER_NAME)
        self.api.grant(workspace.work, sid, WORK_GRANT)
        self.api.grant(workspace.renders, sid, RENDERS_GRANT)
        job = self.api.create_job()
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

    def release(self, workspace: Workspace) -> None:
        """Every session's kernel shares the one SID, so a finished session's grants go."""
        sid = self.api.appcontainer_sid(APPCONTAINER_NAME)
        self.api.revoke(workspace.work, sid)
        self.api.revoke(workspace.renders, sid)


def job_limits() -> _ExtendedLimitInformation:
    limits = _ExtendedLimitInformation()
    basic = limits.BasicLimitInformation
    basic.LimitFlags = (
        _LIMIT_KILL_ON_JOB_CLOSE
        | _LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        | _LIMIT_JOB_MEMORY
        | _LIMIT_ACTIVE_PROCESS
        | _LIMIT_PRIORITY_CLASS
    )
    basic.ActiveProcessLimit = KERNEL_JOB_PROCESSES
    basic.PriorityClass = _BELOW_NORMAL_PRIORITY_CLASS
    limits.JobMemoryLimit = KERNEL_JOB_MEMORY_BYTES
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

    def create_job(self) -> int:
        job = self._k32.CreateJobObjectW(None, None)
        self._check(job, "CreateJobObjectW")
        limits = job_limits()
        self._check(
            self._k32.SetInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
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
