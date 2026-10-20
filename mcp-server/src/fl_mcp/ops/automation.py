"""Automation points on a channel's clip: FILE-only byte surgery - no live
automation host call exists (PROBE Q). The docs/30 worked trace, verbatim:
this file plus flpkit/formats/automation.py IS the capability."""

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

WRITE = Operation.spec(
    kind="write_automation",
    requires=Capability.FILE,
    tag=CapabilityTag(
        "automation.write",
        status="byte",
        verified_fl="26.1.5",
        evidence="flpkit 0.6 write_automation: differential goldens + 1,100-blob corpus "
        "byte-identity; fuzz row in tests/e2e/test_roundtrip_fuzz.py",
    ),
)


def set_automation(session: Routable, channel: int, points: Sequence[Any]) -> Any:
    op = WRITE.at(Target(channel=channel))
    return route(op, session.caps, session.lanes, session.breaker).apply(op, points)
