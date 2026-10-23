"""The server side of the workspace tools: one confined process per call (docs/39).

Each call starts ``python -I -m fl_mcp.workspace.op`` through the run
kernel's launcher, so it gets the same OS boundary, workspace and built
environment as the kernel, in its own job. The process tree is stopped when
the call returns, so nothing a call starts outlives it. A confined launcher
must pass the boundary probe once before any call runs; if it fails, every
call is refused and nothing runs unconfined.

The server never opens a path in the workspace itself: everything that
touches ``work`` runs inside the confined process, so a link the model plants
there cannot turn a server read or write into one somewhere else. What the
op process answers is untrusted data: bounded in size and validated.
"""

from __future__ import annotations

import collections
import contextlib
import itertools
import json
import logging
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from pydantic import BaseModel, ValidationError

from fl_mcp import errors
from fl_mcp.errors import ToolError
from fl_mcp.kernel.launch import JobLimits, KernelProcess, Launcher, Workspace
from fl_mcp.kernel.wire import BoundaryProbe

log = logging.getLogger(__name__)

# A workspace call's tree. The kernel's job is separate (4 GiB, 32), so both
# together stay bounded beside FL.
TOOL_JOB = JobLimits(memory_bytes=2 << 30, processes=16)
# A file operation that runs this long is a pathological search or file.
FILE_OP_TIMEOUT_SECONDS = 60.0
# Interpreter start plus the probe's one-second metadata connect; generous for a cold Windows box.
PROBE_TIMEOUT_SECONDS = 60.0
# shell enforces the command's own timeout inside the op; this backstop covers
# the op itself (its start, its answer) and is past the command's timeout.
SHELL_GRACE_SECONDS = 15.0
# An answer past this is a broken op, never a result (results are about 20,000 characters).
_MAX_ANSWER_BYTES = 4 << 20
_STDERR_TAIL_LINES = 20
_DRAIN_SECONDS = 2.0


class OpTimeout(Exception):
    """The op process ran past its call's deadline and was stopped; its answer is lost."""


class _Failure(BaseModel):
    message: str
    fix: str


class _Answer(BaseModel):
    ok: dict[str, Any] | None = None
    error: _Failure | None = None


class WorkspaceRunner:
    """Runs workspace operations for one session, each in a fresh confined process."""

    def __init__(
        self,
        workspace: Workspace,
        launcher: Launcher,
        environ: Mapping[str, str],
        *,
        probe_timeout: float = PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace = workspace
        self._launcher = launcher
        self._environ = environ
        self._probe_timeout = probe_timeout
        self._verified = not launcher.confined
        self._breach: str | None = None
        self._probe_lock = threading.Lock()
        self._live_lock = threading.Lock()
        self._live: set[KernelProcess] = set()
        self._closed = False
        self._shell_numbers = itertools.count(1)

    @property
    def confined(self) -> bool:
        """Whether the OS confines these calls to the workspace (False on dev hosts)."""
        return self._launcher.confined

    def next_shell_number(self) -> int:
        """The number in a shell call's full-output log name (.logs/shell-<n>.txt)."""
        return next(self._shell_numbers)

    def call(self, op: str, args: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        """Run one operation; its result dict, or ToolError, or OpTimeout past ``timeout``."""
        self._verify_boundary()
        return self._run(op, args, timeout)

    def close(self) -> None:
        """Stop every call still running and withdraw the workspace grants; later calls are refused."""
        with self._live_lock:
            self._closed = True
            live = list(self._live)
        for process in live:
            with contextlib.suppress(OSError):
                process.kill()
        self._launcher.release(self.workspace)

    # -- internals ------------------------------------------------------------------

    def _verify_boundary(self) -> None:
        with self._probe_lock:
            if self._verified:
                return
            if self._breach is not None:
                raise errors.workspace_unavailable(self._breach, retryable=False)
            try:
                found = BoundaryProbe.model_validate(self._run("probe", {}, self._probe_timeout))
            except OpTimeout as exc:
                raise errors.workspace_unavailable("the boundary check did not finish in time.") from exc
            except ValidationError as exc:
                raise errors.workspace_unavailable("the boundary check answered nonsense.") from exc
            if found.canary_readable or found.imds_reachable:
                log.error("workspace boundary probe failed: %s", found)
                self._breach = (
                    f"its OS boundary is not in place ({found}); refusing to run anything unconfined."
                )
                raise errors.workspace_unavailable(self._breach, retryable=False)
            self._verified = True

    def _run(self, op: str, args: dict[str, Any], timeout: float) -> dict[str, Any]:
        argv = [sys.executable, "-I", "-m", "fl_mcp.workspace.op"]
        env = self.workspace.kernel_env(self._environ, python=Path(sys.executable))
        with self._live_lock:
            if self._closed:
                raise errors.workspace_unavailable("the session is closing.", retryable=False)
            try:
                process = self._launcher(argv, workspace=self.workspace, env=env, limits=TOOL_JOB)
            except (OSError, subprocess.SubprocessError) as exc:
                log.exception("workspace op launch failed")
                raise errors.workspace_unavailable(f"launch failed: {exc}") from exc
            self._live.add(process)
        try:
            return self._exchange(process, op, args, timeout)
        finally:
            # Stop whatever the call left running (its job on Windows, its process group elsewhere).
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(OSError):
                process.wait()
            with self._live_lock:
                self._live.discard(process)

    def _exchange(
        self, process: KernelProcess, op: str, args: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        request = json.dumps({"op": op, "root": str(self.workspace.root), "args": args}).encode("utf-8")
        answer: list[bytes] = []
        stderr_tail: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL_LINES)
        threading.Thread(target=_write_all, args=(process.stdin, request), daemon=True).start()
        reader = threading.Thread(target=_read_answer, args=(process.stdout, answer), daemon=True)
        reader.start()
        threading.Thread(target=_read_lines, args=(process.stderr, stderr_tail), daemon=True).start()
        try:
            code = process.wait(timeout)
        except subprocess.TimeoutExpired as exc:
            log.warning("workspace %s ran past %g s and was stopped", op, timeout)
            raise OpTimeout(op) from exc
        reader.join(_DRAIN_SECONDS)
        data = answer[0] if answer else b""
        try:
            parsed = _Answer.model_validate_json(data) if len(data) <= _MAX_ANSWER_BYTES else None
        except ValidationError:
            parsed = None
        if parsed is not None and parsed.error is not None:
            raise ToolError("invalid_argument", parsed.error.message, parsed.error.fix)
        if parsed is None or parsed.ok is None:
            log.error("workspace %s exited %s without an answer: %s", op, code, "\n".join(stderr_tail))
            raise ToolError(
                "internal_error",
                f"{op} stopped without an answer (exit code {code}).",
                "Retry once. If it fails again, do it another way and report it.",
            )
        return parsed.ok


def _write_all(stream: IO[bytes], data: bytes) -> None:
    with contextlib.suppress(OSError, ValueError):
        stream.write(data)
        stream.close()


def _read_answer(stream: IO[bytes], into: list[bytes]) -> None:
    with contextlib.suppress(OSError, ValueError):
        into.append(stream.read(_MAX_ANSWER_BYTES + 1))


def _read_lines(stream: IO[bytes], into: collections.deque[str]) -> None:
    with contextlib.suppress(OSError, ValueError):
        while raw := stream.readline(4096):
            into.append(raw.decode("utf-8", "replace").rstrip())
