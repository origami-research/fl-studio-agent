"""Server-hosted MIDI control transport (the corrected sleek plan).

The server opens the virtual MIDI ports itself and speaks the SysEx control
protocol directly to the FLMCP controller script - there is no separate bridge
process. This replaces the probe-era ``flbridge.py``.

Ordering: the ports must exist before FL Studio enumerates MIDI at launch, which
is the natural user flow (start the Claude session, which spawns this server and
opens the ports, *then* open FL). For the FL-already-running case the installer
can enable a persistent IAC bus; see ``install.py`` / docs/14.

The wire codec (framing, chunking, reassembly) lives in the controller script,
which is the single source of truth for the protocol and is deployed standalone
into FL. We import it here rather than maintain a second copy. (Cleanup noted in
docs/14: lift the codec into a shared vendored module if a third consumer
appears.)
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTROLLER_DIR = _REPO_ROOT / "fl-scripts" / "controller"
if str(_CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_DIR))

from device_FLMCP import (  # noqa: E402  (path set above)
    TYPE_CHUNK,
    TYPE_REQUEST,
    TYPE_RESPONSE,
    FrameError,
    Reassembler,
    decode_frame,
    encode_message,
)

REQ_PORT_NAME = "FLMCP Req"  # server -> FL (FL sees it as a MIDI input source)
RES_PORT_NAME = "FLMCP Res"  # FL -> server (FL sees it as a MIDI output dest)


class ControlUnavailable(RuntimeError):
    """python-rtmidi is missing, or the ports could not be opened."""


class MidiControl:
    """Owns the virtual ports and the request/response correlation.

    Thread-safe for the single-writer FL model: calls are serialized so a
    response is never mismatched to the wrong request.
    """

    def __init__(self) -> None:
        try:
            import rtmidi
        except ImportError as exc:  # pragma: no cover - env guard
            raise ControlUnavailable(
                "python-rtmidi is not installed; the MIDI control plane is unavailable"
            ) from exc

        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._responses: dict[int, dict] = {}
        self._events: list[dict] = []
        self._reassembler = Reassembler()

        self._out = rtmidi.MidiOut()
        self._out.open_virtual_port(REQ_PORT_NAME)
        self._in = rtmidi.MidiIn()
        self._in.ignore_types(sysex=False)  # rtmidi drops SysEx by default
        self._in.set_callback(self._on_message)
        self._in.open_virtual_port(RES_PORT_NAME)

    # -- inbound ----------------------------------------------------------

    def _on_message(self, event, _data=None) -> None:
        try:
            decoded = decode_frame(event[0])
        except FrameError:
            return
        if decoded is None:
            return
        frame_type, payload = decoded
        message = self._reassembler.feed(frame_type, payload)
        if message is None:
            return
        msg_id = message.get("id")
        if msg_id in self._pending:
            self._responses[msg_id] = message
        else:
            self._events.append(message)

    # -- outbound ---------------------------------------------------------

    def call(self, op: str, args: list | None = None, kwargs: dict | None = None,
             timeout: float = 5.0) -> dict:
        """Send one control op and block until its response lands (or timeout)."""
        with self._lock:
            req_id = next(self._ids)
            payload = {"id": req_id, "op": op, "args": args or [], "kwargs": kwargs or {}}
            self._pending[req_id] = payload
            for frame in encode_message(TYPE_REQUEST, payload, msg_id=req_id):
                self._out.send_message(list(frame))

            deadline = time.time() + timeout
            try:
                while time.time() < deadline:
                    if req_id in self._responses:
                        return self._responses.pop(req_id)
                    time.sleep(0.005)
                raise TimeoutError(
                    f"no response to control op {op!r} (id {req_id}) within {timeout}s - "
                    "is FL running with the FLMCP controller script loaded? "
                    "(the ports must exist before FL launches; see docs/14)"
                )
            finally:
                self._pending.pop(req_id, None)

    def drain_events(self) -> list[dict]:
        """Return and clear buffered events (hello, dirty_channel, refresh, ...)."""
        events, self._events = self._events, []
        return events

    def close(self) -> None:
        for port in (self._in, self._out):
            try:
                port.close_port()
            except Exception:
                pass


_singleton: MidiControl | None = None


def get_control() -> MidiControl:
    """Process-wide MIDI control transport, opened on first use."""
    global _singleton
    if _singleton is None:
        _singleton = MidiControl()
    return _singleton


def call(op: str, args: list | None = None, kwargs: dict | None = None, timeout: float = 5.0) -> dict:
    return get_control().call(op, args=args, kwargs=kwargs, timeout=timeout)
