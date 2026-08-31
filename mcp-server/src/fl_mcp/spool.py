"""Transitional shim (docs/29 lane G2): the file mailbox lives in
``fl_mcp.core.carriers.spool``; id allocation and validation live in
``fl_mcp.core.transport.Transport``. The integration manager deletes this
module when session/ is rewired to build its Transport directly."""

from __future__ import annotations

from pathlib import Path

from fl_mcp.core.carriers.spool import (
    LEDGER_FILE,
    QUEUE_FILE,
    SpoolCarrier,
    SpoolUnavailable,
    read_json,
    resume_next_id,
    spool_roots,
    write_json_atomic,
)
from fl_mcp.core.transport import Transport

__all__ = [
    "LEDGER_FILE",
    "QUEUE_FILE",
    "Spool",
    "SpoolCarrier",
    "SpoolUnavailable",
    "read_json",
    "resume_next_id",
    "spool_roots",
    "write_json_atomic",
]


class Spool(Transport):
    """The pre-rewire name: a Transport over the one file carrier."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__([SpoolCarrier(root)], roots=None if root is None else [root])
