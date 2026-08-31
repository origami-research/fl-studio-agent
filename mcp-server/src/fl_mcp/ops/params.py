"""Plugin parameters: LIVE-only - a generator's parameters exist only inside
the running plugin; there is no file fallback (the plugin blob is opaque).
When no live lane can serve, route() raises rather than pretending."""

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
    kind="set_params",
    requires=Capability.LIVE,
    tag=CapabilityTag(
        "params.set",
        status="live",
        verified_fl="26.1.5",
        evidence="#50; E2-verified 2026-08-29 (schemas.PluginParam)",
    ),
)


def set_params(session: Routable, channel: int, params: Sequence[Any]) -> Receipt:
    """``params`` are (index, value) pairs, values normalized 0..1."""
    op = SET.at(Target(channel=channel))
    return route(op, session.caps, session.lanes, session.breaker).apply(op, params)
