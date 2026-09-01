"""plugins: identify plugin-operation dispatcher candidates without calling them.

The SDK-aligned dispatcher table has no documented programmatic command for
adding a generator, adding an insert effect, or loading a preset.  In
particular, ``FHD_ShowPlugSelector`` is a modal UI command, not an automation
API.  This pass makes that negative result a discovery artifact instead of
turning an undocumented handler into a speculative writer.

For each operation, ``matched`` and ``moved`` are necessarily empty until an
SDK command is documented for it. ``new`` contains only handled,
undocumented, non-fatal ids.  They are candidates for *static decoding*, not
commands to issue.  The bridge's fatal ledger remains the execution gate; this
pass never emits a command.
"""

from __future__ import annotations

from dataclasses import dataclass

from fl_mcp.core.discovery import Command, Response
from fl_mcp.profile import CapabilityProfile

from .map_align import classify

# Keep this Python discovery report conservative even when it is run against a
# profile generated before the native ledger learned id 106.  This is the
# live-confirmed fatal subset of native/abi/fl_dispatch_ledger.h.
FATAL_IDS = frozenset((94, 99, 106))


@dataclass(frozen=True)
class OperationCandidates:
    """Dispatcher ids classified for one requested plugin operation.

    A ``new`` id is unproven. It names static-disassembly work only and is
    deliberately not executable by this pass or by a normal Bridge operation.
    """

    matched: tuple[int, ...]
    moved: tuple[int, ...]
    new: tuple[int, ...]


@dataclass(frozen=True)
class PluginCandidates:
    """The discovery-output map for the three plugin host capabilities."""

    add_instrument: OperationCandidates
    add_effect: OperationCandidates
    load_preset: OperationCandidates


def classify_plugins(profile: CapabilityProfile) -> PluginCandidates:
    """Classify safe-to-decode unknown handlers for plugin capabilities.

    The SDK supplies no documented ids for these operations, so no id can be a
    matched (or moved) programmatic plugin call. Every remaining undocumented
    handler is reported as an unproven candidate for offline analysis only.
    """
    known_fatal = FATAL_IDS | set(profile.dispatcher.unsafe)
    unproven = tuple(dispatch_id for dispatch_id in classify(profile).new if dispatch_id not in known_fatal)
    candidates = OperationCandidates(matched=(), moved=(), new=unproven)
    return PluginCandidates(candidates, candidates, candidates)


class Plugins:
    """A static plugin-capability pass: reports candidates and never probes FL."""

    name = "plugins"

    def commands(self, profile: CapabilityProfile) -> list[Command]:
        return []

    def fold(self, responses: list[Response], profile: CapabilityProfile) -> CapabilityProfile:
        return profile


__all__ = [
    "FATAL_IDS",
    "OperationCandidates",
    "PluginCandidates",
    "Plugins",
    "classify_plugins",
]
