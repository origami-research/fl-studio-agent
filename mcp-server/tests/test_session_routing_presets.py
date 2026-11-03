"""Reshaping a project's sounds live: mixer routing, sends, presets and stock sounds.

The controller is scripted with the answers FL 2026 gave live (controller API
45, docs/research/routing-presets-live-2026-11-02.md): routing and sends read
back exactly, a send needs afterRoutingChanged, FL's default send level is
0.8, stepping a preset renames the channel (the only preset readback), and a
sampler answers plugin calls with "Plugin not valid". Each test names the rule
it holds: a result is FL's readback, nothing is written before an argument is
known good, and a call that stops partway says what already landed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from session_fakes import HELLO, FakeControl, make_session, snapshot_answer

from fl_mcp import errors, flp, sdk
from fl_mcp.actions import ActionLog
from fl_mcp.kernel import wire
from fl_mcp.schemas import ChannelPreset, ChannelRoute, ControlResponse, MixerSend, Sound
from fl_mcp.session import WriteRejected
from fl_mcp.session import facade as facade_mod

MIXER_TRACKS = 127  # a full mixer as mixer.trackCount reports it, the master included
DEFAULT_SEND_LEVEL = 0.8  # FL's level for a send just turned on (live)
FLEX_PRESETS = ["Default", "Analog Bass", "Bright Pluck", "Deep Sub", "Glass Keys", "Hollow Lead", "Pure Feather"]
# The saved song's channels; FL's live names follow the plugin's preset steps.
SAVED_CHANNELS = tuple(
    flp.FlpChannel(index=i, name=name, volume=10000, pan=6400, pitch_semitones=0)
    for i, name in enumerate(["808 Kick", "FLEX Bass"])
)


def refused(detail: str) -> ControlResponse:
    return ControlResponse(id=1, ok=False, err=detail)


class FakeMixer:
    """FL's mixer routing as its controller answers it."""

    def __init__(self, tracks: int = MIXER_TRACKS) -> None:
        self.tracks = tracks
        self.targets: dict[int, int] = {0: 1, 1: 2}
        self.sends: dict[tuple[int, int], bool] = {}
        self.levels: dict[tuple[int, int], float] = {}
        self.routing_applied = True

    def handlers(self) -> dict[str, Any]:
        def set_target(args):
            channel, track = args
            self.targets[channel] = track

        def set_route(args):
            source, target, on = args
            self.sends[(source, target)] = on
            self.routing_applied = False  # FL shows it only after afterRoutingChanged

        def routing_changed(args):
            self.routing_applied = True

        def set_level(args):
            source, target, level = args
            self.levels[(source, target)] = level

        def send_active(args):
            return int(self.routing_applied and self.sends.get(tuple(args), False))

        def route_level(args):
            # Live, FL answered "Index out of range" for the level of a send that is off.
            if not send_active(args):
                return refused("RuntimeError: Index out of range")
            return self.levels.get(tuple(args), DEFAULT_SEND_LEVEL)

        return {
            "mixer.trackCount": lambda args: self.tracks,
            "channels.setTargetFxTrack": set_target,
            "channels.getTargetFxTrack": lambda args: self.targets[args[0]],
            "mixer.setRouteTo": set_route,
            "mixer.afterRoutingChanged": routing_changed,
            "mixer.setRouteToLevel": set_level,
            "mixer.getRouteSendActive": send_active,
            "mixer.getRouteToLevel": route_level,
        }


class FakePlugin:
    """A plugin channel's presets as its controller answers them.

    ``position`` is the preset FL is on, which no getter reports; a step moves
    it and renames the channel to that preset, as FL did live.
    """

    def __init__(self, name: str = "FLEX Bass", position: int = 2, presets: list[str] | None = None) -> None:
        self.name = name
        self.position = position
        self.presets = list(FLEX_PRESETS if presets is None else presets)
        self.fail_step_at: int | None = None
        self.steps = 0

    def step(self, by: int) -> Any:
        if self.fail_step_at is not None and self.steps == self.fail_step_at:
            return refused("RuntimeError: plugin busy")
        self.steps += 1
        self.position = max(0, min(len(self.presets) - 1, self.position + by))
        self.name = self.presets[self.position]
        return None

    def handlers(self) -> dict[str, Any]:
        return {
            "channels.getChannelName": lambda args: self.name,
            "plugins.getPresetCount": lambda args: len(self.presets),
            "plugins.getName": lambda args: self.presets[args[3]] if args[2] == 6 else "FLEX",
            "plugins.nextPreset": lambda args: self.step(1),
            "plugins.prevPreset": lambda args: self.step(-1),
        }


def session_with(*fakes: FakeMixer | FakePlugin, project: Path | None = None):
    handlers: dict[str, Any] = {}
    for fake in fakes:
        handlers.update(fake.handlers())
    control = FakeControl(hello=HELLO, handlers=handlers)
    session, _ = make_session(control=control, project=project)
    return session, control


def ops(control: FakeControl) -> list[str]:
    return [op for op, _ in control.calls if op != "ping"]


# -- channel routing ------------------------------------------------------------


def test_route_returns_the_mixer_track_fl_reports():
    mixer = FakeMixer()
    session, control = session_with(mixer)

    assert session.route_channel(1, 5) == ChannelRoute(channel=1, mixer_track=5)
    assert ("channels.setTargetFxTrack", [1, 5]) in control.calls
    assert ops(control)[-1] == "channels.getTargetFxTrack", "the result is read back after the write"


def test_a_route_fl_does_not_keep_raises_with_what_fl_has():
    mixer = FakeMixer()
    session, control = session_with(mixer)
    control.handlers["channels.setTargetFxTrack"] = lambda args: None  # FL ignores it

    with pytest.raises(WriteRejected, match="routes channel 1 to mixer track 2, not mixer track 5"):
        session.route_channel(1, 5)


def test_a_mixer_track_fl_does_not_have_is_refused_before_anything_is_written():
    session, control = session_with(FakeMixer(tracks=4))

    with pytest.raises(errors.ToolError, match="FL has mixer tracks 0 \\(the master\\) to 3") as raised:
        session.route_channel(1, 9)
    assert raised.value.category == "invalid_argument"
    assert "channels.setTargetFxTrack" not in ops(control)


def test_routing_is_live_only():
    session, _ = make_session(control=FakeControl(hello=None))

    with pytest.raises(errors.ToolError) as raised:
        session.route_channel(1, 5)
    assert raised.value.category == "control_plane_unavailable"


# -- mixer sends ------------------------------------------------------------------


def test_a_send_applies_its_routing_change_then_its_level_and_reads_both_back():
    """Live, a send showed only after afterRoutingChanged; the level was set after it."""
    mixer = FakeMixer()
    session, control = session_with(mixer)

    assert session.set_send(2, 5, on=True, level=0.5) == MixerSend(source=2, target=5, on=True, level=0.5)
    assert ops(control)[-5:] == [
        "mixer.setRouteTo",
        "mixer.afterRoutingChanged",
        "mixer.setRouteToLevel",
        "mixer.getRouteSendActive",
        "mixer.getRouteToLevel",
    ]


def test_a_send_without_a_level_keeps_fls_level():
    session, control = session_with(FakeMixer())

    assert session.set_send(2, 0) == MixerSend(source=2, target=0, on=True, level=DEFAULT_SEND_LEVEL)
    assert "mixer.setRouteToLevel" not in ops(control)


def test_a_send_turns_off():
    mixer = FakeMixer()
    mixer.sends[(2, 5)] = True
    session, _ = make_session(control=FakeControl(hello=HELLO, handlers=mixer.handlers()))

    assert session.set_send(2, 5, on=False) == MixerSend(source=2, target=5, on=False, level=None)
    assert mixer.sends[(2, 5)] is False


def test_a_send_fl_leaves_off_raises():
    mixer = FakeMixer()
    session, control = session_with(mixer)
    control.handlers["mixer.afterRoutingChanged"] = lambda args: None  # the change never shows

    with pytest.raises(WriteRejected, match="left the send from mixer track 2 to mixer track 5 off"):
        session.set_send(2, 5, on=True)


def test_a_refused_level_says_the_send_itself_landed():
    mixer = FakeMixer()
    session, control = session_with(mixer)
    control.handlers["mixer.setRouteToLevel"] = lambda args: refused("TypeError: bad level")

    with pytest.raises(WriteRejected, match="turned on the send from mixer track 2 to the master but did not"):
        session.set_send(2, 0, on=True, level=0.5)
    assert mixer.sends[(2, 0)] is True


def test_a_level_fl_does_not_keep_raises():
    mixer = FakeMixer()
    session, control = session_with(mixer)
    control.handlers["mixer.setRouteToLevel"] = lambda args: None  # FL keeps its default

    with pytest.raises(WriteRejected, match=r"to level 0\.80, not 0\.30"):
        session.set_send(2, 5, level=0.3)


# -- presets ------------------------------------------------------------------------


def test_presets_lists_the_plugins_preset_names_in_fl_order():
    session, _ = session_with(FakePlugin())

    assert session.presets(1) == FLEX_PRESETS


def test_set_preset_on_a_channel_whose_name_shows_no_preset_steps_once_to_find_it():
    """A fresh channel keeps its own name, so where FL is in the list is unknown
    until one step renames it; the rest of the way is counted from there."""
    plugin = FakePlugin(name="FLEX Bass", position=2)
    session, control = session_with(plugin)

    result = session.set_preset(1, "Pure Feather")

    assert result == ChannelPreset(channel=1, index=6, name="Pure Feather")
    assert ops(control).count("plugins.nextPreset") == 4  # 2 -> 3 to learn the place, then 3 -> 6
    assert plugin.name == "Pure Feather"


def test_set_preset_steps_back_from_the_preset_the_channel_shows():
    plugin = FakePlugin(name="Hollow Lead", position=5)
    session, control = session_with(plugin)

    assert session.set_preset(1, 1).name == "Analog Bass"
    assert ops(control).count("plugins.prevPreset") == 4
    assert "plugins.nextPreset" not in ops(control)


def test_set_preset_already_on_it_steps_nowhere():
    session, control = session_with(FakePlugin(name="Deep Sub", position=3))

    assert session.set_preset(1, "deep sub") == ChannelPreset(channel=1, index=3, name="Deep Sub")
    assert not {"plugins.nextPreset", "plugins.prevPreset"} & set(ops(control))


def test_set_preset_takes_a_unique_part_of_a_name_and_lists_choices_otherwise():
    session, control = session_with(FakePlugin(name="Default", position=0))

    assert session.set_preset(1, "feather").index == 6
    with pytest.raises(errors.ToolError, match="several presets matching 'b'") as ambiguous:
        session.set_preset(1, "b")
    assert ambiguous.value.candidates == ["Analog Bass", "Bright Pluck", "Deep Sub"]
    with pytest.raises(errors.ToolError, match="no preset matching 'choir'") as unknown:
        session.set_preset(1, "choir")
    assert unknown.value.category == "unknown_target" and unknown.value.candidates == FLEX_PRESETS


def test_a_preset_index_out_of_range_is_refused_before_stepping():
    session, control = session_with(FakePlugin())

    with pytest.raises(errors.ToolError, match="presets 0 to 6, not 7"):
        session.set_preset(1, 7)
    assert "plugins.nextPreset" not in ops(control)


def test_landing_on_another_preset_raises_and_the_retry_steps_from_what_fl_shows():
    """A channel renamed by hand to a preset's name misleads the first count; the
    readback catches it, and the stepped channel's name is then FL's own."""
    plugin = FakePlugin(name="Hollow Lead", position=3)  # FL is on Deep Sub
    session, _ = session_with(plugin)

    with pytest.raises(WriteRejected, match="now shows the preset 'Default', not 'Bright Pluck'"):
        session.set_preset(1, "Bright Pluck")
    assert session.set_preset(1, "Bright Pluck").name == "Bright Pluck"


def test_a_step_that_fails_partway_says_how_far_it_got():
    plugin = FakePlugin(name="Default", position=0)
    plugin.fail_step_at = 1
    session, _ = session_with(plugin)

    with pytest.raises(WriteRejected, match="after 1 of 3 steps"):
        session.set_preset(1, 3)


def test_a_channel_name_that_never_shows_a_preset_cannot_be_confirmed():
    plugin = FakePlugin()
    session, control = session_with(plugin)
    control.handlers["channels.getChannelName"] = lambda args: "My Bass"  # FL never renames it

    with pytest.raises(WriteRejected, match="'My Bass' does not tell which of its presets") as raised:
        session.set_preset(1, "Pure Feather")
    assert raised.value.retryable is False


def test_a_step_that_fails_after_the_locating_step_counts_it():
    plugin = FakePlugin(name="FLEX Bass", position=0)
    plugin.fail_step_at = 2
    session, _ = session_with(plugin)

    with pytest.raises(WriteRejected, match="after 2 of 4 steps"):
        session.set_preset(1, 4)  # one step to learn the place (0 -> 1), then 3 more


def test_a_name_two_presets_share_is_located_by_one_step_not_guessed():
    """The first of two equal names would miscount every time, so a retry could never land."""
    names = ["Init", "Wide Pad", "Init", "Pluck", "Sub"]
    plugin = FakePlugin(name="Init", position=2, presets=names)
    session, control = session_with(plugin)

    assert session.set_preset(1, "Sub") == ChannelPreset(channel=1, index=4, name="Sub")
    assert ops(control).count("plugins.nextPreset") == 2  # 2 -> 3 to learn the place, then 3 -> 4


@pytest.mark.parametrize("call", [lambda s: s.presets(0), lambda s: s.set_preset(0, 1)])
def test_a_sampler_channel_is_not_a_plugin(call):
    """Live: a sampler answers every plugins.* call with "Plugin not valid"."""
    control = FakeControl(
        hello=HELLO,
        handlers={
            "channels.getChannelName": lambda args: "808 Kick",
            "plugins.getPresetCount": lambda args: refused("TypeError: Plugin not valid"),
        },
    )
    session, _ = make_session(control=control)

    with pytest.raises(errors.ToolError) as raised:
        call(session)
    assert raised.value.category == "invalid_argument"
    assert raised.value.message == "808 Kick is a sampler channel, not a plugin, so it has no presets."
    assert "load_sample" in raised.value.fix


# -- stock sounds -------------------------------------------------------------------


@pytest.fixture
def patches(tmp_path, monkeypatch):
    """A small copy of FL's Data/Patches layout."""
    root = tmp_path / "Patches"
    for relative in (
        "Plugin presets/Generators/FLEX/Bass/Pure Feather.fst",
        "Plugin presets/Generators/FLEX/Keys/Glass Keys.fst",
        "Plugin presets/Effects/Fruity Reeverb 2/Big Hall.fst",
        "Packs/Drums/Kicks/808 Kick.wav",
        "Packs/Drums/Kicks/readme.txt",
    ):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(b"")
    monkeypatch.setattr(facade_mod, "fl_patches_dir", lambda: root)
    return root


def test_sounds_lists_what_this_fl_install_ships(patches):
    session, _ = make_session()

    assert session.sounds("kick") == [
        Sound(kind="sample", name="808 Kick", plugin=None, path="Packs/Drums/Kicks/808 Kick.wav")
    ]
    flex = session.sounds(kind="preset", plugin="flex")
    assert [(s.name, s.plugin) for s in flex] == [("Pure Feather", "FLEX"), ("Glass Keys", "FLEX")]
    assert [s.name for s in session.sounds("hall reeverb")] == ["Big Hall"]


def test_sounds_without_fls_stock_folder_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(facade_mod, "fl_patches_dir", lambda: tmp_path / "missing")
    session, _ = make_session()

    with pytest.raises(errors.ToolError, match="stock sounds were not found") as raised:
        session.sounds("kick")
    assert raised.value.category == "unknown_target"


# -- the SDK surface ------------------------------------------------------------------


@pytest.fixture
def live(tmp_path, monkeypatch):
    """The SDK on a saved song whose channel 1 is "FLEX Bass"; FL renames it as its preset steps."""
    mixer, plugin = FakeMixer(), FakePlugin()
    saved = tmp_path / "agent.flp"
    saved.write_bytes(b"")
    saved_song = flp.FlpProject(ppq=96, tempo=120.0, channels=SAVED_CHANNELS, notes=())
    monkeypatch.setattr(flp, "read", lambda path: saved_song)
    session, control = session_with(mixer, plugin, project=saved)
    control.handlers["state.snapshot"] = lambda args: snapshot_answer(["808 Kick", plugin.name])
    return sdk.connect(session), control


def test_sdk_calls_name_channels_like_fl_track(live):
    fl, control = live

    assert fl.route("FLEX", 3) == ChannelRoute(channel=1, mixer_track=3)
    assert fl.presets("FLEX Bass") == FLEX_PRESETS
    assert fl.set_preset(1, "Pure Feather").name == "Pure Feather"
    assert fl.send(3, 0, level=0.6) == MixerSend(source=3, target=0, on=True, level=0.6)
    with pytest.raises(sdk.ChannelNotFound):
        fl.route("Lead", 3)


def test_a_channel_goes_by_the_preset_name_fl_gave_it_at_once(live):
    """Stepping a preset renames the channel in FL while the saved song keeps the
    old name; the next call naming the channel finds it by the name FL reported."""
    fl, _ = live

    assert fl.set_preset("FLEX Bass", "Pure Feather").name == "Pure Feather"
    assert fl.route("Pure Feather", 3) == ChannelRoute(channel=1, mixer_track=3)
    assert fl.presets("Pure Feather") == FLEX_PRESETS
    with pytest.raises(sdk.ChannelNotFound, match="1: Pure Feather"):
        fl.presets("FLEX Bass")


@pytest.mark.parametrize(
    "call",
    [
        lambda fl: fl.route("FLEX", -1),
        lambda fl: fl.route("FLEX", 2.0),
        lambda fl: fl.send(2, 2),
        lambda fl: fl.send(2, 5, level=1.5),
        lambda fl: fl.send(2, 5, on="yes"),
        lambda fl: fl.send(2, 5, level=0.4, on=False),
        lambda fl: fl.set_preset("FLEX", -1),
        lambda fl: fl.set_preset("FLEX", ""),
        lambda fl: fl.sounds(limit=0),
        lambda fl: fl.sounds(limit=201),
        lambda fl: fl.sounds(kind="loop"),
    ],
)
def test_sdk_refuses_bad_arguments_before_fl_is_asked(live, call):
    fl, control = live
    before = len(control.calls)

    with pytest.raises(errors.ToolError) as raised:
        call(fl)
    assert raised.value.category == "invalid_argument"
    assert control.calls[before:] == []


def test_sdk_sounds_returns_stock_sounds(live, patches):
    fl, _ = live

    assert [s.path for s in fl.sounds("808", kind="sample")] == ["Packs/Drums/Kicks/808 Kick.wav"]


def test_the_run_kernel_carries_these_calls_and_their_results():
    """A preset by name stays a str and by index an int across the kernel's JSON."""
    by_name = wire.decode_call(wire.encode_call("set_preset", (1, "Pure Feather"), {}))
    by_index = wire.decode_call(wire.encode_call("set_preset", (1, 6), {}))
    assert by_name == {"channel": 1, "preset": "Pure Feather"}
    assert by_index == {"channel": 1, "preset": 6}
    send = wire.decode_call(wire.encode_call("set_send", (2, 5), {"on": False, "level": None}))
    assert send == {"source": 2, "target": 5, "on": False, "level": None}
    found = [Sound(kind="preset", name="Pure Feather", plugin="FLEX", path="Plugin presets/x.fst")]
    assert wire.decode_result("sounds", wire.encode_result("sounds", found)) == found


# -- what the result says FL did ----------------------------------------------------------


def test_each_change_reads_as_one_sentence_from_fls_readback():
    names = {0: "808 Kick", 1: "FLEX Bass", 4: "Pure Feather"}
    actions = ActionLog(lambda channel: names[channel])
    actions.called("route_channel", {"channel": 1, "mixer_track": 3}, ChannelRoute(channel=1, mixer_track=3))
    actions.called("route_channel", {"channel": 0, "mixer_track": 0}, ChannelRoute(channel=0, mixer_track=0))
    actions.called("set_send", {}, MixerSend(source=3, target=5, on=True, level=0.5))
    actions.called("set_send", {}, MixerSend(source=3, target=0, on=False))
    actions.called("set_preset", {}, ChannelPreset(channel=1, index=6, name="Pure Feather"))
    # A channel this call had not named yet reads its live name, already the preset's.
    actions.called("set_preset", {}, ChannelPreset(channel=4, index=6, name="Pure Feather"))
    actions.called("presets", {}, FLEX_PRESETS)
    actions.called("sounds", {}, [])

    assert actions.sentences() == [
        "Routed FLEX Bass to mixer track 3",
        "Routed 808 Kick to the master",
        "Sent mixer track 3 to mixer track 5 at level 0.50",
        "Stopped sending mixer track 3 to the master",
        "Loaded preset Pure Feather on FLEX Bass",
        "Loaded preset Pure Feather on channel 4",
    ]
