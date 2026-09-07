"""The one machine-wide MIDI wire, served over authenticated loopback TCP."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fl_mcp.core.carriers.wire import WIRE_PROTOCOL_VERSION, ControlUnavailable, MidiControl
from fl_mcp.errors import ToolError
from fl_mcp.fl.paths import app_data_root
from fl_mcp.platform.runtime import BrokerEndpoint

log = logging.getLogger(__name__)
ENDPOINT_NAME = "control-broker.json"
MAX_CLIENT_TIMEOUT_SECONDS = 60.0
IDLE_TIMEOUT_SECONDS = 5 * 60.0


def broker_version() -> str:
    source = Path(__file__).read_bytes()
    digest = hashlib.sha256(source + b"\0" + str(WIRE_PROTOCOL_VERSION).encode()).hexdigest()
    return f"{WIRE_PROTOCOL_VERSION}:{digest[:16]}"


def default_endpoint() -> BrokerEndpoint:
    return BrokerEndpoint("127.0.0.1", 0, "", app_data_root() / ENDPOINT_NAME)


def _read_endpoint(path: Path) -> BrokerEndpoint | None:
    try:
        data = json.loads(path.read_text())
        result = BrokerEndpoint(data["host"], int(data["port"]), data["token"], path)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return result if result.host == "127.0.0.1" and result.port > 0 and result.token else None


def _write_endpoint(endpoint: BrokerEndpoint) -> None:
    endpoint.port_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = endpoint.port_file.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"host": endpoint.host, "port": endpoint.port, "token": endpoint.token}) + "\n"
    )
    temporary.replace(endpoint.port_file)


def _error(exc: ToolError) -> dict[str, Any]:
    return {
        "kind": "error",
        "category": exc.category,
        "message": exc.message,
        "fix": exc.fix,
        "retryable": exc.retryable,
    }


class ControlBroker:
    """Bind-wins TCP owner. Every connection must authenticate before a request."""

    def __init__(
        self, endpoint: BrokerEndpoint | Path | None = None, wire_factory: Callable[[], Any] = MidiControl
    ) -> None:
        # Path remains a test-only compatibility input while callers migrate;
        # it is a TCP endpoint discovery file, never a Unix socket path.
        if isinstance(endpoint, Path):
            endpoint = BrokerEndpoint("127.0.0.1", 0, "", endpoint)
        self._requested_endpoint, self._endpoint = endpoint or default_endpoint(), None
        self._wire, self._server, self._shutdown = wire_factory(), None, threading.Event()
        self._last_client_at, self._client_count, self._client_lock = 0.0, 0, threading.Lock()

    def bind(self) -> bool:
        existing = _read_endpoint(self._requested_endpoint.port_file)
        if existing and _endpoint_answers(existing):
            return False
        with contextlib.suppress(FileNotFoundError):
            self._requested_endpoint.port_file.unlink()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self._requested_endpoint.host, self._requested_endpoint.port))
        except OSError:
            server.close()
            return False
        server.listen(16)
        server.settimeout(1)
        host, port = server.getsockname()[:2]
        self._endpoint = BrokerEndpoint(
            host,
            port,
            self._requested_endpoint.token or secrets.token_hex(16),
            self._requested_endpoint.port_file,
        )
        _write_endpoint(self._endpoint)
        self._server, self._last_client_at = server, time.monotonic()
        return True

    def serve_forever(self) -> None:
        assert self._server is not None
        while not self._shutdown.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                if (
                    self._client_count == 0
                    and time.monotonic() - self._last_client_at >= IDLE_TIMEOUT_SECONDS
                ):
                    break
                continue
            except OSError:
                break
            with self._client_lock:
                self._client_count += 1
            threading.Thread(target=self._serve_client, args=(conn,), daemon=True).start()
        self.close()

    def close(self) -> None:
        if self._server:
            with contextlib.suppress(OSError):
                self._server.close()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self._requested_endpoint.port_file.unlink()

    def _serve_client(self, conn: socket.socket) -> None:
        try:
            with conn, conn.makefile("r") as reader:
                auth = json.loads(reader.readline() or "{}")
                allowed = auth.get("kind") == "broker_auth" and secrets.compare_digest(
                    str(auth.get("token", "")), self._endpoint.token if self._endpoint else ""
                )
                conn.sendall((json.dumps({"kind": "broker_auth", "ok": allowed}) + "\n").encode())
                if not allowed:
                    return
                for line in reader:
                    try:
                        response = self._handle(json.loads(line))
                    except ValueError:
                        response = {
                            "kind": "error",
                            "category": "fl_api_error",
                            "message": "invalid broker JSON",
                            "fix": "Check the broker client.",
                            "retryable": True,
                        }
                    conn.sendall((json.dumps(response) + "\n").encode())
                    if response.get("kind") == "broker_version_mismatch":
                        self._shutdown.set()
                        return
        finally:
            with self._client_lock:
                self._client_count -= 1
                if self._client_count == 0:
                    self._last_client_at = time.monotonic()

    def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        try:
            if req.get("kind") == "broker_hello":
                return (
                    {"kind": "broker_hello", "version": broker_version()}
                    if req.get("version") == broker_version()
                    else {"kind": "broker_version_mismatch", "version": broker_version()}
                )
            timeout = float(req.get("timeout", 2))
            if not 0 < timeout <= MAX_CLIENT_TIMEOUT_SECONDS:
                return _error(
                    ToolError(
                        "control_plane_busy",
                        f"Requested control timeout {timeout:g}s exceeds the {MAX_CLIENT_TIMEOUT_SECONDS:g}s broker limit.",
                        "Retry with a timeout no greater than 60 seconds.",
                        retryable=True,
                    )
                )
            result = self._wire.send_request(
                req["op"],
                args=req.get("args") or [],
                kwargs=req.get("kwargs") or {},
                timeout=timeout,
                pid=req.get("pid"),
            )
            return {"kind": "response", "response": result.model_dump()}
        except ToolError as exc:
            return _error(exc)
        except Exception as exc:  # noqa: BLE001 - malformed client input must not drop the wire
            return {
                "kind": "error",
                "category": "fl_api_error",
                "message": f"{type(exc).__name__}: {exc}",
                "fix": "Check the broker log.",
                "retryable": True,
            }


def _endpoint_answers(endpoint: BrokerEndpoint) -> bool:
    try:
        with socket.create_connection((endpoint.host, endpoint.port), timeout=0.5) as conn:
            conn.sendall((json.dumps({"kind": "broker_auth", "token": endpoint.token}) + "\n").encode())
            return json.loads(conn.makefile("r").readline()).get("ok") is True
    except (OSError, ValueError):
        return False


def main() -> None:
    logging.basicConfig(stream=__import__("sys").stderr, level=os.environ.get("FL_MCP_LOG", "INFO").upper())
    try:
        broker = ControlBroker()
    except ControlUnavailable:
        log.exception("control wire unavailable; broker cannot start")
        raise SystemExit(1)
    if broker.bind():
        broker.serve_forever()
