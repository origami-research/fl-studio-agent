"""File mailbox between the server and the code running inside FL.

LANE S implements this module (docs/21). ONE protocol - the sequenced
command/response directories; v1's single-pending-file generation dies at
cutover, and the piano-roll pyscript is updated in lockstep (that update is
part of Lane S's deliverable, preserving the fail-closed applied-ids ledger).

UGLY(U2), quarantined: FL's embedded-Python sandbox. ``open()`` works only
inside FL's data tree and only 'Piano roll scripts' accepts writes;
``os.remove`` raises SystemError - so consumption is tombstones + a ledger,
never deletion. Every rule here was measured, not guessed (docs/10).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .schemas import SpoolCommand, SpoolResponse


class SpoolUnavailable(RuntimeError):
    """FL Studio's settings folder could not be found."""


class Spool:
    """A session-scoped mailbox under FL's settings tree."""

    def __init__(self, root: Path | None = None) -> None:
        """``root=None`` discovers the measured writable location; an explicit
        root is the test seam (tmp_path)."""
        raise NotImplementedError("Lane S")

    def send(self, op: str, **args: Any) -> SpoolCommand:
        """Schema-validated (schemas.OP_ARGS), sequence-numbered (lock-guarded
        - concurrent tool calls must never share an id), atomically written."""
        raise NotImplementedError("Lane S")

    def await_response(self, command: SpoolCommand, timeout: float) -> SpoolResponse:
        """Block for the in-FL consumer's confirmation; raises ToolError
        (note_plane_unavailable) on timeout - never claims unverified success."""
        raise NotImplementedError("Lane S")


# -- helpers --------------------------------------------------------------------


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """tmp + rename so a reader never sees partial JSON; the temp file is
    cleaned up even on KeyboardInterrupt (the one legitimate
    ``except BaseException: ... raise``)."""
    raise NotImplementedError("Lane S")


def read_json(path: Path) -> dict[str, Any] | None:
    """None for a missing file; CORRUPTION IS LOGGED, never silently read as
    'empty' - a torn ledger read as empty replays applied commands."""
    raise NotImplementedError("Lane S")


def spool_roots() -> list[Path]:
    """The measured writable locations under FL's settings tree, best first
    (Settings/Piano roll scripts/FLMCP, then Settings/Piano roll scripts)."""
    raise NotImplementedError("Lane S")
