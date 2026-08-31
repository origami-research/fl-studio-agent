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
    Receipt,
    Routable,
    Target,
    route,
)

WRITE = Operation.spec(
    kind="write_automation",
    requires=Capability.FILE,
    tag=CapabilityTag(
        "automation.write",
        # planned until the flpkit pin ships write_automation (0.5); the
        # provenance net (tests/e2e/test_provenance.py) forces the promotion
        # to "byte" + a fuzz row the moment the writer lands.
        status="planned",
        verified_fl=None,
        evidence="docs/26 D1: opaque trailer, RE via minimal-pair; flpkit 0.5 writer pending pin",
    ),
)


def set_automation(session: Routable, channel: int, points: Sequence[Any]) -> Receipt:
    op = WRITE.at(Target(channel=channel))
    return route(op, session.caps, session.lanes, session.breaker).apply(op, points)
