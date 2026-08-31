"""The Carrier contract (docs/30 Surface 2): one carrier per medium.

A carrier moves ALREADY-BUILT commands over its medium and correlates the
consumer's responses by id. It never allocates ids and never validates args -
that is ``core.transport.Transport``'s job, kept in one place so every
carrier draws from the one id space the consumer's consumed-ledger is keyed
by (the #58 exactly-once invariant).
"""

from __future__ import annotations

from typing import Protocol


class Command(Protocol):
    """A correlatable id; the concrete carrier knows its own body format."""

    id: int


class Response(Protocol):
    id: int
    ok: bool


class CarrierUnavailable(RuntimeError):
    """This carrier cannot take this command NOW (medium missing, endpoint
    dead, frame above the medium's ceiling). Transport falls through to the
    next carrier with the SAME command - same id, exactly-once."""


class Carrier(Protocol):
    """docs/30 Surface 2, frozen."""

    def send(self, cmd: Command) -> None:
        """Dispatch one framed command without waiting; raises
        CarrierUnavailable when this medium cannot take it."""

    def await_response(self, cmd_id: int, timeout: float) -> Response:
        """Block for the consumer's response to ``cmd_id``; raises the
        plane's retryable ToolError on timeout - never a maybe."""

    def is_live(self) -> bool:
        """Capability detection for this medium: evidence, never a flag."""
