"""fl.audition: hear a preset alone, then leave the song as it was.

The steps are FL calls already covered on their own (loading, notes, render); these tests
hold what audition adds: nothing changes before the arguments and the Audition channel's
notes are known, the notes go where a render from bar 1 plays them, and the channel's
notes are written back whatever the render did.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fl_mcp import errors
from fl_mcp.actions import ActionLog
from fl_mcp.kernel import wire
from fl_mcp.schemas import Audition, ChannelSound, Note, PlaylistClip, RenderReceipt, RenderSpan, Target
from fl_mcp.session import preview

PRESET = "Plugin presets/Generators/Sytrus/Arp/3o3ish 2.fst"
EFFECT = "Plugin presets/Effects/Fruity Reeverb 2/Drum Room.fst"
CHANNEL = 27
SONG_NOTES = [Note(key=50, start=0.0, length=1.0)]
SPAN = RenderSpan(source="song", pattern=None, beats=8.0, tempo_bpm=120.0, seconds=4.0)


class Steps:
    """The audition's FL calls, recorded in order; ``render`` can be made to fail."""

    def __init__(self, monkeypatch, *, held: list[Note], span: RenderSpan = SPAN, clips=()) -> None:
        self.calls: list[tuple] = []
        self.held = held
        self.render_error: Exception | None = None
        self.session = type("S", (), {})()
        self.session._require_note_path = lambda: self.calls.append(("bridge",))
        self.session.playlist = lambda: list(clips)
        m = monkeypatch
        m.setattr(preview.stock_sounds, "resolve", lambda lib, sound, kind: Path(sound))
        m.setattr(preview.loading, "channels_named", lambda s, names, call: {names[0]: CHANNEL})
        m.setattr(preview, "live_arrangement_span", lambda s: span)
        m.setattr(preview.note_ops, "read_notes", self._read)
        m.setattr(preview.loading, "load_sound", self._load)
        m.setattr(preview.loading, "name_channel", self._name)
        m.setattr(preview.ops_notes, "write_notes", lambda s, t, notes, mode: self.calls.append(("write", t, notes, mode)))
        m.setattr(preview.ops_notes, "clear_notes", lambda s, t: self.calls.append(("clear", t)))
        m.setattr(preview.controls, "render_wav", self._render)

    def _read(self, session, target):
        self.calls.append(("read", target))
        return list(self.held)

    def _load(self, session, channel, sound, kind):
        self.calls.append(("load", channel, sound, kind))
        return ChannelSound(channel=channel, kind="preset", sound=sound, name="3o3ish 2", plugin="Sytrus")

    def _name(self, session, channel, name):
        self.calls.append(("name", channel, name))
        return name

    def _render(self, session, output, *, start_bar, bars, solo):
        self.calls.append(("render", start_bar, bars, solo))
        if self.render_error is not None:
            raise self.render_error
        return RenderReceipt(path=output, span=SPAN, solo=list(solo))

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


def test_a_preset_plays_alone_on_the_audition_channel_and_its_notes_go_back(monkeypatch, tmp_path):
    clips = [PlaylistClip(track=1, start=0.0, length=16.0, pattern=1)]
    steps = Steps(monkeypatch, held=SONG_NOTES, clips=clips)

    result = preview.audition(steps.session, PRESET, None, tmp_path / "a.wav")

    target = Target(pattern=1, channel=CHANNEL)
    assert steps.kinds() == ["bridge", "read", "load", "name", "write", "render", "write"]
    assert steps.calls[4] == ("write", target, list(preview.DEFAULT_NOTES), "replace")
    assert steps.calls[5] == ("render", 1, 2, (CHANNEL,)), "two bars of the default phrase, that channel alone"
    assert steps.calls[6] == ("write", target, SONG_NOTES, "replace")
    assert result.sound.name == preview.AUDITION_CHANNEL and result.render.solo == [CHANNEL]


def test_the_notes_go_back_when_writing_the_phrase_is_not_confirmed(monkeypatch, tmp_path):
    """FL may apply a write whose confirmation timed out, so the notes are put back anyway."""
    steps = Steps(monkeypatch, held=SONG_NOTES, clips=[PlaylistClip(track=1, start=0.0, length=16.0, pattern=1)])

    def unconfirmed(session, target, notes, mode):
        steps.calls.append(("write", target, notes, mode))
        if notes != SONG_NOTES:
            raise errors.ToolError("note_plane_unavailable", "no confirmation", "retry")

    monkeypatch.setattr(preview.ops_notes, "write_notes", unconfirmed)

    with pytest.raises(errors.ToolError, match="no confirmation"):
        preview.audition(steps.session, PRESET, None, tmp_path / "a.wav")
    assert steps.calls[-1] == ("write", Target(pattern=1, channel=CHANNEL), SONG_NOTES, "replace")
    assert "render" not in steps.kinds()


def test_the_notes_go_back_when_the_render_fails(monkeypatch, tmp_path):
    steps = Steps(monkeypatch, held=[], span=SPAN.model_copy(update={"source": "pattern", "pattern": 3}))
    steps.render_error = errors.ToolError("render_failed", "FL stopped", "retry")

    with pytest.raises(errors.ToolError, match="FL stopped"):
        preview.audition(steps.session, PRESET, [Note(key=40, start=0.0, length=1.0)], tmp_path / "a.wav")

    assert steps.calls[-1] == ("clear", Target(pattern=3, channel=CHANNEL)), "pattern mode: the selected pattern"


def test_a_preset_fl_refuses_leaves_the_channel_named_audition(monkeypatch, tmp_path):
    """FL renames the channel after a preset even when the load is not confirmed (SimSynth on the
    index build, 2026-11-13); without naming it back, every later audition found no Audition channel."""
    steps = Steps(monkeypatch, held=[], clips=[PlaylistClip(track=1, start=0.0, length=16.0, pattern=1)])

    def refused(session, channel, sound, kind):
        steps.calls.append(("load", channel, sound, kind))
        raise errors.ToolError("write_rejected", "Channel 27 plays SimSynth Live as 'BLANK'", "retry")

    monkeypatch.setattr(preview.loading, "load_sound", refused)

    with pytest.raises(errors.ToolError, match="SimSynth"):
        preview.audition(steps.session, PRESET, None, tmp_path / "a.wav")
    assert steps.kinds()[-2:] == ["load", "name"] and steps.calls[-1] == ("name", CHANNEL, preview.AUDITION_CHANNEL)
    assert "write" not in steps.kinds()


@pytest.mark.parametrize(
    ("preset", "notes", "message"),
    [
        (EFFECT, None, "is an effect preset"),
        ("Plugin presets/VST/Some Synth/Init.fst", None, "VST presets cannot be loaded"),
        (PRESET, [], "at least one note"),
        (PRESET, [Note(key=60, start=31.0, length=2.0)], "at most 8"),
    ],
)
def test_an_audition_that_cannot_run_changes_nothing(monkeypatch, tmp_path, preset, notes, message):
    steps = Steps(monkeypatch, held=[])

    with pytest.raises(errors.ToolError, match=message):
        preview.audition(steps.session, preset, notes, tmp_path / "a.wav")
    assert steps.calls == []


def test_a_song_without_a_pattern_at_bar_one_is_refused_before_loading(monkeypatch, tmp_path):
    steps = Steps(monkeypatch, held=[], clips=[PlaylistClip(track=1, start=16.0, length=4.0, pattern=2)])

    with pytest.raises(errors.ToolError, match="no pattern starts the song at bar 1"):
        preview.audition(steps.session, PRESET, None, tmp_path / "a.wav")
    assert "load" not in steps.kinds()


def test_an_audition_reads_as_one_sentence_and_crosses_the_kernel_wire(tmp_path):
    sound = ChannelSound(channel=CHANNEL, kind="preset", sound=PRESET, name="Audition", plugin="Sytrus")
    result = Audition(sound=sound, render=RenderReceipt(path=tmp_path / "a.wav", span=SPAN, solo=[CHANNEL]))
    actions = ActionLog(lambda channel: "Audition")
    actions.called("audition", {}, result)

    assert actions.sentences() == [
        f"Auditioned the Sytrus preset 3o3ish 2 on channel {CHANNEL}: rendered 0:04; its notes were put back"
    ]
    call = wire.decode_call(wire.encode_call("audition", (PRESET, Path("a.wav")), {"notes": None}))
    assert call == {"preset": PRESET, "output": Path("a.wav"), "notes": None}
    assert wire.decode_result("audition", wire.encode_result("audition", result)) == result


def test_a_preset_name_that_is_no_file_is_refused_with_the_real_ones(monkeypatch, tmp_path):
    """Live 2026-11-13: a model auditioned "PAD Dark MC", a FLEX channel's own preset name, and was
    told only that it is no preset FL ships; it is now looked up first and the closest named."""
    from fl_mcp import sounds

    stock = tmp_path / "Patches"
    (stock / PRESET).parent.mkdir(parents=True)
    (stock / PRESET).write_bytes(b"")
    monkeypatch.setattr(sounds, "fl_patches_dir", lambda: stock)
    monkeypatch.setattr(sounds, "sound_library_dir", lambda: tmp_path / "sounds")
    with pytest.raises(errors.ToolError, match=r"Did you mean 'Plugin presets/Generators/Sytrus/Arp/3o3ish 2\.fst'"):
        preview.audition(object(), "3o3ish 2", None, tmp_path / "a.wav")  # refused before the session is used
