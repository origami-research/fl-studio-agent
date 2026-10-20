"""Channel levels (volume/pan/pitch): TARGETED writes to a channel. The file
lane serves them by byte surgery (flpkit set_channel_levels); a live control
lane that lands them by construction (the wire) outranks it when registered."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fl_mcp.core.router import (
    Capability,
    CapabilityTag,
    Operation,
    Routable,
    Target,
    route,
)

SET = Operation.spec(
    kind="set_levels",
    requires=Capability.TARGETED,
    tag=CapabilityTag(
        "levels.write",
        status="live",
        verified_fl="26.1.5",
        evidence="e2e matrix tier 2 volume/pan readback (wire); flpkit set_channel_levels (byte)",
    ),
)


def set_levels(session: Routable, channel: int, levels: Sequence[Any]) -> Any:
    op = SET.at(Target(channel=channel))
    return route(op, session.caps, session.lanes, session.breaker).apply(op, levels)
