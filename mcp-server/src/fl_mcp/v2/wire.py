"""Server-hosted MIDI control transport: virtual ports + SysEx correlation.

LANE W implements this module (docs/21), largely a port of v1 control_midi.py.
The codec (framing/chunking/reassembly) is ``flmcp_codec`` - the frozen module
the controller script embeds byte-for-byte; imported as a real installed
package (repo-root pyproject), no path games.

NO module-level singleton: MidiControl wraps a process-unique resource
(duplicate virtual ports show FL duplicate devices and silently split
traffic), and uniqueness is owned by the ONE caller that constructs it -
``session.create_session()`` in production, the e2e fixture in tests.

Ordering rule (docs/14): the ports must exist BEFORE FL Studio launches.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .schemas import ControlResponse, HelloEvent


class ControlUnavailable(RuntimeError):
    """python-rtmidi is missing, or the virtual ports could not be opened."""


class MidiControl:
    """Owns the virtual ports and request/response correlation. Thread-safe
    for the single-writer FL model: calls are serialized so a response is
    never mismatched to the wrong request."""

    def __init__(self) -> None:
        raise NotImplementedError("Lane W")

    def send_request(
        self,
        op: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> ControlResponse:
        """One correlated round trip to the controller script.

        NOTE(U4): FL parks engine calls that arrive "unsafe at current time"
        for OnIdle retry, and OnIdle freezes under modal dialogs - callers see
        that as a timeout, not an error (live-observed 2026-08-27).
        """
        raise NotImplementedError("Lane W")

    def await_hello(self, timeout: float = 5.0) -> HelloEvent:
        """Block until the controller announces itself; raises ToolError
        (control_plane_unavailable) on timeout."""
        raise NotImplementedError("Lane W")

    def add_event_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        raise NotImplementedError("Lane W")

    def drain_events(self) -> list[dict[str, Any]]:
        raise NotImplementedError("Lane W")

    @property
    def dropped_frames(self) -> int:
        """Malformed frames swallowed by the C callback since open - surfaced
        in fl_doctor so 'every frame rejected' is diagnosable, not a mystery
        timeout."""
        raise NotImplementedError("Lane W")

    def close(self) -> None:
        raise NotImplementedError("Lane W")

    # -- helpers ---------------------------------------------------------------

    def _on_message(self, message: Any) -> None:
        """UGLY(U3), quarantined: runs on rtmidi's C callback thread where an
        escaping exception is undefined behavior. Malformed frames are dropped
        AND counted (see ``dropped_frames``)."""
        raise NotImplementedError("Lane W")

    def _correlate(self, response: dict[str, Any]) -> None:
        """Match a response frame to its pending request by id."""
        raise NotImplementedError("Lane W")
