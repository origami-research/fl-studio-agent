"""Mixer track state (name/color/volume/pan/mute): LIVE via the bridge's
FHD_SetContextInfo path (docs/26 D2, planned). Routing needs nothing new when
it lands - the bridge adds "set_mixer" to its served-kinds table and this
tag's status moves planned -> live."""

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
    kind="set_mixer",
    requires=Capability.LIVE,
    tag=CapabilityTag(
        "mixer.set",
        status="planned",
        verified_fl=None,
        evidence="docs/26 D2 (FHD_SetContextInfo live path; file RE for routing)",
    ),
)


def set_mixer(session: Routable, track: int, values: Sequence[Any]) -> Any:
    op = SET.at(Target(track=track))
    return route(op, session.caps, session.lanes, session.breaker).apply(op, values)
