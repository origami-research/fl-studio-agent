"""Provenance: how and when every capability was verified (docs/30 Surface 5).

A ``CapabilityTag`` travels with each capability so no result ever claims
more than its evidence: ``status`` says HOW it was verified, ``verified_fl``
says against WHICH FL, ``evidence`` says where to read the proof. The
registry is the queryable index - the coverage map (docs/24) and the scene
consult it instead of restating verification facts in prose, and an op
binding (``fl_mcp/ops/*``) registers its tag at import.

Statuses, strongest first:
  live    - confirmed against a running FL Studio (closed loop, FL's readback)
  byte    - byte-exact against FL-written files, no live confirmation
  oracle  - agrees with an external decoder (e.g. pyflp) only
  planned - declared but not yet verified at all
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["live", "byte", "oracle", "planned"]


@dataclass(frozen=True)
class CapabilityTag:
    capability: str  # dotted, e.g. "notes.write.pattern"
    status: Status
    verified_fl: str | None  # FL version last confirmed against (profile meta format, "26.1.5")
    evidence: str  # runbook / verdict / test reference a reader can open

    def stale(self, current_fl: str) -> bool:
        """Verified once, but not against the FL we are running now."""
        return self.verified_fl is not None and self.verified_fl != current_fl


_REGISTRY: dict[str, CapabilityTag] = {}


def register(tag: CapabilityTag) -> CapabilityTag:
    """Idempotent for identical tags; two different claims for one capability
    is a wiring bug and raises rather than letting one silently win."""
    existing = _REGISTRY.get(tag.capability)
    if existing is not None and existing != tag:
        raise ValueError(f"conflicting tags for {tag.capability!r}: {existing} vs {tag}")
    _REGISTRY[tag.capability] = tag
    return tag


def lookup(capability: str) -> CapabilityTag | None:
    return _REGISTRY.get(capability)


def tags() -> tuple[CapabilityTag, ...]:
    """Every registered tag, sorted by capability name (stable output for
    the coverage map and the scene)."""
    return tuple(_REGISTRY[name] for name in sorted(_REGISTRY))


# What we can prove TODAY, seeded from the coverage map (docs/24) and the
# probe verdict. Each entry is data; an op binding may re-register the same
# tag (idempotent) and MUST update it here when its evidence changes.
for _tag in (
    CapabilityTag("notes.read", "byte", "26.1.5", "docs/24; flpkit corpus suite (no live getter: PROBE_VERDICT_2026-08-30)"),
    CapabilityTag("notes.write.selected", "live", "26.1.5", "#65 FL-save-confirmed 2026-08-29"),
    CapabilityTag("notes.write.pattern", "live", "26.1.5", "PROBE_VERDICT_2026-08-30: PatNum honored"),
    CapabilityTag("notes.write.channel", "byte", "26.1.5", "PROBE_VERDICT_2026-08-30: ChanNum ignored; file lane + e2e matrix"),
    CapabilityTag("playlist.read", "byte", "26.1.5", "docs/24; e2e matrix (80/32-byte strides)"),
    CapabilityTag("playlist.write", "live", "26.1.5", "e2e matrix tier 2 (FL readback via tempo/window controls)"),
    CapabilityTag("automation.read", "byte", "26.1.5", "docs/24; flpkit 0.3"),
    CapabilityTag("automation.write", "planned", None, "docs/26 D1: opaque trailer, RE via minimal-pair"),
    CapabilityTag("levels.write", "live", "26.1.5", "e2e matrix tier 2 channel volume/pan readback"),
    CapabilityTag("tempo.set", "live", "26.1.5", "control-plane e2e round-trip"),
    CapabilityTag("params.set", "live", "26.1.5", "#50 wire read/set"),
    CapabilityTag("mixer.route", "planned", None, "docs/24: opaque; RE file + host-call probe"),
    CapabilityTag("render.full_mix", "live", "26.1.5", "experiment E4 (OsxFL -R) + e2e render regression"),
    CapabilityTag("scene.read", "byte", "26.1.5", "get_scene over the corpus"),
    CapabilityTag("observe.user_actions", "live", "26.1.5", "PROBE_VERDICT_2026-08-30: push callbacks confirmed"),
):
    register(_tag)
