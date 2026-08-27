"""Server-hosted MIDI control transport (the corrected sleek plan).

The server opens the virtual MIDI ports itself and speaks the SysEx control
protocol directly to the FLMCP controller script - there is no separate bridge
process. This replaces the probe-era ``flbridge.py``.

Ordering: the ports must exist before FL Studio enumerates MIDI at launch, which
is the natural user flow (start the Claude session, which spawns this server and
opens the ports, *then* open FL). For the FL-already-running case the installer
can enable a persistent IAC bus; see ``install.py`` / docs/14.

The wire codec (framing, chunking, reassembly) lives in ``flmcp_codec.py``
(docs/15 fix item 6), the single source of truth for the protocol. The
controller script embeds it verbatim (byte-for-byte, checked by
``test_codec_sync.py``) since FL loads it as one standalone file; the server
imports the same module directly since it has no such constraint.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTROLLER_DIR = _REPO_ROOT / "fl-scripts" / "controller"
if str(_CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_DIR))

import contextlib

from flmcp_codec import (
    TYPE_EVENT,
    TYPE_REQUEST,
    TYPE_RESPONSE,
    FrameError,
    Reassembler,
    decode_frame,
    encode_message,
)

from . import errors, schemas

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
        self._event_listeners: list[Callable[[dict], None]] = []
        self.hello: schemas.HelloEvent | None = None
        self._hello_event = threading.Event()

        self._out = rtmidi.MidiOut()
        self._out.open_virtual_port(REQ_PORT_NAME)
        self._in = rtmidi.MidiIn()
        self._in.ignore_types(sysex=False)  # rtmidi drops SysEx by default
        self._in.set_callback(self._on_message)
        self._in.open_virtual_port(RES_PORT_NAME)

    # -- inbound ----------------------------------------------------------

    def _on_message(self, event, _data=None) -> None:
        # Runs on rtmidi's callback thread. Any escaping exception would cross a
        # C boundary with undefined behavior, so every step is guarded and the
        # frame is dropped on malformed input (the documented contract).
        try:
            decoded = decode_frame(event[0])
            if decoded is None:
                return
            frame_type, payload = decoded
            message = self._reassembler.feed(frame_type, payload)
        except (FrameError, KeyError, TypeError, ValueError):
            return
        if message is None:
            return
        # Route by frame type, not by id: event and response id namespaces are
        # independent (different sides count them), so a shared-id collision must
        # not misroute. Responses correlate to a live/abandoned call; events buffer.
        if frame_type == TYPE_RESPONSE:
            msg_id = message.get("id")
            if msg_id in self._pending:
                self._responses[msg_id] = message
            # else: orphan (call already timed out and was abandoned) -> drop.
        elif frame_type == TYPE_EVENT:
            self._events.append(message)
            if message.get("op") == "hello":
                self.hello = schemas.HelloEvent.model_validate(message)
                self._hello_event.set()
            for fn in self._event_listeners:
                fn(message)

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
                # Pop both: on timeout the callback may have written a late
                # response between the poll's last check and here; dropping it
                # prevents an unbounded per-timeout leak (ids are never reused).
                self._pending.pop(req_id, None)
                self._responses.pop(req_id, None)

    def drain_events(self) -> list[dict]:
        """Return and clear buffered events (hello, dirty_channel, refresh, ...)."""
        events, self._events = self._events, []
        return events

    # -- kernel/session ergonomics ------------------------------------------
    #
    # The kernel (kernel.py) and session (session.py) depend only on this
    # surface, not on MidiControl itself, so tests can substitute
    # fake_fl.FakeControl without touching real MIDI ports.

    def add_event_listener(self, fn: Callable[[dict], None]) -> None:
        """Subscribe to inbound events as they arrive (docs/11 Layer 1),
        instead of polling ``drain_events()``."""
        self._event_listeners.append(fn)

    def await_hello(self, timeout: float = 5.0) -> schemas.HelloEvent:
        if self.hello is not None:
            return self.hello
        if not self._hello_event.wait(timeout):
            raise errors.control_plane_unavailable("hello", timeout)
        assert self.hello is not None
        return self.hello

    def send_request(
        self,
        op: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> schemas.ControlResponse:
        """``call()`` wrapped as a validated ``schemas.ControlResponse`` —
        the shape kernel.py and session.py's doctor()/tools already expect.

        ``call()`` raises a plain ``TimeoutError`` on no response (it has no
        dependency on errors.py); every caller here wants the structured
        ``control_plane_unavailable`` ToolError instead, so it's converted
        at this one seam.
        """
        try:
            resp = self.call(op, args=args, kwargs=kwargs, timeout=timeout)
        except TimeoutError as exc:
            raise errors.control_plane_unavailable(op, timeout) from exc
        return schemas.ControlResponse.model_validate(resp)

    def ping(self, timeout: float = 2.0) -> bool:
        return self.send_request("ping", timeout=timeout).ok

    def close(self) -> None:
        for port in (self._in, self._out):
            with contextlib.suppress(Exception):
                port.close_port()


_singleton: MidiControl | None = None
_singleton_lock = threading.Lock()


def get_control() -> MidiControl:
    """Process-wide MIDI control transport, opened on first use.

    Double-checked locking: ``call()`` is a blocking function, so a threaded MCP
    server may invoke it concurrently. Constructing two MidiControls would open
    duplicate virtual ports (FL sees duplicate devices; the losing instance's
    callback silently swallows traffic). The lock makes first-open atomic.
    """
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = MidiControl()
    return _singleton


def close() -> None:
    """Close the process-wide transport and reset the singleton.

    Without the reset a later get_control()/call() would reuse the dead instance
    and hang against closed ports; resetting makes the next call reopen cleanly.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.close()
            _singleton = None


def call(op: str, args: list | None = None, kwargs: dict | None = None, timeout: float = 5.0) -> dict:
    return get_control().call(op, args=args, kwargs=kwargs, timeout=timeout)
