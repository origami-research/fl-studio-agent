"""timing: how expensive the getter surface is to poll, folded onto each getter.

Times every confirmed getter (``wp_timing``, bounded iters) and folds each id's
median call cost into the matching ``profile.getters`` fact. That median is what
the observer budgets a poll bundle against (docs/27 §5): a getter nobody can
afford to call at a checkpoint is a getter the observer leaves out.
"""

from __future__ import annotations

from dataclasses import replace

from fl_mcp.core.discovery import Command, Response
from fl_mcp.profile import CapabilityProfile

DEFAULT_ITERS = 200


class Timing:
    name = "timing"

    def __init__(self, iters: int = DEFAULT_ITERS) -> None:
        self._iters = iters

    def commands(self, profile: CapabilityProfile) -> list[Command]:
        return [Command("wp_timing", {"iters": self._iters})]

    def fold(self, responses: list[Response], profile: CapabilityProfile) -> CapabilityProfile:
        median_by_id: dict[int, int] = {}
        for response in responses:
            for entry in response.result.get("getters", []):
                median_by_id[int(entry["id"])] = int(entry.get("median_ns", 0))
        if not median_by_id:
            return profile
        updated = tuple(
            replace(g, median_ns=median_by_id[g.id]) if g.id in median_by_id else g
            for g in profile.getters
        )
        return replace(profile, getters=updated)


__all__ = ["DEFAULT_ITERS", "Timing"]
