"""Per-session control over the controller's FILE lane, addressed by pid.

The MIDI wire cannot serve two FL instances: its virtual port names are fixed
constants, so concurrent sessions cross-wire nondeterministically (#150). The
controller script's ctl file lane has no such identity problem - every FL
instance polls the SAME shared spool - so per-session control is an
ADDRESSING fix, not a transport rewrite: each command carries the target FL's
pid, and a controller executes only its own (device_FLMCP.py leaves foreign
commands untouched).

The target pid is never guessed: it is read per round trip from the session's
``bridge_udp.json`` (the bridge's port file), the one identity that survives
FL's boot-time re-exec. Request ids are namespaced by this server's pid so
two fl-mcp servers sharing the spool cannot collide on response filenames.

Latency rides OnIdle (the same clock the wire's engine calls already park on),
so the wire's timeout semantics carry over unchanged. What this lane does NOT
have is push: hello/dirty/refresh events never arrive on their own, so
``drain_events`` is honestly empty and observation features degrade in
session mode until the bridge event stream covers them.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from itertools import count
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from fl_mcp.errors import ToolError, control_plane_unavailable
from fl_mcp.fl.paths import SPOOL_SUBDIRS, fl_settings_dirs
from fl_mcp.schemas import ControlRequest, ControlResponse, HelloEvent

CMD_PREFIX = "ctl_cmd_"
RESP_PREFIX = "ctl_resp_"
BRIDGE_PORT_FILE = "bridge_udp.json"
RESPONSE_POLL_SECONDS = 0.02
# Request ids must be unique ACROSS SERVERS sharing the spool: the low bits
# count requests, the high bits are this server's pid. Both fit in the
# controller's plain-int id contract.
_ID_PID_SHIFT = 20


class CtlSpoolControl:
    """MidiControl's carrier surface over pid-addressed ctl spool files."""

    def __init__(self, session_spool_root: Path) -> None:
        self._session_spool_root = session_spool_root
        self._ids = count(1)
        self._call_lock = Lock()
        self._hello: HelloEvent | None = None

    # -- identity ---------------------------------------------------------------

    def target_pid(self) -> int | None:
        """The session's FL pid, from the bridge port file - re-read per call
        because FL re-execs at boot and can relaunch mid-session."""
        try:
            payload = json.loads((self._session_spool_root / BRIDGE_PORT_FILE).read_text())
        except (OSError, ValueError):
            return None
        pid = payload.get("pid")
        return pid if isinstance(pid, int) and pid > 0 else None

    def _ctl_root(self) -> Path | None:
        """The controller's shared spool - deliberately NOT env-overridable:
        this is the folder the sandboxed script inside FL can actually reach."""
        for settings in fl_settings_dirs():
            for sub in SPOOL_SUBDIRS:
                root = settings / sub
                if root.is_dir():
                    return root
        return None

    # -- the carrier contract ----------------------------------------------------

    def is_live(self) -> bool:
        return self._hello is not None

    def await_hello(self, timeout: float = 5.0) -> HelloEvent:
        """The file lane has no push, so hello is a round trip: ``ctl.hello``
        answers the same fields the wire's controller announces."""
        if self._hello is not None:
            return self._hello
        response = self.send_request("ctl.hello", timeout=timeout)
        if not response.ok or not isinstance(response.ret, dict):
            raise control_plane_unavailable("ctl.hello", timeout)
        self._hello = HelloEvent.model_validate(response.ret)
        return self._hello

    def send_request(
        self,
        op: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> ControlResponse:
        with self._call_lock:
            pid = self.target_pid()
            if pid is None:
                raise ToolError(
                    "control_plane_unavailable",
                    f"No response to {op!r}: this session's FL has not published "
                    f"its bridge port file under {self._session_spool_root}.",
                    "Wait for the session's FL instance to finish loading its "
                    "project (the bridge publishes bridge_udp.json), then retry.",
                    retryable=True,
                )
            root = self._ctl_root()
            if root is None:
                raise control_plane_unavailable(op, 0.0)
            cmd = ControlRequest(
                id=(os.getpid() << _ID_PID_SHIFT) + next(self._ids),
                op=op,
                args=args or [],
                kwargs=kwargs or {},
            )
            payload = cmd.model_dump()
            payload["pid"] = pid
            cmd_path = root / f"{CMD_PREFIX}{cmd.id}.json"
            tmp_path = root / f"{CMD_PREFIX}{cmd.id}.json.tmp"
            tmp_path.write_text(json.dumps(payload))
            tmp_path.rename(cmd_path)  # the controller must never see a half-written command
            try:
                return self._await_response(root, cmd.id, op, timeout)
            finally:
                # This server owns both files; unlike the sandboxed script it
                # may remove them, so the shared folder does not accumulate.
                for stale in (cmd_path, root / f"{RESP_PREFIX}{cmd.id}.json"):
                    with contextlib.suppress(OSError):
                        stale.unlink()

    def _await_response(self, root: Path, cmd_id: int, op: str, timeout: float) -> ControlResponse:
        resp_path = root / f"{RESP_PREFIX}{cmd_id}.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return ControlResponse.model_validate(json.loads(resp_path.read_text()))
            except (OSError, ValueError):
                time.sleep(RESPONSE_POLL_SECONDS)
        if self._hello is None:
            raise control_plane_unavailable(op, timeout)
        raise ToolError(
            "control_plane_unavailable",
            f"No response to {op!r} within {timeout:g}s, but this session's "
            "controller HAS answered before - FL is likely busy, not absent.",
            "FL parks calls that arrive 'unsafe at current time' for OnIdle retry "
            "(common right after a project load). Retry, with a longer timeout if "
            "this was the first engine call.",
            retryable=True,
        )

    # -- push surface: the file lane has none -------------------------------------

    def add_event_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """No push exists here; accepted so callers stay carrier-blind."""

    def drain_events(self) -> list[dict[str, Any]]:
        return []

    def dropped_frames(self) -> int:
        return 0

    def close(self) -> None:
        """Nothing persistent to release: every round trip cleans its files."""
