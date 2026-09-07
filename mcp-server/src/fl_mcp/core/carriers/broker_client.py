"""Client for the machine's authenticated loopback-TCP control broker."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

from fl_mcp.contract import BRIDGE_PORT_FILE
from fl_mcp.core.carriers.control_broker import _read_endpoint, broker_version, default_endpoint
from fl_mcp.errors import ToolError, control_plane_unavailable
from fl_mcp.platform.runtime import BrokerEndpoint
from fl_mcp.schemas import ControlResponse, HelloEvent

_BROKER_START_TIMEOUT = 10.0


class BrokerControl:
    def __init__(
        self,
        session_spool_root: Path,
        endpoint: BrokerEndpoint | Path | None = None,
        *,
        socket_path: Path | None = None,
    ) -> None:
        endpoint = socket_path or endpoint
        if isinstance(endpoint, Path):
            endpoint = BrokerEndpoint("127.0.0.1", 0, "", endpoint)
        self._session_spool_root, self._endpoint, self._hello, self._broker_spawned = (
            session_spool_root,
            endpoint or default_endpoint(),
            None,
            False,
        )

    def target_pid(self) -> int | None:
        try:
            pid = json.loads((self._session_spool_root / BRIDGE_PORT_FILE).read_text()).get("pid")
        except (OSError, ValueError):
            return None
        return pid if isinstance(pid, int) and pid > 0 else None

    def is_live(self) -> bool:
        return self._hello is not None

    def await_hello(self, timeout: float = 5.0) -> HelloEvent:
        if self._hello is None:
            response = self.send_request("ctl.hello", timeout=timeout)
            if not response.ok or not isinstance(response.ret, dict):
                raise control_plane_unavailable("ctl.hello", timeout)
            self._hello = HelloEvent.model_validate(response.ret)
        return self._hello

    def send_request(
        self, op: str, args: list | None = None, kwargs: dict | None = None, timeout: float = 2.0
    ) -> ControlResponse:
        pid = self.target_pid()
        if pid is None:
            raise ToolError(
                "control_plane_unavailable",
                f"No response to {op!r}: this session's FL has not published its bridge port file under {self._session_spool_root}.",
                "Wait for FL to finish loading its project, then retry.",
                retryable=True,
            )
        conn = self._connect()
        try:
            conn.sendall(
                (
                    json.dumps(
                        {"pid": pid, "op": op, "args": args or [], "kwargs": kwargs or {}, "timeout": timeout}
                    )
                    + "\n"
                ).encode()
            )
            conn.settimeout(timeout + 5)
            payload = conn.makefile("r").readline()
        finally:
            conn.close()
        if not payload:
            raise control_plane_unavailable(op, timeout)
        data = json.loads(payload)
        if data.get("kind") == "error":
            raise ToolError(
                data["category"], data["message"], data.get("fix", ""), retryable=data.get("retryable", True)
            )
        return ControlResponse.model_validate(data["response"])

    def _connect(self) -> socket.socket:
        deadline = monotonic() + _BROKER_START_TIMEOUT
        while monotonic() < deadline:
            endpoint = _read_endpoint(self._endpoint.port_file) or (
                self._endpoint if self._endpoint.port else None
            )
            if endpoint:
                try:
                    conn = socket.create_connection((endpoint.host, endpoint.port), timeout=0.5)
                    conn.sendall(
                        (json.dumps({"kind": "broker_auth", "token": endpoint.token}) + "\n").encode()
                    )
                    if json.loads(conn.makefile("r").readline()).get("ok") and self._matches_broker_version(
                        conn
                    ):
                        return conn
                    conn.close()
                except (OSError, ValueError):
                    pass
            if not self._broker_spawned:
                self._start_broker()
            sleep(0.1)
        raise ToolError(
            "control_plane_unavailable",
            f"The control broker did not come up from {self._endpoint.port_file} within {_BROKER_START_TIMEOUT:g}s.",
            "Check that python-rtmidi and the configured MIDI ports are available.",
            retryable=True,
        )

    def _start_broker(self) -> None:
        if not self._broker_spawned:
            self._broker_spawned = True
            subprocess.Popen(
                [sys.executable, "-m", "fl_mcp.control_broker"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

    @staticmethod
    def _matches_broker_version(conn: socket.socket) -> bool:
        conn.sendall((json.dumps({"kind": "broker_hello", "version": broker_version()}) + "\n").encode())
        try:
            data = json.loads(conn.makefile("r").readline())
        except ValueError:
            return False
        return data == {"kind": "broker_hello", "version": broker_version()}

    def add_event_listener(self, _fn):
        return None

    def drain_events(self) -> list[dict]:
        return []

    @property
    def dropped_frames(self) -> int:
        return 0

    def close(self) -> None:
        return None
