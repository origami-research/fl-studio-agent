"""map_align: align FL's scraped jump table to the SDK's command map (#63).

The dispatcher's jump table is what FL 2026 ACTUALLY handles per id; the SDK
header is what a command id is SUPPOSED to mean. This pass aligns the two and
classifies every id:

- **matched** - documented AND handled: the SDK command is where it should be.
- **moved** - documented but now inert (the jump table sends it to the default
  branch): a documented command's handler disappeared on this build. The smoke
  alarm - a silent format/ABI shift, surfaced as a named finding.
- **new** - handled but undocumented: a real handler at an id the SDK never
  described. These are the genuinely-new ids, and the ONLY input the live sweep
  needs.

The scraped jump table is carried in the profile as the dispatcher facts the
static scraper produced: ``handled_max_id`` (the ``cmp x0,#imm`` bound) and
``inert_in_range`` (in-range ids that hit the default branch). The SDK map is
:data:`SDK_DISPATCH` below - the documented FHD_* command ids, the stable SDK
contract this build is measured against (NOT a per-build constant: divergence
from it is precisely what "moved"/"new" report).

:func:`unknown_ids` is why this shrinks the risky sweep: it returns the ``new``
ids minus everything the profile already explains (known getters, known-unsafe
ids), so the sweep only ever dispatches ids nothing can yet account for. As
discovery folds each finding back into the profile, that set shrinks toward
empty - a converged FL build sweeps nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from fl_mcp.core.discovery import Command, Response
from fl_mcp.profile import CapabilityProfile

# The SDK's documented dispatcher command map (the plugin API's FHD_* constants). Its KEYS
# are the documented id range - the boundary this build is aligned against; its
# VALUES label findings in the report. The twin of native/abi's
# fl_dispatch_ledger.h kDocumentedMaxId=87: the SDK documents ids 0..87, so any
# handled id past 87 is undocumented by construction. Names are the authoritative
# labels the repo already carries (native ledger + profile getters); unnamed
# documented ids get an "FHD_<id>" placeholder so the domain stays complete
# without inventing a meaning.
_NAMED = {
    7: "FHD_LocatePlugin",
    10: "FHD_SetNewName",
    14: "FHD_LocateDataFile",
    15: "FHD_ShowPlugSelector",
    17: "FHD_AddNotesToPR",
    18: "FHD_GetParamMenuEntry",
    19: "FHD_MsgBox",
    28: "FHD_PackDataFile",
    35: "FHD_SetUID",
    36: "FHD_GetMixingTime",
    37: "FHD_GetPlaybackTime",
    38: "FHD_GetSelTime",
    39: "FHD_GetTimeMul",
    41: "FHD_SendSysEx",
    42: "FHD_LoadAudioClip",
    43: "FHD_LoadInChannel",
    44: "FHD_ShowInBrowser",
    45: "FHD_DebugLogMsg",
    49: "FHD_AddToRecent",
    51: "FHD_GetInName",
    52: "FHD_GetOutName",
    53: "FHD_ShowEditor",
    55: "FHD_ShowSettings",
    57: "FHD_ShowPicker",
    60: "FHD_RenderProject",
    61: "FHD_GetProjectInfo",
    66: "FHD_RenderWindowBitmap",
    70: "FHD_OpenTool",
    74: "FHD_ReportError",
    75: "FHD_ShowStandardParamMenu",
    76: "FHD_GetContextInfo",
    77: "FHD_SetContextInfo",
    83: "FHD_PickVoiceColor",
    84: "FHD_GetColorRGBValue",
    85: "FHD_ShowException",
    86: "FHD_GetTranslationMoFile",
}
DOCUMENTED_MAX_ID = 87
SDK_DISPATCH = {i: _NAMED.get(i, f"FHD_{i}") for i in range(DOCUMENTED_MAX_ID + 1)}


@dataclass(frozen=True)
class Alignment:
    """The scraped table classified against the SDK map. Names come from SDK_DISPATCH."""

    matched: tuple[int, ...]  # documented & handled - the command is where it should be
    moved: tuple[int, ...]  # documented & now inert - a handler disappeared
    new: tuple[int, ...]  # handled & undocumented - genuinely new ids

    def name(self, dispatch_id: int) -> str:
        return SDK_DISPATCH.get(dispatch_id, f"undocumented_{dispatch_id}")


def _is_handled(profile: CapabilityProfile, dispatch_id: int) -> bool:
    """Does the jump table route this id to a real handler (not the default branch)?"""
    d = profile.dispatcher
    return dispatch_id <= d.handled_max_id and dispatch_id not in d.inert_in_range


def classify(profile: CapabilityProfile) -> Alignment:
    """Align the scraped jump table (profile.dispatcher) to :data:`SDK_DISPATCH`."""
    matched, moved, new = [], [], []
    for dispatch_id in range(profile.dispatcher.handled_max_id + 1):
        documented = dispatch_id <= DOCUMENTED_MAX_ID
        handled = _is_handled(profile, dispatch_id)
        if documented and handled:
            matched.append(dispatch_id)
        elif documented and not handled:
            moved.append(dispatch_id)
        elif handled:  # undocumented & handled
            new.append(dispatch_id)
        # undocumented & inert: an empty slot; carries no information.
    return Alignment(tuple(matched), tuple(moved), tuple(new))


def unknown_ids(profile: CapabilityProfile) -> list[int]:
    """The ``new`` ids the profile cannot yet explain - the live sweep's whole input.

    A ``new`` id already recorded as a known getter or a known-unsafe fatal needs
    no re-probe, so it is excluded. This is the set the sweep dispatches; it
    shrinks to empty as findings are folded back into the profile.
    """
    known = set(profile.dispatcher.unsafe) | {g.id for g in profile.getters}
    return [dispatch_id for dispatch_id in classify(profile).new if dispatch_id not in known]


class MapAlign:
    """A static pass: it reasons over the scraped profile, asking FL nothing.

    It issues no commands and folds no new facts - its output is the
    classification (:func:`classify`) the report reads and the unknowns
    (:func:`unknown_ids`) the sweep consumes. It sits in the discovery sequence
    so the run has one obvious place where "what changed on this build" is decided.
    """

    name = "map_align"

    def commands(self, profile: CapabilityProfile) -> list[Command]:
        return []

    def fold(self, responses: list[Response], profile: CapabilityProfile) -> CapabilityProfile:
        return profile


__all__ = ["DOCUMENTED_MAX_ID", "SDK_DISPATCH", "Alignment", "MapAlign", "classify", "unknown_ids"]
