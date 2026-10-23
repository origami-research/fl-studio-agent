"""The Win32 calls behind AppContainerLauncher, recorded instead of made.

Real Windows verification is a box run (docs/38, docs/39); these fakes pin what
the orchestration asks Windows for.
"""

from __future__ import annotations

SID = "S-1-15-2-1-2-3-4-5-6-7"


class FakeWin32:
    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail = fail
        self.alive = True

    def _record(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name == self.fail:
            raise OSError(f"{name} failed")

    def appcontainer_sid(self, name):
        self._record("appcontainer_sid", name)
        return SID

    def grant(self, path, sid, rights):
        self._record("grant", path, sid, rights)

    def revoke(self, path, sid):
        self._record("revoke", path, sid)

    def inheritable_handle(self, fd):
        return 1000 + fd

    def create_job(self, limits):
        self._record("create_job", limits)
        return 7

    def create_process(self, command_line, *, cwd, env_block, appcontainer_sid, capabilities, std_handles):
        self.request = {
            "command_line": command_line,
            "cwd": cwd,
            "env_block": env_block,
            "appcontainer_sid": appcontainer_sid,
            "capabilities": tuple(capabilities),
            "std_handles": std_handles,
        }
        self._record("create_process")
        return 11, 12, 4242

    def assign(self, job, process):
        self._record("assign", job, process)

    def resume(self, thread):
        self._record("resume", thread)

    def terminate_process(self, process):
        self._record("terminate_process", process)

    def terminate_job(self, job):
        self._record("terminate_job", job)
        self.alive = False

    def wait(self, handle, timeout_ms):
        return not self.alive

    def exit_code(self, process):
        return 1

    def close(self, handle):
        self._record("close", handle)
