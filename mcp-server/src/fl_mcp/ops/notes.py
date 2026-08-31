"""Notes: TARGETED writes to a (pattern, channel). The bridge serves them live
when the channel-match evidence holds (PROBE_VERDICT Q2: PatNum honored,
ChanNum blind); the file lane serves everything else by byte surgery."""

from __future__ import annotations

from collections.abc import Sequence

from fl_mcp.core.router import (
    Capability,
    CapabilityTag,
    Mode,
    Operation,
    Receipt,
    Routable,
    Target,
    route,
)
from fl_mcp.schemas import Note

WRITE = Operation.spec(
    kind="write_notes",
    requires=Capability.TARGETED,
    tag=CapabilityTag(
        "notes.write.pattern",
        status="live",
        verified_fl="26.1.5",
        evidence="docs/24 map; PROBE_VERDICT Q2; #65 FL-save-confirmed",
    ),
)

CLEAR = Operation.spec(
    kind="clear_notes",
    requires=Capability.TARGETED,
    tag=CapabilityTag(
        "notes.clear",
        status="live",
        verified_fl="26.1.5",
        evidence="docs/24 map; #65 (clear is a replace-with-empty write)",
    ),
)


def write_notes(
    session: Routable, target: Target, notes: Sequence[Note], mode: Mode = "merge"
) -> Receipt:
    op = WRITE.at(target, mode=mode)
    return route(op, session.caps, session.lanes, session.breaker).apply(op, notes)


def clear_notes(session: Routable, target: Target) -> Receipt:
    op = CLEAR.at(target)
    return route(op, session.caps, session.lanes, session.breaker).apply(op, [])
