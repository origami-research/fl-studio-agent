"""Loading stock samples and presets into channels: select one channel, load, read back.

The fake FL answers as FL 2026 did live (docs/research/load-sample-preset-live-2026-11-02.md):
FHD_LoadInChannel loads into EVERY selected channel; a sample renames a sampler
after itself ("808 Kick #2" when that name is taken) and only renames a plugin
channel; an instrument preset puts its plugin on the channel and names it after
the preset; an effect preset adds its effect to the next free slot of the
channel's mixer track. Each test names the rule it holds: nothing reaches FL
before the arguments and the channel are known good, exactly one channel is
selected when the file is sent, and every result is FL's readback.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from session_fakes import HELLO, FakeControl, FakeSpool, isolate_spool, live_untargeted_bridge, make_session

from fl_mcp import errors, sdk
from fl_mcp.actions import ActionLog
from fl_mcp.kernel import wire
from fl_mcp.schemas import (
    Channel,
    ChannelSound,
    ControlResponse,
    MixerEffect,
    Sound,
    SpoolCommand,
    SpoolResponse,
)
from fl_mcp.session import WriteRejected
from fl_mcp.session import loading as loading_mod

KICK = "Packs/Drums/Kicks/808 Kick.wav"
COWBELL = "Packs/Drums/Percussion/808 Cowbell.wav"
SYTRUS = "Plugin presets/Generators/Sytrus/Arp/3o3ish 2.fst"
OSC = "Plugin presets/Generators/3x Osc/Default.fst"
REEVERB = "Plugin presets/Effects/Fruity Reeverb 2/Drum Room.fst"
VST = "Plugin presets/VST/Some Synth/Init.fst"
PING_OK = SpoolResponse(id=0, ok=True, op="ping")


@pytest.fixture(autouse=True)
def _no_real_spool(monkeypatch):
    isolate_spool(monkeypatch)


@pytest.fixture
def patches(tmp_path, monkeypatch):
    """A small copy of FL's Data/Patches layout."""
    root = tmp_path / "Patches"
    for relative in (KICK, COWBELL, SYTRUS, OSC, REEVERB, VST):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(b"")
    monkeypatch.setattr(loading_mod, "fl_patches_dir", lambda: root)
    return root


def refused(detail: str) -> ControlResponse:
    return ControlResponse(id=1, ok=False, err=detail)


class FakeFL:
    """The template's channels and their mixer tracks, as FL's controller answers them."""

    def __init__(self) -> None:
        self.channels: list[dict[str, Any]] = [
            {"name": "808 Kick", "type": 0, "plugin": None, "fx": 1},
            {"name": "808 Clap", "type": 0, "plugin": None, "fx": 2},
            {"name": "808 HiHat", "type": 0, "plugin": None, "fx": 3},
            {"name": "808 Snare", "type": 0, "plugin": None, "fx": 4},
            {"name": "FLEX Bass", "type": 2, "plugin": "FLEX", "fx": 5},
        ]
        self.slots: dict[int, list[str | None]] = {track: [None] * 10 for track in range(6)}
        self.selected: list[int] = [0]
        self.deselects = True  # select_channels clears the rest, as selectOneChannel does
        self.ignores_loads = False

    def _unique(self, stem: str, channel: int) -> str:
        taken = {c["name"] for i, c in enumerate(self.channels) if i != channel}
        if stem not in taken:
            return stem
        number = 2
        while f"{stem} #{number}" in taken:
            number += 1
        return f"{stem} #{number}"

    def load(self, path: str) -> None:
        """FHD_LoadInChannel: the file goes to every selected channel."""
        if self.ignores_loads:
            return
        parts = PurePosixPath(Path(path).as_posix()).parts
        stem = PurePosixPath(path).stem
        for index in self.selected:
            channel = self.channels[index]
            if "Effects" in parts:
                slots = self.slots[channel["fx"]]
                # FL's own name differs from the folder in case, as live.
                slots[slots.index(None)] = parts[parts.index("Effects") + 1].replace("Reeverb", "reeverb")
            elif "Generators" in parts:
                plugin = parts[parts.index("Generators") + 1]
                channel.update(type=2, plugin=plugin, name=self._unique(stem, index))
            else:
                channel["name"] = self._unique(stem, index)  # a plugin channel is only renamed

    def handlers(self) -> dict[str, Any]:
        def select(args):
            channel = args[0][0]
            self.selected = [channel] if self.deselects else sorted({*self.selected, channel})

        def selected(args):
            # selectedChannel(canBeNone, offset): the offset-th selected channel, or the last (live).
            offset = args[1] if len(args) > 1 else 0
            return self.selected[min(offset, len(self.selected) - 1)]

        def plugin_name(args):
            index, slot = args
            name = self.channels[index]["plugin"] if slot == -1 else self.slots[index][slot]
            return name if name is not None else refused("RuntimeError: Plugin not valid")

        return {
            "select_channels": select,
            "channels.selectedChannel": selected,
            "channels.getChannelName": lambda args: self.channels[args[0]]["name"],
            "channels.getChannelType": lambda args: self.channels[args[0]]["type"],
            "channels.getTargetFxTrack": lambda args: self.channels[args[0]]["fx"],
            "plugins.isValid": lambda args: int(self.channels[args[0]]["plugin"] is not None),
            "plugins.getPluginName": plugin_name,
            "mixer.isTrackPluginValid": lambda args: int(self.slots[args[0]][args[1]] is not None),
        }


class LoadingSpool(FakeSpool):
    """The transport to the FL MCP Bridge: load_in_channel reaches the fake FL."""

    def __init__(self, fl: FakeFL) -> None:
        super().__init__(responses={"ping": PING_OK})
        self.fl = fl
        self.refuse: str | None = None

    def await_response(self, command: SpoolCommand, timeout: float) -> SpoolResponse:
        if command.op != "load_in_channel":
            return super().await_response(command, timeout)
        if self.refuse is not None:
            return SpoolResponse(id=command.id, ok=False, op="load_in_channel", err=self.refuse)
        self.fl.load(self.sent[-1][1]["path"])
        return SpoolResponse(id=command.id, ok=True, op="load_in_channel")


@pytest.fixture
def box(tmp_path, monkeypatch, patches):
    """A session with a live FL MCP Bridge and the fake FL behind it."""
    live_untargeted_bridge(monkeypatch, tmp_path)
    fl = FakeFL()
    control = FakeControl(hello=HELLO, handlers=fl.handlers())
    spool = LoadingSpool(fl)
    session, _ = make_session(control=control, transport=spool)
    return session, fl, control, spool


def loads(spool: LoadingSpool) -> list[dict[str, Any]]:
    return [args for op, args in spool.sent if op == "load_in_channel"]


def touched_fl(control: FakeControl, spool: LoadingSpool) -> bool:
    return any(op == "select_channels" for op, _ in control.calls) or bool(loads(spool))


# -- samples ---------------------------------------------------------------------------


def test_a_sample_loads_into_the_one_selected_sampler_and_reads_back_its_new_name(box, patches):
    session, fl, control, spool = box

    loaded = session.load_sound(1, COWBELL, "sample")

    assert loaded == ChannelSound(channel=1, kind="sample", sound=COWBELL, name="808 Cowbell", plugin=None)
    assert ("select_channels", [[1]]) in control.calls
    assert loads(spool) == [{"path": str(patches / COWBELL)}], "the Bridge gets the file's full path"
    assert [c["name"] for c in fl.channels] == ["808 Kick", "808 Cowbell", "808 HiHat", "808 Snare", "FLEX Bass"]


def test_fl_numbers_a_sample_name_another_channel_already_has(box):
    session, fl, _, _ = box

    assert session.load_sound(2, KICK, "sample").name == "808 Kick #2"


def test_a_sample_into_a_plugin_channel_is_refused_before_fl_is_asked(box):
    """Live, FL only renamed a 3x Osc channel after the sample: nothing a model wants."""
    session, fl, control, spool = box

    with pytest.raises(errors.ToolError) as raised:
        session.load_sound(4, KICK, "sample")
    assert raised.value.category == "invalid_argument"
    assert "plays FLEX, not samples" in raised.value.message
    assert not touched_fl(control, spool)


# -- presets ---------------------------------------------------------------------------


@pytest.mark.parametrize(("channel", "was"), [(4, "FLEX"), (3, None)])
def test_an_instrument_preset_replaces_the_channels_instrument(box, channel, was):
    session, fl, _, _ = box
    assert fl.channels[channel]["plugin"] == was

    loaded = session.load_sound(channel, SYTRUS, "preset")

    assert loaded == ChannelSound(channel=channel, kind="preset", sound=SYTRUS, name="3o3ish 2", plugin="Sytrus")
    assert loaded.effect is None


def test_an_effect_preset_adds_its_effect_to_the_next_free_slot_of_the_channels_mixer_track(box):
    session, fl, _, _ = box

    first = session.load_sound(2, REEVERB, "preset")
    second = session.load_sound(2, REEVERB, "preset")

    assert first.effect == MixerEffect(mixer_track=3, slot=0, plugin="Fruity reeverb 2")
    assert second.effect == MixerEffect(mixer_track=3, slot=1, plugin="Fruity reeverb 2")
    assert (first.name, first.plugin) == ("808 HiHat", None), "the instrument stays"


def test_a_full_mixer_track_is_refused_before_fl_is_asked(box):
    session, fl, control, spool = box
    fl.slots[3] = ["Fruity Limiter"] * 10

    with pytest.raises(errors.ToolError, match="mixer track 3, which channel 2 plays through, has no free effect slot"):
        session.load_sound(2, REEVERB, "preset")
    assert not touched_fl(control, spool)


# -- refusals before FL ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("sound", "kind"),
    [
        ("../Packs/x.wav", "sample"),
        ("/Packs/Drums/Kicks/808 Kick.wav", "sample"),
        (SYTRUS, "sample"),
        (KICK, "preset"),
        ("Packs/Drums/Kicks/missing.wav", "sample"),
        ("Packs/D:/Drums/x.wav", "sample"),  # a drive letter in a part would leave the folder on Windows
        (VST, "preset"),  # which plugin and role a VST preset loads as was not checked live
    ],
)
def test_a_sound_that_is_not_a_stock_file_of_its_kind_is_refused_before_fl_is_asked(box, sound, kind):
    session, _, control, spool = box

    with pytest.raises(errors.ToolError) as raised:
        session.load_sound(1, sound, kind)
    assert raised.value.category == "invalid_argument"
    assert control.calls == [] and spool.sent == []


def test_without_a_live_bridge_nothing_is_selected_or_sent(tmp_path, monkeypatch, patches):
    fl = FakeFL()
    control = FakeControl(hello=HELLO, handlers=fl.handlers())
    spool = LoadingSpool(fl)
    session, _ = make_session(control=control, transport=spool)  # no heartbeat: no live Bridge

    with pytest.raises(errors.ToolError) as raised:
        session.load_sound(1, COWBELL, "sample")
    assert raised.value.category == "note_plane_unavailable" and raised.value.retryable
    assert not touched_fl(control, spool)


def test_a_second_selected_channel_stops_the_load_before_the_file_is_sent(box):
    """FL loads into every selected channel: a selection FL kept would overwrite another channel."""
    session, fl, _, spool = box
    fl.selected = [3]
    fl.deselects = False  # FL keeps channel 3 selected beside the target

    with pytest.raises(errors.ToolError, match="kept channel 3 selected beside channel 0") as raised:
        session.load_sound(0, COWBELL, "sample")
    assert raised.value.retryable
    assert loads(spool) == []
    assert fl.channels[0]["name"] == "808 Kick"


def test_a_bridge_refusal_raises_in_plain_words(box):
    """The Bridge's own detail is logged, not shown: it names internals the model cannot act on."""
    session, _, _, spool = box
    spool.refuse = "no file at C:/x.wav"

    with pytest.raises(WriteRejected) as raised:
        session.load_sound(1, COWBELL, "sample")
    assert raised.value.message == "FL Studio refused to load 808 Cowbell.wav into channel 1. Nothing was loaded."
    assert raised.value.retryable


def test_an_fl_plugin_older_than_the_tool_says_to_update_it_not_to_retry(box):
    """A Bridge built before load_in_channel answers "unknown op"; a retry can never help."""
    session, _, _, spool = box
    spool.refuse = "unknown op: load_in_channel"

    with pytest.raises(WriteRejected, match="older than this tool") as raised:
        session.load_sound(1, COWBELL, "sample")
    assert not raised.value.retryable


def test_an_instrument_preset_fl_ignores_on_a_channel_already_playing_that_plugin_raises(box):
    """The plugin alone cannot confirm the load there; FL's rename to the preset does."""
    session, fl, _, _ = box
    fl.channels[4].update(plugin="Sytrus", name="Lead")
    fl.ignores_loads = True

    with pytest.raises(WriteRejected, match="did not confirm the preset"):
        session.load_sound(4, SYTRUS, "preset")


@pytest.mark.parametrize("sound", [COWBELL, SYTRUS, REEVERB])
def test_a_load_fl_does_not_show_raises(box, sound):
    """FL acknowledged but nothing changed (as an unreadable file name did live): never a success."""
    session, fl, _, _ = box
    fl.ignores_loads = True

    with pytest.raises(WriteRejected):
        session.load_sound(1, sound, "preset" if sound.endswith(".fst") else "sample")


# -- the SDK surface and the run kernel -------------------------------------------------


@pytest.fixture
def live(box, monkeypatch):
    session, fl, control, spool = box
    names = [Channel(index=i, name=c["name"]) for i, c in enumerate(fl.channels)]
    monkeypatch.setattr(session, "channel_names", lambda: names)
    return sdk.connect(session), control, spool


def test_the_sdk_takes_fl_sounds_results_or_their_paths(live):
    fl, _, _ = live
    cowbell = Sound(kind="sample", name="808 Cowbell", path=COWBELL)
    sytrus = Sound(kind="preset", name="3o3ish 2", plugin="Sytrus", path=SYTRUS)

    assert fl.load_sample("808 Clap", cowbell).name == "808 Cowbell"
    assert fl.load_sample(2, KICK).name == "808 Kick #2"
    assert fl.load_preset("FLEX", sytrus).plugin == "Sytrus"


@pytest.mark.parametrize(
    "call",
    [
        lambda fl: fl.load_sample(1, Sound(kind="preset", name="3o3ish 2", plugin="Sytrus", path=SYTRUS)),
        lambda fl: fl.load_preset(1, Sound(kind="sample", name="808 Kick", path=KICK)),
        lambda fl: fl.load_sample(1, ""),
        lambda fl: fl.load_sample(1, 5),
    ],
)
def test_the_sdk_refuses_a_sound_of_the_wrong_kind_before_fl_is_asked(live, call):
    fl, control, spool = live

    with pytest.raises(errors.ToolError) as raised:
        call(fl)
    assert raised.value.category == "invalid_argument"
    assert not touched_fl(control, spool)


def test_the_run_kernel_carries_a_load_and_its_result():
    call = wire.decode_call(wire.encode_call("load_sound", (2, REEVERB, "preset"), {}))
    assert call == {"channel": 2, "sound": REEVERB, "kind": "preset"}
    result = ChannelSound(
        channel=2, kind="preset", sound=REEVERB, name="808 HiHat",
        effect=MixerEffect(mixer_track=3, slot=0, plugin="Fruity Reeverb 2"),
    )
    assert wire.decode_result("load_sound", wire.encode_result("load_sound", result)) == result


def test_each_load_reads_as_one_sentence_from_fls_readback():
    actions = ActionLog(lambda channel: "stale saved name")
    actions.called("load_sound", {}, ChannelSound(channel=1, kind="sample", sound=COWBELL, name="808 Cowbell"))
    actions.called("load_sound", {}, ChannelSound(channel=2, kind="sample", sound=KICK, name="808 Kick #2"))
    actions.called("load_sound", {}, ChannelSound(channel=4, kind="preset", sound=SYTRUS, name="3o3ish 2",
                                                  plugin="Sytrus"))
    actions.called("load_sound", {}, ChannelSound(channel=2, kind="preset", sound=REEVERB, name="808 HiHat",
                                                  effect=MixerEffect(mixer_track=3, slot=1, plugin="Fruity Reeverb 2")))

    assert actions.sentences() == [
        "Loaded the sample 808 Cowbell into channel 1",
        "Loaded the sample 808 Kick into channel 2, now named 808 Kick #2",
        "Loaded the Sytrus preset 3o3ish 2 into channel 4",
        "Added Fruity Reeverb 2 (Drum Room) to mixer track 3 in effect slot 1",
    ]
    assert not any(re.search("stale", s) for s in actions.sentences())
