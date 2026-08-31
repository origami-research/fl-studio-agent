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
    Receipt,
    Routable,
    Target,
    route,
)

SET = Operation.spec(
    kind="set_levels",
    requires=Capability.TARGETED,
    tag=CapabilityTag(
        "levels.set",
        status="byte",
        verified_fl="26.1.5",
        evidence="docs/24 map (flpkit set_channel_levels; live wire path in session/controls)",
    ),
)


def set_levels(session: Routable, channel: int, levels: Sequence[Any]) -> Receipt:
    op = SET.at(Target(channel=channel))
    return route(op, session.caps, session.lanes, session.breaker).apply(op, levels)
