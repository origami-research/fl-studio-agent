"""target: does FHD_AddNotesToPR honour PatNum/ChanNum on this build?

Plants one uniquely-keyed note per (pattern, channel) step (``wp_target``) so a
later file readback can attribute every landed note to exactly one target. The
live host call returns nothing meaningful - FHD_AddNotesToPR is void - so this
pass CANNOT decide the verdict from its own responses. It only plants; the
verdict (``capabilities.pattern_targeting_live`` / ``channel_targeting_live``) is
set by the orchestrator's Cmd+S + flpkit readback, the non-negotiable live-
acceptance gate (#63). ``fold`` therefore carries the profile through unchanged,
by design - see the RUNBOOK's readback step.
"""

from __future__ import annotations

from fl_mcp.core.discovery import Command, Response
from fl_mcp.profile import CapabilityProfile

# The default targeting matrix: two patterns x two channels. Each step plants one
# note; the readback tells us whether PatNum and ChanNum landed it where asked.
DEFAULT_MATRIX = (
    {"pattern": 1, "channel": 0},
    {"pattern": 2, "channel": 0},
    {"pattern": 1, "channel": 1},
    {"pattern": 2, "channel": 1},
)


class Target:
    name = "target"

    def __init__(self, ppq: int = 96, steps: tuple[dict[str, int], ...] = DEFAULT_MATRIX) -> None:
        self._ppq = ppq
        self._steps = steps

    def commands(self, profile: CapabilityProfile) -> list[Command]:
        return [Command("wp_target", {"ppq": self._ppq, "steps": list(self._steps)})]

    def fold(self, responses: list[Response], profile: CapabilityProfile) -> CapabilityProfile:
        return profile


__all__ = ["DEFAULT_MATRIX", "Target"]
