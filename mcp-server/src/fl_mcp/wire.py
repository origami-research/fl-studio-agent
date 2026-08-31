"""Transitional shim (docs/29 lane G2): the MIDI control carrier lives in
``fl_mcp.core.carriers.wire``. The integration manager deletes this module
when session/ imports the carrier from its new home."""

from fl_mcp.core.carriers.wire import (
    REQ_PORT_NAME,
    RES_PORT_NAME,
    ControlUnavailable,
    MidiControl,
)

__all__ = ["REQ_PORT_NAME", "RES_PORT_NAME", "ControlUnavailable", "MidiControl"]
