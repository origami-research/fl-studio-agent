"""UDP client for the native Bridge plugin - the low-latency carrier of the
spool protocol (design: docs/22).

Discovery: the plugin binds an ephemeral localhost port and publishes
``bridge_udp.json`` (port + 128-bit token) into the spool roots this server
already reads. Every datagram carries the token - localhost UDP is spoofable
by any local process, so requests prove they read the port file and acks
prove they came from the token's holder.

Frame (both directions)::

    FLMCP1 <32-hex token> <payload JSON>

The outbound payload is the ``SpoolCommand`` JSON verbatim - the same bytes
``Spool`` would write to ``commands/NNNNNN.json``; the inbound payload is the
result shape the bridge also writes to ``result.json``. One protocol, two
carriers: this class mirrors ``Spool``'s interface (``send`` /
``await_response``) and shares its id space (``spool.resume_next_id``),
because the bridge's consumed-id ledger spans both.

Delivery is at-least-once (retransmit until acked); the bridge's ledger makes
it exactly-once. A timeout here CANNOT cancel a datagram the bridge already
queued (unlike the file spool's tombstone), so callers treat a timeout as
retryable and the session's readback verification stays the authority on what
landed - never this client's word.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from hmac import compare_digest
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field, ValidationError

from fl_mcp import errors
from fl_mcp.schemas import OP_ARGS, SCHEMA_VERSION, SpoolCommand, SpoolOp, SpoolResponse
from fl_mcp.spool import read_json, resume_next_id, spool_roots

log = logging.getLogger(__name__)

PORT_FILE = "bridge_udp.json"
MAGIC = b"FLMCP1 "
TOKEN_HEX_LEN = 32
# The transport's declared ceiling (bridge_udp.h kMaxDatagramBytes); larger
# commands take the file spool - fragmenting datagrams would be reinventing TCP.
MAX_DATAGRAM_BYTES = 60000
_RETRANSMIT_INTERVAL = 0.25
_RECV_SLICE = 0.05
_SOCKET_BUF_BYTES = 262144  # platform UDP defaults are below MAX_DATAGRAM_BYTES


class BridgeUnavailable(RuntimeError):
    """No usable Bridge UDP endpoint for this request; use the file spool."""


class BridgeEndpoint(BaseModel):
    """The contents of the plugin's published port file."""

    proto: int
    transport: int = 1
    port: int = Field(ge=1, le=65535)
    token: str = Field(min_length=TOKEN_HEX_LEN, max_length=TOKEN_HEX_LEN, pattern=r"^[0-9a-f]+$")
    pid: int | None = None


def discover(roots: list[Path] | None = None) -> BridgeEndpoint | None:
    """The published endpoint from the first root that has a valid port file,
    or None (bridge not loaded, or it shut down cleanly and unpublished)."""
    for root in roots if roots is not None else spool_roots():
        payload = read_json(root / PORT_FILE)
        if payload is None:
            continue
        try:
            return BridgeEndpoint.model_validate(payload)
        except ValidationError:
            log.warning("malformed %s under %s; ignoring it", PORT_FILE, root)
    return None


def connect(roots: list[Path] | None = None, *, ping_timeout: float = 0.5) -> BridgeWire | None:
    """THE carrier decision (R4 B3-1, adopted): a ready ``BridgeWire`` when
    the bridge published an endpoint AND answers one id-less ping round
    trip; None means use the file spool for this session. Capability
    detection, never a flag - and freshness is proven by the round trip,
    not a stamp (R4 B2-7): a port file left behind by a crashed FL cannot
    pong, and a wedged GUI thread cannot ack, so both correctly fall back.

    The canonical wiring is one expression at session setup:
    ``carrier = bridge_wire.connect() or spool``."""
    endpoint = discover(roots)
    if endpoint is None:
        return None
    wire = BridgeWire(endpoint, roots=roots)
    if wire.ping(timeout=ping_timeout):
        return wire
    wire.close()
    log.info(
        "bridge UDP endpoint on port %d is published but not answering pings; "
        "using the file spool",
        endpoint.port,
    )
    return None


class BridgeWire:
    """``Spool``-shaped client (``send`` / ``await_response``) over UDP."""

    def __init__(self, endpoint: BridgeEndpoint, *, roots: list[Path] | None = None) -> None:
        """``roots`` is the id-resume seam (tmp_path in tests); defaults to the
        real spool roots so ids never collide with ``Spool``'s."""
        self._endpoint = endpoint
        self._token = endpoint.token.encode("ascii")
        self._lock = threading.Lock()  # id allocation + the pending/ack maps
        self._recv_lock = threading.Lock()  # one reader on the socket at a time
        self._next_id = resume_next_id(roots if roots is not None else spool_roots())
        self._pending: dict[int, bytes] = {}  # id -> frame, kept for retransmit
        self._acks: dict[int, SpoolResponse] = {}  # acks read while awaiting another id
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SOCKET_BUF_BYTES)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _SOCKET_BUF_BYTES)
        # connect() pins the peer: sends need no address and the kernel drops
        # datagrams from anyone else before we even see them.
        self._sock.connect(("127.0.0.1", endpoint.port))

    def close(self) -> None:
        self._sock.close()

    def send(self, op: str, **args: Any) -> SpoolCommand:
        """Schema-validated, sequence-numbered, transmitted once (retransmits
        happen in ``await_response``). Raises ``BridgeUnavailable`` for a
        command above the datagram ceiling - the caller sends THAT command via
        the file spool instead."""
        if op not in OP_ARGS:
            raise ValueError(f"unknown spool op {op!r}; expected one of {sorted(OP_ARGS)}")
        validated = OP_ARGS[op](**args).model_dump(mode="json")
        with self._lock:
            cmd = SpoolCommand(id=self._next_id, op=cast(SpoolOp, op), args=validated)
            self._next_id += 1
        payload = cmd.model_dump_json().encode("utf-8")
        if len(payload) > MAX_DATAGRAM_BYTES:
            raise BridgeUnavailable(
                f"command {cmd.id} is {len(payload)} bytes, above the "
                f"{MAX_DATAGRAM_BYTES}-byte datagram ceiling; send it via the file spool"
            )
        frame = MAGIC + self._token + b" " + payload
        with self._lock:
            self._pending[cmd.id] = frame
        self._transmit(frame, first=True)
        return cmd

    def await_response(self, command: SpoolCommand, timeout: float) -> SpoolResponse:
        """Block for the bridge's ack; retransmits until acked (the ledger
        dedupes), raises the note plane's retryable ToolError on timeout."""
        with self._lock:
            frame = self._pending.get(command.id)
        try:
            ack = self._await_ack(command.id, frame, timeout)
        finally:
            with self._lock:
                self._pending.pop(command.id, None)
        if ack is not None:
            return ack
        raise errors.note_plane_unavailable(command.id, timeout)

    def ping(self, timeout: float = 0.5) -> bool:
        """One liveness round trip through the whole pipeline (socket thread ->
        queue -> tick -> ack). False means: fall back to the file spool.

        The probe is ID-LESS on purpose: the bridge answers it statelessly
        (ack id 0) and never claims a ledger slot, so heartbeats cannot evict
        real command ids from the bridge's bounded ledger ring."""
        payload = json.dumps({"proto": SCHEMA_VERSION, "op": "ping"}).encode("utf-8")
        frame = MAGIC + self._token + b" " + payload
        with self._lock:
            self._acks.pop(0, None)  # a stale pong must not answer this probe
        try:
            self._transmit(frame, first=True)
        except BridgeUnavailable:
            return False
        ack = self._await_ack(0, frame, timeout)
        return ack is not None and ack.ok

    # -- wire-format jobs -------------------------------------------------------

    def _await_ack(self, ack_id: int, frame: bytes | None, timeout: float) -> SpoolResponse | None:
        """The one receive loop: wait for the ack correlated to `ack_id`,
        retransmitting `frame` on an interval; None on deadline."""
        deadline = time.monotonic() + timeout
        next_retransmit = time.monotonic() + _RETRANSMIT_INTERVAL
        while True:
            with self._lock:
                ack = self._acks.pop(ack_id, None)
            if ack is not None:
                return ack
            now = time.monotonic()
            if now >= deadline:
                return None
            if now >= next_retransmit and frame is not None:
                self._transmit(frame, first=False)
                next_retransmit = now + _RETRANSMIT_INTERVAL
            self._recv_into_acks(min(_RECV_SLICE, deadline - now))

    def _transmit(self, frame: bytes, *, first: bool) -> None:
        """Boundary: the first transmit of a command converts socket failure to
        BridgeUnavailable; a failed RETRANSMIT is logged and left to the
        timeout path (the command may already be queued bridge-side, so
        'unavailable' would be a claim we cannot make)."""
        try:
            self._sock.send(frame)
        except OSError as exc:
            if first:
                raise BridgeUnavailable(
                    f"could not send to the bridge UDP port {self._endpoint.port}: {exc}"
                ) from exc
            log.warning("bridge UDP retransmit failed: %s", exc)

    def _recv_into_acks(self, timeout: float) -> None:
        """Read whatever acks arrive within `timeout` into the shared map.
        Only one thread reads the socket at a time; others just wait a slice
        (their ack lands in the map either way)."""
        if not self._recv_lock.acquire(blocking=False):
            time.sleep(timeout)
            return
        try:
            self._sock.settimeout(timeout)
            datagram = self._sock.recv(65536)
        except TimeoutError:
            return
        except OSError:
            # ECONNREFUSED from a dead bridge (ICMP on a connected UDP socket)
            # and friends: not an ack, not fatal - the timeout path reports.
            time.sleep(timeout)
            return
        finally:
            self._recv_lock.release()
        response = self._parse_ack(datagram)
        if response is not None:
            with self._lock:
                self._acks[response.id] = response

    def _parse_ack(self, datagram: bytes) -> SpoolResponse | None:
        """Authenticate and decode one ack; None for anything unproven (bad
        frame, bad token, junk JSON) - the retransmit/timeout machinery treats
        it as silence, and the drop is logged, never swallowed."""
        header_len = len(MAGIC) + TOKEN_HEX_LEN + 1
        if (
            len(datagram) < header_len
            or not datagram.startswith(MAGIC)
            or datagram[header_len - 1 : header_len] != b" "
            or not compare_digest(datagram[len(MAGIC) : len(MAGIC) + TOKEN_HEX_LEN], self._token)
        ):
            log.warning("dropping unauthenticated datagram on the bridge wire")
            return None
        try:
            payload = json.loads(datagram[header_len:])
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("dropping unparseable ack on the bridge wire")
            return None
        try:
            return SpoolResponse.model_validate(payload)
        except ValidationError:
            log.warning("dropping ack that does not match the result shape: %r", datagram[:120])
            return None
