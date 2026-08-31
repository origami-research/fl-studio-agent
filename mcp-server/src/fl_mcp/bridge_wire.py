"""Transitional shim (docs/29 lane G2): the UDP client lives in
``fl_mcp.core.carriers.udp``. The integration manager deletes this module
when session/ is rewired to build its Transport directly."""

from __future__ import annotations

from pathlib import Path

from fl_mcp.core.carriers.udp import (
    MAGIC,
    MAX_DATAGRAM_BYTES,
    PORT_FILE,
    BridgeEndpoint,
    BridgeUnavailable,
    UdpCarrier,
    discover,
)
from fl_mcp.core.transport import Transport

__all__ = [
    "MAGIC",
    "MAX_DATAGRAM_BYTES",
    "PORT_FILE",
    "BridgeEndpoint",
    "BridgeUnavailable",
    "BridgeWire",
    "UdpCarrier",
    "connect",
    "discover",
]

BridgeWire = UdpCarrier  # the pre-rewire name, annotation-only at today's call-sites


def connect(roots: list[Path] | None = None, *, ping_timeout: float = 0.5) -> Transport | None:
    """The pre-rewire carrier decision: a ready Transport over the UDP carrier
    when it proves live (endpoint + pong, UdpCarrier.is_live), else None and
    the caller uses the file spool. Post-rewire this collapses into ONE
    Transport([UdpCarrier(), SpoolCarrier()]) picking per call."""
    carrier = UdpCarrier(roots, ping_timeout=ping_timeout)
    return Transport([carrier], roots=roots) if carrier.is_live() else None
