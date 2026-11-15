"""Tests for the scene summary.

The load-bearing property is not that key inference is clever - it is that it is
*honest*. A confidently wrong key would silently steer every subsequent note
choice, which is worse than reporting nothing. So most of these assert on the
confidence label, not just the key.
"""

from __future__ import annotations

import pytest
from sdk_fakes import StudioFake
from session_fakes import FIVE_CHANNEL_PROJECT, HELLO, FakeControl, make_session

from fl_mcp import errors, scene, sdk
from fl_mcp.contract import SceneSummary
from fl_mcp.scene import SceneRow as Row
from fl_mcp.schemas import Channel, MixerEffect, MixerSend, MixerTrack, Note, PatternInfo, PlaylistClip
from fl_mcp.store import Store


def weights(*midis: int) -> dict[int, int]:
    out: dict[int, int] = {}
    for m in midis:
        out[m % 12] = out.get(m % 12, 0) + 1
    return out


# --- key inference ----------------------------------------------------------


def test_unambiguous_c_major_is_found_with_confidence():
    # A full C major scale, tonic emphasised.
    key, confidence = scene._infer_key(weights(60, 60, 60, 62, 64, 65, 67, 69, 71))
    assert key == "C major"
    assert confidence in ("high", "medium")


def test_a_minor_distinguished_from_c_major_by_tonic_emphasis():
    """Same pitch classes; only the emphasised tonic differs.

    Without the tonic bonus these two are indistinguishable, which is the whole
    reason that term exists.
    """
    key, _ = scene._infer_key(weights(57, 57, 57, 57, 59, 60, 62, 64, 65, 67))
    assert key == "A minor"


def test_chromatic_material_reports_low_confidence():
    """All twelve pitch classes fit no key. Saying so is the correct answer."""
    _, confidence = scene._infer_key(weights(*range(60, 72)))
    assert confidence == "low"


def test_empty_input_is_unknown_not_a_guess():
    key, confidence = scene._infer_key({})
    assert key == "unknown"
    assert confidence == "none"


def test_two_notes_do_not_produce_high_confidence():
    """A fifth fits a dozen keys equally; claiming certainty would be a lie."""
    _, confidence = scene._infer_key(weights(60, 67))
    assert confidence != "high"


# --- root motion ------------------------------------------------------------


def test_root_motion_takes_the_lowest_note_per_bar():
    rows = [
        Row(midi=50, start_beat=0),   # D, bar 0
        Row(midi=62, start_beat=2),   # higher D, same bar - ignored
        Row(midi=48, start_beat=4),   # C, bar 1
        Row(midi=55, start_beat=8),   # G, bar 2
    ]
    assert scene._root_motion(rows) == ["D", "C", "G"]


def test_root_motion_is_capped_to_stay_compact():
    rows = [Row(midi=48, start_beat=bar * 4) for bar in range(32)]
    motion = scene._root_motion(rows)
    assert len(motion) == scene.MAX_ROOT_MOTION_BARS + 1
    assert motion[-1] == "...+24 bars"


# --- summarize --------------------------------------------------------------


def digest(rows=(), channels=(), **reads) -> SceneSummary:
    """What fl.scene() returns for these reads: summarize's fields, as the model gets them."""
    return SceneSummary.model_validate(scene.summarize(list(rows), list(channels), **reads))


def test_summary_groups_by_channel_and_names_them():
    out = digest(
        [Row(midi=60, channel=2, start_beat=0), Row(midi=64, channel=2, start_beat=1), Row(midi=40, channel=1)],
        [Channel(index=1, name="Bass", mixer_track=3), Channel(index=2, name="Lead", mixer_track=4)],
        tempo=128,
    )

    assert out.note_count == 3
    assert [c["name"] for c in out.channels] == ["Bass", "Lead"]
    assert "tempo 128, key " in str(out)
    assert "  2 Lead -> 4: vol 0.78; 2 notes in patterns 1, beats 0-2, C4-E4, pitches C E" in str(out)


def test_bass_register_gets_root_motion_and_lead_does_not():
    out = digest(
        [Row(midi=40, channel=1), Row(midi=72, channel=2)],  # E2, well below G3; C5, well above it
        [Channel(index=1, name="Bass"), Channel(index=2, name="Lead")],
    )
    by_name = {c["name"]: c for c in out.channels}

    assert by_name["Bass"]["root_motion"] == ["E"]
    assert "root_motion" not in by_name["Lead"]


def test_empty_project_says_so_rather_than_failing():
    out = digest(tempo=120)
    assert out.note_count == 0 and out.key is None
    assert "tempo 120, no notes yet" in str(out)


def test_named_channels_without_notes_read_as_empty_not_missing():
    """Live run b5343990: an empty scene said channels=[], and the model
    concluded "No channels found" and spent steps looking for them."""
    out = digest([Row(midi=36, channel=0)], [Channel(index=0, name="808 Kick"), Channel(index=1, name="808 Clap")])

    assert out.channels[-1]["name"] == "808 Clap" and out.channels[-1]["notes"] == 0
    assert "  1 808 Clap: vol 0.78; no notes" in str(out)


def test_a_channel_fl_did_not_list_is_shown_without_made_up_levels():
    """Notes or clips on a channel missing from FL's list are shown, and its levels are
    unknown, not Channel's defaults."""
    out = digest([Row(midi=60, channel=7)], clips=[PlaylistClip(track=2, start=0.0, length=4.0, channel=9)])

    assert [(c["name"], c["notes"], c["volume"]) for c in out.channels] == [("Channel 7", 1, None), ("Channel 9", 0, None)]
    text = str(out)
    assert "  7 Channel 7: not in FL's channel list, levels unknown; 1 note in patterns 1" in text
    assert "  9 Channel 9: not in FL's channel list, levels unknown; no notes, playlist clips at 0" in text


def test_rows_from_notes_carries_each_channel_across_patterns():
    rows = scene.rows_from_notes(
        {(2, 1): [Note(key=40, start=4, length=1, velocity=1.0)], (1, 0): [Note(key=36, start=0, length=0.5)]}
    )
    assert [(r.channel, r.pattern, r.midi, r.start_beat, r.velocity) for r in rows] == [
        (0, 1, 36, 0, 102), (1, 2, 40, 4, 127)
    ]


def test_summary_is_far_cheaper_than_the_raw_notes():
    """The entire point of this module, asserted rather than assumed."""
    rows = [Row(midi=60 + (i % 12), channel=0, start_beat=i * 0.25) for i in range(256)]
    out = str(digest(rows, [Channel(index=0, name="Lead")], tempo=130))

    raw_chars = sum(len(f'{{"midi":{r.midi},"channel":{r.channel},'
                        f'"start_beat":{r.start_beat},"length_beats":{r.length_beats}}}')
                    for r in rows)
    assert len(out) * 10 < raw_chars, f"summary {len(out)} chars vs raw {raw_chars} - expected a 10x reduction"


def test_note_names_round_trip_the_octave_convention():
    # C4 == MIDI 60 in this project's scientific-pitch convention.
    assert scene.note_name(60) == "C4"
    assert scene.note_name(57) == "A3"
    assert scene.note_name(72) == "C5"


# --- patterns and the playlist ------------------------------------------------


def test_each_pattern_says_where_it_plays_and_which_channels_hold_its_notes():
    out = digest(
        [Row(midi=36, channel=0, pattern=2), Row(midi=40, channel=1, pattern=2), Row(midi=60, channel=3, pattern=1)],
        patterns=[PatternInfo(number=1, name="Intro", beats=16), PatternInfo(number=2, name="Drop", beats=32),
                  PatternInfo(number=3, name="Spare", beats=16)],
        clips=[PlaylistClip(track=2, start=16.0, length=32.0, pattern=2), PlaylistClip(track=1, start=0.0, length=16.0, pattern=1),
               PlaylistClip(track=2, start=80.0, length=32.0, pattern=2), PlaylistClip(track=3, start=0.0, length=8.0, channel=5)],
    )

    assert out.playlist == {
        "clip_count": 4, "tracks_used": 3, "arrangements": [{"arrangement": 0, "clip_count": 4, "span_beats": [0.0, 112.0]}]
    }
    assert out.patterns[1] == {
        "number": 2, "name": "Drop", "beats": 32, "at": [{"arrangement": 0, "beats": [16.0, 80.0]}],
        "notes": 2, "channels": [0, 1],
    }
    text = str(out)
    assert "playlist: 4 clips on 3 tracks, beats 0-112" in text
    assert "  2 'Drop': 32 beats; at 16 80; 2 notes on 0 1" in text
    assert "  3 'Spare': 16 beats; not in the playlist; no notes" in text


def test_a_channels_own_clips_are_playlist_clips_not_audio():
    """FL's channel clips are audio and automation clips alike (flpkit FlpPlaylistItem.channel)."""
    out = digest([], [Channel(index=5, name="Volume - Master")],
                 clips=[PlaylistClip(track=3, start=0.0, length=8.0, channel=5)])
    assert "  5 Volume - Master: vol 0.78; no notes, playlist clips at 0" in str(out)


def test_a_clip_this_session_placed_reads_plainly_though_fl_did_not_report_its_track():
    """fl.place records track None when FL reported none; the line must not read "on 0 tracks"."""
    out = digest(clips=[PlaylistClip(track=None, start=0.0, length=16.0, pattern=1, arrangement=None)])

    assert out.playlist == {
        "clip_count": 1, "tracks_used": 0, "untracked": 1,
        "arrangements": [{"arrangement": None, "clip_count": 1, "span_beats": [0.0, 16.0]}],
    }
    assert out.patterns == [
        {"number": 1, "name": None, "beats": None, "at": [{"arrangement": None, "beats": [0.0]}], "notes": 0, "channels": []}
    ]
    text = str(out)
    assert "playlist: 1 clip (1 on a track FL did not report), beats 0-16" in text
    assert "  1 (name unknown): length unknown; at 0; no notes" in text
    assert "0 tracks" not in text


def test_several_arrangements_are_told_apart_not_merged():
    """A song may hold several arrangements; one pattern's starts in each are different places."""
    out = digest(clips=[
        PlaylistClip(track=1, start=0.0, length=16.0, pattern=1, arrangement=0),
        PlaylistClip(track=1, start=64.0, length=16.0, pattern=1, arrangement=0),
        PlaylistClip(track=1, start=16.0, length=16.0, pattern=1, arrangement=1),
        PlaylistClip(track=None, start=32.0, length=16.0, pattern=1, arrangement=None),
    ])

    assert [a["arrangement"] for a in out.playlist["arrangements"]] == [0, 1, None]
    text = str(out)
    assert ("playlist: 4 clips on 1 track (1 on a track FL did not report); arrangement 0: 2 clips, beats 0-80; "
            "arrangement 1: 1 clip, beats 16-32; the clips added this session: 1 clip, beats 32-48") in text
    assert "at 0 64 in arrangement 0, 16 in arrangement 1, 32 in the clips added this session" in text


def test_summary_reports_empty_playlist_honestly():
    out = digest([Row(midi=60, channel=0)])
    assert out.playlist == {"clip_count": 0}
    assert "playlist: empty" in str(out)


def test_runs_of_channels_and_patterns_print_as_ranges():
    rows = [Row(midi=60, channel=c, pattern=p) for c in (0, 2, 3, 4, 5) for p in range(1, 9)]
    text = str(digest(rows))
    assert "5 notes on 0 2-5" in text
    assert "8 notes in patterns 1-8" in text


# --- mixer -------------------------------------------------------------------


def test_the_mixer_reads_one_line_per_track_with_fl_levels_rounded():
    """FL reads a fader at 0.8 back as 0.7999999998137355; the digest shows 0.8."""
    track = MixerTrack(
        index=4, name="Kick", volume=0.7999999998137355, pan=-0.41953125037252903, muted=True,
        effects=[MixerEffect(mixer_track=4, slot=0, plugin="Fruity Limiter")],
        sends=[MixerSend(source=4, target=117, on=True, level=0.8), MixerSend(source=4, target=113, on=False)],
    )
    out = digest(mixer=[track])

    assert out.mixer[0]["volume"] == 0.7999999998137355, "the data keeps what FL reported"
    assert out.mixer[0]["sends"] == [{"target": 117, "level": 0.8}], "a send that is off is not a route"
    assert "  4 Kick: vol 0.8, pan -0.42, muted; effects 0:Fruity Limiter; sends 117:0.8" in str(out)


# --- automation ----------------------------------------------------------------


def test_summary_reports_automation_channels():
    out = digest(
        [Row(midi=60, channel=0)],
        automations=[scene.AutomationRow(channel=5, name="Volume - Master", points=3, span_beats=16.0,
                                         from_value=1.0, to_value=0.0)],
    )
    assert out.automations == [
        {"channel": 5, "name": "Volume - Master", "points": 3, "span_beats": 16.0, "values": [1.0, 0.0]}
    ]
    assert "automation: Volume - Master (3 pts over 16 beats, 1.00 -> 0.00)" in str(out)


# --- FL not answering --------------------------------------------------------------


def test_the_scene_leaves_out_what_fl_did_not_answer_and_says_so():
    out = digest([Row(midi=60, channel=0)], [Channel(index=0, name="Lead")], patterns=None, mixer=None,
                 unanswered="FL Studio did not answer in time.")

    assert out.patterns is None and out.mixer is None
    text = str(out)
    assert "left out the patterns and the mixer: FL Studio did not answer in time." in text
    assert "patterns (" not in text and "mixer tracks" not in text
    assert "  0 Lead: vol 0.78; 1 note in patterns 1" in text


def test_the_scene_reads_the_saved_song_while_fl_is_not_connected():
    """Before the scene read the patterns and the mixer it worked from the saved song with
    FL closed or starting; it still does, leaving out the two reads only FL can answer."""
    session, _ = make_session(project=FIVE_CHANNEL_PROJECT)

    out = session.scene_summary()

    assert out.patterns is None and out.mixer is None
    assert out.unanswered == "FL Studio did not answer in time."
    assert [c["name"] for c in out.channels][:2] == ["808 Kick", "808 Clap"]
    assert "left out the patterns and the mixer: FL Studio did not answer in time." in str(out)


def test_a_failure_other_than_fl_not_answering_still_raises():
    """Only "FL did not answer" is left out; FL refusing a read is a real error."""
    control = FakeControl(hello=HELLO, handlers={"state.snapshot": lambda args: {
        "tempo": 120_000, "ppq": 96, "playing": False, "current_pattern": 1, "mixer": [], "channels": []}})
    session, _ = make_session(control=control, project=FIVE_CHANNEL_PROJECT)

    with pytest.raises(errors.ToolError) as raised:
        session.scene_summary()
    assert raised.value.category == "fl_api_error"


# --- the printout -----------------------------------------------------------------


def test_printing_the_scene_or_showing_it_bare_is_the_digest():
    """print(fl.scene()) is how models read the project, and a bare fl.scene() at the
    end of a cell shows its repr: both are the digest, never the fields' dump."""
    out = digest([Row(midi=60, channel=0)], [Channel(index=0, name="Lead")], tempo=120)
    assert repr(out) == str(out)
    assert str(out).startswith("tempo 120, key ")


# The arena's harness (fl-studio-cua, MAX_RESULT_CHARS) keeps the first 20,000 characters
# of a tool result, counted on its JSON. The scene leaves room for the rest of a cell's
# output and for that escaping, on any project a model is likely to build.
SCENE_BUDGET = 12_000


def test_the_whole_project_prints_well_under_runs_output_cap(tmp_path):
    """Arena runs of 2026-11: the first call printed the scene with fl.channels,
    fl.patterns and fl.mixer, 23,558 characters on the starting project, and the
    harness kept the first 20,000. The scene is now the whole project, a line per pattern, channel
    and mixer track: here every channel of the agent project holds notes in each of
    16 patterns, each placed six times, over the template's mixer."""
    fake = StudioFake(store=Store("busy", root=tmp_path / "store"), library=tmp_path / "library")
    fl = sdk.connect(fake)
    start = str(fl.scene())
    for number in range(2, 17):
        fl.new_pattern(f"Section {number}")
    for number in range(1, 17):
        for channel in fl.channels:
            notes = [Note(key=30 + (channel.index * 5 + i * 7) % 60, start=i * 0.25, length=0.25) for i in range(128)]
            fl.track(channel.index, pattern=number).write(notes, mode="replace")
        fl.place(number, [1 + 2 * (number - 1) + 32 * k for k in range(6)])
    busy = str(fl.scene())

    assert "project notes: FLMCP agent project" in start and "no notes yet" in start
    assert all(f"  {c.index} {c.name} -> {c.mixer_track}: " in busy for c in fl.channels)
    assert "  16 'Section 16': " in busy and "  125 Sub Bus: " in busy
    assert len(start) < SCENE_BUDGET / 1.5, len(start)
    assert len(busy) < SCENE_BUDGET, len(busy)
