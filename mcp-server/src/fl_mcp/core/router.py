"""Generic operation routing: an Operation + the evidence -> the Lane that applies it.

This is docs/30 Surface 3, the note-only predicate of session/router.py
generalized to ANY capability. Notes, playlist, automation, levels, params and
mixer all flow through the ONE rule in route(); adding a capability adds an
OpSpec (data) in fl_mcp/ops/ plus, when a lane learns to execute it, an entry
in that lane's served-kinds table - never a routing branch.

The evidence model is unchanged (docs/27 §2, doctrine #54): no env var, no
flag. The best lane is chosen per call from Capabilities (heartbeat + wire
reads, produced by session/router.detect) under the BridgeBreaker's admission.
Both are consumed structurally - the Protocols below - so this module never
imports session, and lanes built on flpkit's codec (G1) or the Transport (G2)
plug in the same way.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Flag, auto
from typing import Any, Literal, Protocol

from fl_mcp import errors

from .provenance import CapabilityTag, register

# -- the capability algebra ---------------------------------------------------


class Capability(Flag):
    """What an operation requires of the lane that serves it. A lane serves an
    op iff it PROVIDES every required bit under the current evidence."""

    # Byte surgery on the saved project suffices (and is required: no live
    # host call exists for this element). Offline-friendly by definition.
    FILE = auto()
    # Must apply inside the running FL session (the element only exists live,
    # e.g. a plugin's parameters).
    LIVE = auto()
    # Must VERIFIABLY land on the op's coordinates. The file lane provides
    # this by construction; the bridge only with channel-match evidence
    # (PROBE_VERDICT Q2: ChanNum is ignored, notes land in the SELECTED
    # channel).
    TARGETED = auto()


def satisfied(requires: Capability, provided: Capability) -> bool:
    """Whether a lane's provided capabilities cover an op's requirement."""
    return (requires & provided) == requires


# -- the operation --------------------------------------------------------------

# The generic write mode (docs/30 Surface 1: codec.patch takes it; the bridge
# wire's AddNotesArgs carries the same values). Same literal as
# schemas.NoteWriteMode - the "Note" prefix there is historical.
Mode = Literal["merge", "replace"]


@dataclass(frozen=True)
class Target:
    """A coordinate in the project. An op sets the axes that exist for it:
    notes/automation at (pattern, channel), levels/params at a channel, mixer
    at a track, playlist at the arrangement (no axes). Routing reads only
    ``channel`` (the one evidence-gated axis); lanes and formats read the
    rest."""

    pattern: int | None = None
    channel: int | None = None
    track: int | None = None


@dataclass(frozen=True)
class Operation:
    """One routable intent: what to do, where, and what serving it requires.
    ``mode`` rides along because both codec.patch (G1) and the bridge wire
    take it; routing never reads it."""

    kind: str  # "write_notes", "write_automation", "set_mixer", ...
    target: Target
    requires: Capability
    mode: Mode = "replace"

    @staticmethod
    def spec(kind: str, requires: Capability, tag: CapabilityTag) -> OpSpec:
        """Declare a capability (fl_mcp/ops/*): the kind + requirement +
        provenance, instantiated per call via .at(). The tag registers in
        core.provenance at import - the op binding IS the claim's one
        source, and a conflicting second claim raises there."""
        return OpSpec(kind=kind, requires=requires, tag=register(tag))


@dataclass(frozen=True)
class OpSpec:
    """A capability's declaration - the data half of the readability
    guarantee (docs/30): reading the spec tells you what the op needs."""

    kind: str
    requires: Capability
    tag: CapabilityTag

    def at(self, target: Target, mode: Mode = "replace") -> Operation:
        return Operation(kind=self.kind, target=target, requires=self.requires, mode=mode)


# -- the evidence, consumed structurally ---------------------------------------


class ProfileCapabilitiesLike(Protocol):
    """The one profile bit routing reads (docs/27 §4)."""

    channel_targeting_live: bool


class CapabilityProfileLike(Protocol):
    capabilities: ProfileCapabilitiesLike


class Capabilities(Protocol):
    """What the evidence says right now (docs/27 §2), computed per operation
    by session/router.detect. Structural: only the fields routing reads."""

    bridge_live: bool
    bridge_channel: int | None
    bridge_heartbeat_at: float | None
    profile: CapabilityProfileLike


class BreakerLike(Protocol):
    """session/router.BridgeBreaker's admission gate (docs/27 §3)."""

    def admit(self, heartbeat_at: float | None) -> bool: ...


@dataclass(frozen=True)
class _DeadEvidence:
    """The evidence as it would read with the live plane gone. route() asks
    each candidate lane whether it could still serve under this view; a lane
    that could not is resting on live evidence, and that is exactly the set
    the breaker gates."""

    profile: CapabilityProfileLike
    bridge_live: bool = False
    bridge_channel: None = None
    bridge_heartbeat_at: None = None


# -- the lane -------------------------------------------------------------------


class Receipt(Protocol):
    """What a lane CONFIRMED (readback, never echo - docs/27 §1)."""

    backend: str


class Lane(Protocol):
    """One way to apply operations (docs/30 Surface 3).

    ``can_serve`` is a PURE predicate over (op, evidence) - route() may
    evaluate it under hypothetical evidence to learn what the service rests
    on. ``apply`` RAISES on failure; it never silently falls through - the
    router decides fallback BEFORE the call (docs/27 §1)."""

    name: str  # "project-write", "bridge"

    def can_serve(self, op: Operation, caps: Capabilities) -> bool: ...

    def apply(self, op: Operation, data: Sequence[Any]) -> Receipt: ...


# What each lane KIND provides, stated once so a concrete lane's can_serve is
# a two-liner: `op.kind in KINDS and satisfied(op.requires, <provides>)`.

# The file lane honors every coordinate by construction and works offline;
# it can never touch a live-only element.
FILE_PROVIDES = Capability.FILE | Capability.TARGETED


def bridge_provides(op: Operation, caps: Capabilities) -> Capability:
    """What the live bridge provides under this evidence: LIVE when the
    heartbeat proves it ticking; TARGETED only when the op's channel
    constraint is verifiably satisfied - the wire-read selected channel
    equals the target (channel targeting is bridge-blind, PROBE_VERDICT Q2) -
    until a profile reports live channel targeting, which widens only this
    predicate."""
    if not caps.bridge_live:
        return Capability(0)
    provided = Capability.LIVE
    if caps.profile.capabilities.channel_targeting_live or (
        op.target.channel is not None and op.target.channel == caps.bridge_channel
    ):
        provided |= Capability.TARGETED
    return provided


# -- the ONE routing rule ---------------------------------------------------------


class CapabilityUnavailable(errors.ToolError):
    """No lane can serve this operation under the current evidence."""

    def __init__(self, op: Operation, caps: Capabilities) -> None:
        requires = ", ".join(c.name.lower() for c in Capability if c.name and c in op.requires)
        super().__init__(
            # errors.py's "no backend can apply this change" category.
            "note_plane_unavailable",
            f"No lane can serve {op.kind} (requires: {requires}; "
            f"bridge {'live' if caps.bridge_live else 'down'}).",
            "Run fl_doctor. A LIVE op needs FL running with the Bridge healthy and its "
            "breaker closed; FILE/TARGETED ops need a project file the agent may rewrite "
            "(python -m fl_mcp.make_agent_project, or FLMCP_AGENT_PROJECT).",
            retryable=True,
        )


def route(
    op: Operation, caps: Capabilities, lanes: Sequence[Lane], breaker: BreakerLike
) -> Lane:
    """The ONE routing rule (docs/30 Surface 3): the first lane that can_serve
    under the evidence, best-capability first (the caller orders ``lanes``),
    breaker-gated.

    Breaker-gated means: a lane whose service RESTS on live evidence - it
    could not serve this op if the bridge were gone - must also be admitted
    by the breaker (one ping round-trip when half-open, a refusal when open).
    A lane that serves regardless (the file lane) needs no admission, so
    routing around a dead or ineligible bridge never costs a ping. There is
    no per-feature branch here and never will be: a new capability changes
    ops/ data, not this function."""
    dead = _DeadEvidence(profile=caps.profile)
    for lane in lanes:
        if not lane.can_serve(op, caps):
            continue
        if lane.can_serve(op, dead) or breaker.admit(caps.bridge_heartbeat_at):
            return lane
    raise CapabilityUnavailable(op, caps)


class Routable(Protocol):
    """What the ops glue reads off a session: fresh per-op evidence, the lane
    stack (best-capability first), and the bridge breaker."""

    caps: Capabilities
    lanes: Sequence[Lane]
    breaker: BreakerLike
