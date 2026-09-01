"""Discovery passes: the DATA the generic runner (core.discovery) executes.

Each module is one probe stated as a spec - what it asks FL, what it learns. A
new probe is a new module dropped into :data:`DEFAULT_PASSES`, never a branch in
the runner. The sequence reads top-to-bottom as the discovery itself: align the
scraped table to the SDK map (so we know what changed and what is genuinely new),
report plugin-operation candidates, confirm the getters, time them, test note
targeting, then sweep only the ids nothing else explained.
"""

from __future__ import annotations

from .getters import Getters
from .map_align import Alignment, MapAlign, classify, unknown_ids
from .plugins import (
    PluginActionTrace,
    PluginCandidates,
    Plugins,
    TraceExpectation,
    action_trace,
    classify_plugins,
)
from .sweep import Sweep
from .target import Target
from .timing import Timing

DEFAULT_PASSES = (MapAlign(), Plugins(), Getters(), Timing(), Target(), Sweep())

__all__ = [
    "DEFAULT_PASSES",
    "Alignment",
    "Getters",
    "MapAlign",
    "PluginActionTrace",
    "PluginCandidates",
    "Plugins",
    "Sweep",
    "Target",
    "Timing",
    "TraceExpectation",
    "action_trace",
    "classify",
    "classify_plugins",
    "unknown_ids",
]
