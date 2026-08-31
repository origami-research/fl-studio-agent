"""sweep: dispatch the genuinely-new ids and fold live-learned fatals into the profile.

The sweep is the one risky pass - an undocumented id can crash or hang FL. It is
kept small and safe by construction: its command carries ONLY
:func:`~fl_mcp.passes.map_align.unknown_ids` (the handled, undocumented ids the
profile cannot yet explain), and the Bridge skips every documented, ledger-fatal,
or quarantined id inside that list anyway. When the unknown set is empty (a
converged build) it issues nothing.

``fold`` closes the crash-resume loop into the profile: any id the Bridge
reported SKIPPED for a fatal reason - a static ledger fatal, or an id the native
quarantine ledger learned the expensive way on a prior crashed run - is promoted
into ``profile.dispatcher.unsafe``. So a new build's crashers flow into the
profile as data, without editing the C++ ledger by hand (docs/research r4 A3,
#92). Surviving ids that returned a value are left for the report; the live-
acceptance judgement of what they MEAN stays human (#63).
"""

from __future__ import annotations

from dataclasses import replace

from fl_mcp.core.discovery import Command, Response
from fl_mcp.profile import CapabilityProfile, UnsafeFact

from .map_align import unknown_ids

# Bridge skip reasons that mean "this id is fatal to call blind". A statically
# ledgered fatal reports its hazard name; a dynamically quarantined crasher (an
# id a prior run crashed in flight) reports "quarantined" - we know it took FL
# down, not whether by crash or hang, so it lands as the conservative crash.
_FATAL_HAZARD = {
    "crashes_host": "crashes_host",
    "hangs_host": "hangs_host",
    "quarantined": "crashes_host",
}


class Sweep:
    name = "sweep"

    def commands(self, profile: CapabilityProfile) -> list[Command]:
        ids = unknown_ids(profile)
        if not ids:
            return []
        return [Command("wp_sweep", {"ids": ids})]

    def fold(self, responses: list[Response], profile: CapabilityProfile) -> CapabilityProfile:
        unsafe = dict(profile.dispatcher.unsafe)
        changed = False
        for response in responses:
            for entry in response.result.get("results", []):
                hazard = _FATAL_HAZARD.get(str(entry.get("skipped", "")))
                dispatch_id = int(entry["id"])
                if hazard is None or dispatch_id in unsafe:
                    continue
                unsafe[dispatch_id] = UnsafeFact(
                    hazard=hazard,
                    evidence="wp_sweep skip + native/abi/fl_dispatch_ledger.h (live-learned fatal)",
                )
                changed = True
        if not changed:
            return profile
        return replace(profile, dispatcher=replace(profile.dispatcher, unsafe=unsafe))


__all__ = ["Sweep"]
