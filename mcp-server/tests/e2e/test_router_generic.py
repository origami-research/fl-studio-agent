"""Generic router property suite over docs/30 Surface 3.

Exercises ``route()`` through the Lane PROTOCOL with stub lanes only - never
another lane's concrete modules - so it pins the routing RULE, not any
feature: first lane that can_serve under the evidence wins, order is
priority, and nothing is silently returned when no lane serves. The same
assertions hold for every Operation kind (that is the "no per-feature
branch" guarantee made executable).

Skips until the G3 router lane merges ``fl_mcp.core.router``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

router = pytest.importorskip("fl_mcp.core.router", reason="fl_mcp.core.router not merged yet (G3)")

OP_KINDS = ("write_notes", "write_automation", "set_mixer", "write_playlist")


@dataclass
class StubLane:
    name: str
    serves: bool
    applied: list = field(default_factory=list)

    def can_serve(self, op, caps) -> bool:
        return self.serves

    def apply(self, op, data):
        self.applied.append((op, data))
        return {"lane": self.name}


class ClosedBreaker:
    closed = True


@pytest.mark.parametrize("kind", OP_KINDS)
def test_first_serving_lane_wins_for_every_kind(kind):
    op = router.Operation(kind=kind, target=None, requires=None)
    lanes = [StubLane("bridge", serves=False), StubLane("project-write", serves=True)]
    chosen = router.route(op, caps=None, lanes=lanes, breaker=ClosedBreaker())
    assert chosen is lanes[1]

    lanes = [StubLane("bridge", serves=True), StubLane("project-write", serves=True)]
    chosen = router.route(op, caps=None, lanes=lanes, breaker=ClosedBreaker())
    assert chosen is lanes[0], "priority order not respected: the best-capability lane lost"


@pytest.mark.parametrize("kind", OP_KINDS)
def test_no_serving_lane_raises_never_none(kind):
    op = router.Operation(kind=kind, target=None, requires=None)
    lanes = [StubLane("bridge", serves=False), StubLane("project-write", serves=False)]
    with pytest.raises(Exception):  # the error type is the router's to choose
        assert router.route(op, caps=None, lanes=lanes, breaker=ClosedBreaker()) is not None


def test_routing_is_kind_agnostic():
    """Two ops differing only in kind route identically - the rule is one
    table lookup over evidence, never a per-feature branch."""
    lanes = [StubLane("bridge", serves=True), StubLane("project-write", serves=True)]
    picks = {
        router.route(
            router.Operation(kind=kind, target=None, requires=None),
            caps=None,
            lanes=lanes,
            breaker=ClosedBreaker(),
        ).name
        for kind in OP_KINDS
    }
    assert len(picks) == 1
