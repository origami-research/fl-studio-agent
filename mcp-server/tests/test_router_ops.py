"""core/router.route over EVERY op kind through ONE table (docs/30 Surface 3),
plus the ops layer building correct Operations. No per-op branch anywhere: a
row is (spec, evidence, breaker verdict, expected lane); the rule under test
is the same for all of them."""

from types import SimpleNamespace

import pytest

from fl_mcp.core.router import (
    FILE_PROVIDES,
    Capability,
    CapabilityUnavailable,
    Target,
    bridge_provides,
    route,
    satisfied,
)
from fl_mcp.ops import automation, levels, mixer, notes, params, playlist
from fl_mcp.schemas import Note

# -- fakes: lanes are (name, served kinds, provides) - exactly the data a real
# lane binds; the bridge's kinds table includes the wp_* future (set_params /
# set_mixer) to prove new live capabilities need no new routing rule.

BRIDGE_KINDS = frozenset({"write_notes", "clear_notes", "set_params", "set_mixer"})
FILE_KINDS = frozenset({"write_notes", "clear_notes", "write_playlist", "write_automation", "set_levels"})


class FakeLane:
    def __init__(self, name, kinds, provides):
        self.name = name
        self._kinds = kinds
        self._provides = provides
        self.applied = []

    def can_serve(self, op, caps):
        return op.kind in self._kinds and satisfied(op.requires, self._provides(op, caps))

    def apply(self, op, data):
        self.applied.append((op, list(data)))
        return SimpleNamespace(backend=self.name)


def bridge_lane():
    return FakeLane("bridge", BRIDGE_KINDS, bridge_provides)


def file_lane():
    return FakeLane("project-write", FILE_KINDS, lambda op, caps: FILE_PROVIDES)


class FakeBreaker:
    """verdict None = the breaker must NOT be consulted for this row."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def admit(self, heartbeat_at):
        assert self.verdict is not None, "breaker consulted for a lane not resting on live evidence"
        self.calls += 1
        return self.verdict


NO_TARGETING = SimpleNamespace(capabilities=SimpleNamespace(channel_targeting_live=False))
LIVE_TARGETING = SimpleNamespace(capabilities=SimpleNamespace(channel_targeting_live=True))


def caps(**overrides):
    base = {
        "bridge_live": True,
        "bridge_channel": 0,
        "bridge_heartbeat_at": 12345.0,
        "profile": NO_TARGETING,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


TARGET = Target(pattern=1, channel=0, track=0)

# -- THE table: every op kind x evidence -> lane. expected None = no lane can
# serve (route raises). verdict None = the ping gate must never fire.

TABLE = [
    pytest.param(notes.WRITE, {}, True, "bridge", id="notes.write/full-evidence"),
    pytest.param(notes.WRITE, {}, False, "project-write", id="notes.write/breaker-refuses"),
    pytest.param(notes.WRITE, {"bridge_channel": 3}, None, "project-write", id="notes.write/chan-mismatch"),
    pytest.param(notes.WRITE, {"bridge_live": False}, None, "project-write", id="notes.write/bridge-down"),
    pytest.param(
        notes.WRITE,
        {"bridge_channel": None, "profile": LIVE_TARGETING},
        True,
        "bridge",
        id="notes.write/profile-widens-targeting",
    ),
    pytest.param(notes.CLEAR, {}, True, "bridge", id="notes.clear/full-evidence"),
    pytest.param(notes.CLEAR, {"bridge_live": False}, None, "project-write", id="notes.clear/bridge-down"),
    pytest.param(playlist.WRITE, {}, None, "project-write", id="playlist.write/file-even-when-live"),
    pytest.param(automation.WRITE, {}, None, "project-write", id="automation.write/file-even-when-live"),
    pytest.param(automation.WRITE, {"bridge_live": False}, None, "project-write", id="automation.write/offline"),
    pytest.param(levels.SET, {}, None, "project-write", id="levels.set/bridge-kind-unserved"),
    pytest.param(params.SET, {}, True, "bridge", id="params.set/live"),
    pytest.param(params.SET, {"bridge_live": False}, None, None, id="params.set/no-live-lane-raises"),
    pytest.param(params.SET, {}, False, None, id="params.set/breaker-refuses-no-fallback"),
    pytest.param(mixer.SET, {}, True, "bridge", id="mixer.set/live"),
    pytest.param(mixer.SET, {"bridge_live": False}, None, None, id="mixer.set/no-live-lane-raises"),
]


@pytest.mark.parametrize("spec,overrides,verdict,expected", TABLE)
def test_route_picks_per_capability_and_evidence(spec, overrides, verdict, expected):
    lanes = [bridge_lane(), file_lane()]
    breaker = FakeBreaker(verdict)
    op = spec.at(TARGET)
    if expected is None:
        with pytest.raises(CapabilityUnavailable):
            route(op, caps(**overrides), lanes, breaker)
    else:
        assert route(op, caps(**overrides), lanes, breaker).name == expected


def test_route_pings_at_most_once_for_the_admitted_lane():
    breaker = FakeBreaker(True)
    route(notes.WRITE.at(TARGET), caps(), [bridge_lane(), file_lane()], breaker)
    assert breaker.calls == 1


# -- the ops layer builds correct Operations: one table, a recording lane, no
# branches. The recording lane serves unconditionally (so it is not
# live-contingent and the breaker must stay silent - FakeBreaker(None)).

NOTE = Note(key=60, start=0.0, length=1.0)

GLUE = [
    pytest.param(
        notes.write_notes,
        (Target(pattern=2, channel=1), [NOTE], "merge"),
        ("write_notes", Capability.TARGETED, Target(pattern=2, channel=1), "merge", [NOTE]),
        id="notes.write_notes",
    ),
    pytest.param(
        notes.clear_notes,
        (Target(pattern=2, channel=1),),
        ("clear_notes", Capability.TARGETED, Target(pattern=2, channel=1), "replace", []),
        id="notes.clear_notes",
    ),
    pytest.param(
        playlist.write_playlist,
        ([{"clip": 1}],),
        ("write_playlist", Capability.FILE, Target(), "replace", [{"clip": 1}]),
        id="playlist.write_playlist",
    ),
    pytest.param(
        automation.set_automation,
        (3, [(0.0, 0.5)]),
        ("write_automation", Capability.FILE, Target(channel=3), "replace", [(0.0, 0.5)]),
        id="automation.set_automation",
    ),
    pytest.param(
        levels.set_levels,
        (4, [{"volume": 0.8}]),
        ("set_levels", Capability.TARGETED, Target(channel=4), "replace", [{"volume": 0.8}]),
        id="levels.set_levels",
    ),
    pytest.param(
        params.set_params,
        (5, [(12, 0.3)]),
        ("set_params", Capability.LIVE, Target(channel=5), "replace", [(12, 0.3)]),
        id="params.set_params",
    ),
    pytest.param(
        mixer.set_mixer,
        (6, [{"volume": 0.7}]),
        ("set_mixer", Capability.LIVE, Target(track=6), "replace", [{"volume": 0.7}]),
        id="mixer.set_mixer",
    ),
]


@pytest.mark.parametrize("glue,args,expected", GLUE)
def test_ops_build_correct_operations(glue, args, expected):
    lane = FakeLane("recorder", {expected[0]}, lambda op, caps: op.requires)
    session = SimpleNamespace(caps=caps(), lanes=[lane], breaker=FakeBreaker(None))
    receipt = glue(session, *args)
    kind, requires, target, mode, data = expected
    (op, applied), = lane.applied
    assert (op.kind, op.requires, op.target, op.mode) == (kind, requires, target, mode)
    assert applied == data
    assert receipt.backend == "recorder"


ALL_SPECS = [notes.WRITE, notes.CLEAR, playlist.WRITE, automation.WRITE, levels.SET, params.SET, mixer.SET]


def test_every_spec_carries_provenance():
    for spec in ALL_SPECS:
        assert spec.tag.capability, spec.kind
        assert spec.tag.status in {"live", "byte", "oracle", "planned"}, spec.kind
        assert spec.tag.evidence, spec.kind
        # G5 stale() semantics: verified-once means stale against another FL;
        # never-verified (planned) is not stale - it is simply unverified.
        assert spec.tag.stale("99.9.9") is (spec.tag.verified_fl is not None)
        if spec.tag.verified_fl is not None:
            assert spec.tag.stale(spec.tag.verified_fl) is False


def test_route_fits_the_real_session_evidence_and_breaker():
    """The Protocols are structural claims about session/router's concrete
    types; this proves the manager's rewiring is purely mechanical."""
    from fl_mcp.session.router import BridgeBreaker, Capabilities

    evidence = Capabilities(
        bridge_live=True,
        bridge_pattern=None,
        bridge_channel=0,
        profile=NO_TARGETING,
        bridge_heartbeat_at=12345.0,
    )
    breaker = BridgeBreaker(lambda: True)
    op = notes.WRITE.at(Target(pattern=1, channel=0), mode="merge")
    assert route(op, evidence, [bridge_lane(), file_lane()], breaker).name == "bridge"
    assert route(automation.WRITE.at(Target(channel=2)), evidence, [bridge_lane(), file_lane()], breaker).name == "project-write"
