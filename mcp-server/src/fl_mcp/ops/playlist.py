"""Playlist clips: FILE-only byte surgery (flpkit 0.4, FL-verified). No live
playlist host call is known; if discovery finds one, only this spec's
``requires`` and a lane's served-kinds table change."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

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

WRITE = Operation.spec(
    kind="write_playlist",
    requires=Capability.FILE,
    tag=CapabilityTag(
        "playlist.write",
        status="live",
        verified_fl="26.1.5",
        evidence="e2e matrix tier 2 (FL readback); flpkit write_playlist FL-2026 live-verified",
    ),
)


def write_playlist(session: Routable, clips: Sequence[Any], mode: Mode = "replace") -> Receipt:
    # Arrangement-level: clips carry their own track/position, so the target
    # names no finer coordinate.
    op = WRITE.at(Target(), mode=mode)
    return route(op, session.caps, session.lanes, session.breaker).apply(op, clips)
