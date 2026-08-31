"""getters: which host getters read the controller surface, and how to call each.

Asks the Bridge to call every curated safe getter with its real convention
(``wp_getters`` - the guarded buffer for the pointer-out time getters, index-only
for the rest) and folds the ones that answered into ``profile.getters``. The
Bridge owns the SAFE calling convention; this pass owns the record of what was
confirmed callable on this build. Timing (median_ns) is filled by the timing pass.
"""

from __future__ import annotations

from dataclasses import replace

from fl_mcp.core.discovery import Command, Response
from fl_mcp.profile import CapabilityProfile, GetterFact

# The Bridge reports each getter's calling convention under these names; map them
# to the profile's convention vocabulary (docs/27 §4 GetterFact.convention).
_CONVENTION = {
    "buffer_in_value": "guarded_buffer",
    "returns_pointer": "returns_pointer",
    "index_only": "index_only",
}


class Getters:
    name = "getters"

    def commands(self, profile: CapabilityProfile) -> list[Command]:
        return [Command("wp_getters")]

    def fold(self, responses: list[Response], profile: CapabilityProfile) -> CapabilityProfile:
        facts: dict[int, GetterFact] = {}
        for response in responses:
            for entry in response.result.get("results", []):
                if "skipped" in entry:
                    continue
                dispatch_id = int(entry["id"])
                # One id (e.g. FHD_GetContextInfo) backs several getter probes;
                # the first confirmed call is enough to record the id callable.
                facts.setdefault(
                    dispatch_id,
                    GetterFact(
                        id=dispatch_id,
                        name=str(entry.get("getter", f"FHD_{dispatch_id}")),
                        convention=_CONVENTION.get(str(entry.get("call", "")), "index_only"),
                        median_ns=0,
                    ),
                )
        if not facts:
            return profile
        return replace(profile, getters=tuple(facts[i] for i in sorted(facts)))


__all__ = ["Getters"]
