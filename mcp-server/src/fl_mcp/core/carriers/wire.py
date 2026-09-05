"""The MIDI carrier: server-hosted virtual ports + SysEx correlation, the
control plane to the in-FL controller script.

The codec (framing/chunking/reassembly) is ``flmcp_codec`` - the frozen module
the controller script embeds byte-for-byte; imported as a real installed
package (repo-root pyproject), no path games.

NO module-level singleton: MidiControl wraps a process-unique resource
(duplicate virtual ports show FL duplicate devices and silently split
traffic), and uniqueness is owned by the ONE caller that constructs it -
``session.create_session()`` in production, the e2e fixture in tests.

Ordering rule: FL Studio starts first, then these ports hot-plug into the
running host.

This plane has its own consumer and its own id space (the controller script
correlates in memory; there is no on-disk ledger), so it satisfies the
Carrier contract directly rather than joining the spool Transport.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from fl_mcp._codec import (
    TYPE_CHUNK,
    TYPE_REQUEST,
    TYPE_RESPONSE,
    FrameError,
    Reassembler,
    decode_frame,
    encode_message,
)
from fl_mcp.errors import ToolError, control_plane_busy, control_plane_unavailable
from fl_mcp.schemas import ControlRequest, ControlResponse, HelloEvent

log = logging.getLogger(__name__)

REQ_PORT_NAME = "FLMCP Req"  # server -> FL (FL sees it as a MIDI input source)
RES_PORT_NAME = "FLMCP Res"  # FL -> server (FL sees it as a MIDI output dest)
# Bump only when the MIDI framing or request/response semantics change.  The
# broker includes it in its identity so a resident process cannot keep serving
# a client built for a different wire.
WIRE_PROTOCOL_VERSION = 1

RESPONSE_POLL_SECONDS = 0.005
EVENT_BUFFER_SIZE = 256


class ControlUnavailable(RuntimeError):
    """python-rtmidi is missing, or the virtual ports could not be opened."""


class MidiControl:
    """Owns the virtual ports and request/response correlation, in the Carrier
    shape (send / await_response / is_live). ``send_request`` composes the two
    halves serialized - the single-writer FL model: a response is never
    mismatched to the wrong request."""

    def __init__(self) -> None:
        # Resolved here, not at module top, so the substitution seam tests use
        # (a fake ``rtmidi`` in sys.modules) binds at construction time, and a
        # missing dependency surfaces as ControlUnavailable - the contract -
        # rather than making this module unimportable.
        try:
            import rtmidi
        except ImportError as exc:
            raise ControlUnavailable(
                "python-rtmidi is not installed; the MIDI control plane is unavailable"
            ) from exc

        self._ids = itertools.count(1)
        self._call_lock = threading.Lock()  # serializes send_request round trips
        self._lock = threading.Lock()  # the pending/response maps + port writes
        self._pending: dict[int, ControlRequest] = {}
        self._responses: dict[int, dict[str, Any]] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=EVENT_BUFFER_SIZE)
        self._event_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._reassembler = Reassembler()
        self._dropped_frames = 0
        self._hello: HelloEvent | None = None
        self._hello_event = threading.Event()

        # Assigned before the open attempt so close() (also invoked by the
        # failure path below) always finds both attributes.
        self._out: Any = None
        self._in: Any = None
        try:
            self._out = rtmidi.MidiOut()
            self._out.open_virtual_port(REQ_PORT_NAME)
            self._in = rtmidi.MidiIn()
            self._in.ignore_types(sysex=False)  # rtmidi drops SysEx by default
            # rtmidi invokes the callback as fn(event, user_data); the frozen
            # _on_message takes the event alone, so the extra arg is shed here.
            self._in.set_callback(lambda event, _data=None: self._on_message(event))
            self._in.open_virtual_port(RES_PORT_NAME)
        except rtmidi.RtMidiError as exc:
            self.close()
            raise ControlUnavailable(f"could not open the virtual MIDI ports: {exc}") from exc

    # -- the Carrier contract ---------------------------------------------------

    def is_live(self) -> bool:
        """A controller announced itself on these ports; without a hello the
        wire is plugged in but nobody is listening."""
        return self._hello is not None

    def send(self, cmd: ControlRequest) -> None:
        """Frame and transmit one request, without waiting."""
        with self._lock:
            self._pending[cmd.id] = cmd
            for frame in encode_message(TYPE_REQUEST, cmd.model_dump(), msg_id=cmd.id):
                self._out.send_message(list(frame))

    def await_response(self, cmd_id: int, timeout: float) -> ControlResponse:
        """Block for the correlated response.

        NOTE(U4): FL parks engine calls that arrive "unsafe at current time"
        for OnIdle retry, and OnIdle freezes under modal dialogs - callers see
        that as a timeout, not an error (live-observed 2026-08-27).
        """
        op = self._pending[cmd_id].op
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cmd_id in self._responses:
                    return ControlResponse.model_validate(self._responses.pop(cmd_id))
                time.sleep(RESPONSE_POLL_SECONDS)
        finally:
            # Pop both: on timeout the callback may have written a late
            # response between the poll's last check and here; dropping it
            # prevents an unbounded per-timeout leak (ids are never reused).
            self._pending.pop(cmd_id, None)
            self._responses.pop(cmd_id, None)
        # Same category either way, but the language distinguishes the
        # two live-measured failure modes (docs/21 A6): no hello means
        # no controller is attached; hello + timeout usually means FL
        # parked the call "unsafe at current time" - busy, not absent.
        if self._hello is None:
            raise control_plane_unavailable(op, timeout)
        raise ToolError(
            "control_plane_unavailable",
            f"No response to {op!r} within {timeout:g}s, but the controller IS "
            "attached (hello received) - FL is likely busy, not disconnected.",
            "FL parks calls that arrive 'unsafe at current time' for OnIdle retry "
            "(common right after a project load) and the controller gives up after "
            "~4s. Retry, with a longer timeout if this was the first engine call.",
            retryable=True,
        )

    # -- the serialized round trip ----------------------------------------------

    def send_request(
        self,
        op: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        timeout: float = 2.0,
        pid: int | None = None,
    ) -> ControlResponse:
        """One correlated round trip to the controller script.

        ``pid`` addresses one FL among many on the shared broadcast wire; None
        reaches every attached controller (fine when only one is).

        The call lock is acquired with a bound, not held open: the wire is a
        single serialization point shared by every session through the broker,
        so an op that FL parks (waiting its full timeout) must not let another
        session's calls queue behind it without limit. A caller that cannot get
        the wire within its own timeout fails fast and retryably instead of
        stacking into the multi-minute hang seen in the first live match."""
        if not self._call_lock.acquire(timeout=timeout):
            raise control_plane_busy(op, timeout)
        try:
            cmd = ControlRequest(id=next(self._ids), op=op, args=args or [], kwargs=kwargs or {}, pid=pid)
            self.send(cmd)
            return self.await_response(cmd.id, timeout)
        finally:
            self._call_lock.release()

    def await_hello(self, timeout: float = 5.0) -> HelloEvent:
        """Block until the controller announces itself; raises ToolError
        (control_plane_unavailable) on timeout."""
        hello = self._hello
        if hello is not None:
            return hello
        if not self._hello_event.wait(timeout):
            raise control_plane_unavailable("hello", timeout)
        hello = self._hello
        if hello is None:  # event set but hello cleared - a real protocol bug
            raise control_plane_unavailable("hello", timeout)
        return hello

    def add_event_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._event_listeners.append(fn)

    def drain_events(self) -> list[dict[str, Any]]:
        events = list(self._events)
        self._events.clear()
        return events

    @property
    def dropped_frames(self) -> int:
        """Malformed frames swallowed by the C callback since open - surfaced
        in fl_doctor so 'every frame rejected' is diagnosable, not a mystery
        timeout."""
        return self._dropped_frames

    def close(self) -> None:
        for port in (self._in, self._out):
            if port is None:
                continue
            # Best-effort teardown: a port that fails to close is already gone.
            with contextlib.suppress(Exception):
                port.close_port()

    # -- helpers ---------------------------------------------------------------

    def _on_message(self, message: Any) -> None:
        """UGLY(U3), quarantined: runs on rtmidi's C callback thread where an
        escaping exception is undefined behavior. Malformed frames are dropped
        AND counted (see ``dropped_frames``)."""
        try:
            decoded = decode_frame(message[0])
            if decoded is None:
                return  # not our traffic (another device sharing the port)
            frame_type, payload = decoded
            reassembled = self._reassembler.feed(frame_type, payload)
        except (FrameError, KeyError, TypeError, ValueError):
            self._dropped_frames += 1
            return
        if reassembled is None:
            return  # chunk buffered; the message is not complete yet
        # Chunking erases the frame type (the envelope doesn't carry it), so a
        # reassembled chunk is classified by shape: the controller's responses
        # always carry "ok", its events never do. v1 routed by frame type only
        # and silently dropped every chunked inbound message - the caller saw
        # a timeout where a response had in fact arrived.
        if frame_type == TYPE_RESPONSE or (frame_type == TYPE_CHUNK and "ok" in reassembled):
            self._correlate(reassembled)
            return
        self._events.append(reassembled)
        if reassembled.get("op") == "hello":
            try:
                self._hello = HelloEvent.model_validate(reassembled)
            except ValidationError:
                self._dropped_frames += 1
                return
            self._hello_event.set()
        for fn in self._event_listeners:
            try:
                fn(reassembled)
            except Exception:  # U3: an escaping listener would cross the C boundary
                log.exception("event listener failed on %r", reassembled.get("op"))

    def _correlate(self, response: dict[str, Any]) -> None:
        """Match a response frame to its pending request by id."""
        msg_id = response.get("id")
        if msg_id in self._pending:
            self._responses[msg_id] = response
        # else: orphan (the call already timed out and was abandoned) -> drop.
