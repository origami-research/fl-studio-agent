"""fl.mixer, fl.patterns and fl.playlist: what run code reads of FL's mixer and song.

The mixer is one composite controller read (state.mixer) because a full scan of
127 tracks' sends is 16,000 FL calls; the script scans sends only from tracks in use.
"""

from __future__ import annotations

from types import SimpleNamespace

from session_fakes import HELLO, FakeControl, make_session

from fl_mcp import scene
from fl_mcp.fl_scripts import device_FLMCP as flmcp
from fl_mcp.schemas import MixerEffect, MixerSend, PatternInfo, PlaylistClip


def _fl_mixer(monkeypatch, *, names, effects, sends, targets):
    """A 6-track FL mixer: names by track, effects {track: {slot: plugin}}, sends {(src, dst): level}."""
    calls = {"route": 0}

    def active(src, dst):
        calls["route"] += 1
        return (src, dst) in sends

    monkeypatch.setattr(flmcp, "mixer", SimpleNamespace(
        trackCount=lambda: len(names),
        getTrackName=lambda i: names[i],
        getTrackVolume=lambda i: 0.8,
        getTrackPan=lambda i: 0.0,
        isTrackMuted=lambda i: False,
        isTrackSolo=lambda i: False,
        isTrackPluginValid=lambda i, slot: slot in effects.get(i, {}),
        getRouteSendActive=active,
        getRouteToLevel=lambda src, dst: sends[(src, dst)],
    ), raising=False)
    monkeypatch.setattr(flmcp, "plugins", SimpleNamespace(getPluginName=lambda i, slot: effects[i][slot]), raising=False)
    monkeypatch.setattr(flmcp, "channels", SimpleNamespace(
        channelCount=lambda globalCount: len(targets), getTargetFxTrack=lambda c, useGlobalIndex: targets[c]
    ), raising=False)
    return calls


def test_the_mixer_read_lists_the_tracks_in_use_with_effects_and_sends(monkeypatch):
    # Channel 0 plays into track 1, which sends to a reverb bus (4); track 3 holds an
    # effect; track 5 was renamed; track 2 is untouched and left out.
    names = ["Master", "Insert 1", "Insert 2", "Insert 3", "Insert 4", "Vox"]
    calls = _fl_mixer(
        monkeypatch,
        names=names,
        effects={0: {9: "Fruity Limiter"}, 3: {0: "Fruity Parametric EQ 2"}},
        sends={(1, 0): 0.8, (1, 4): 0.5, (4, 0): 0.8, (3, 0): 0.8, (5, 0): 0.8},
        targets=[1],
    )

    rows = flmcp._mixer_tracks()

    assert [r["idx"] for r in rows] == [0, 1, 3, 4, 5]
    by = {r["idx"]: r for r in rows}
    assert by[1]["sends"] == [[0, 0.8], [4, 0.5]]
    assert by[0]["effects"] == [[9, "Fruity Limiter"]]
    assert by[3]["effects"] == [[0, "Fruity Parametric EQ 2"]]
    assert calls["route"] == 5 * (len(names) - 1)  # sends scanned only from the five tracks in use


def test_the_session_reads_the_mixer_into_tracks_with_effects_and_sends():
    payload = [
        {"idx": 0, "name": "Master", "vol": 0.8, "pan": 0.0, "muted": False, "solo": False,
         "effects": [[9, "Fruity Limiter"]], "sends": []},
        {"idx": 4, "name": "Drums", "vol": 0.7, "pan": 0.1, "muted": False, "solo": True,
         "effects": [], "sends": [[0, 0.8]]},
    ]
    session, _ = make_session(control=FakeControl(hello=HELLO, handlers={"state.mixer": lambda args: payload}))

    tracks = session.mixer_tracks()

    assert tracks[0].effects == [MixerEffect(mixer_track=0, slot=9, plugin="Fruity Limiter")]
    assert tracks[1].solo and tracks[1].sends == [MixerSend(source=4, target=0, on=True, level=0.8)]


def test_the_patterns_are_the_ones_holding_something_plus_the_selected_one():
    """patternCount counts only patterns that are not empty (box, 2026-11-03), and they need
    not be numbered from 1: here 3 and 5 hold notes, 1 is selected and still empty."""
    handlers = {
        "patterns.patternCount": lambda args: 2,
        "patterns.patternMax": lambda args: 999,
        "patterns.patternNumber": lambda args: 1,
        "patterns.isPatternDefault": lambda args: args[0] not in (3, 5),
        "patterns.getPatternName": lambda args: f"Pattern {args[0]}",
        "patterns.getPatternLength": lambda args: {1: 16, 3: 64, 5: 32}[args[0]],  # 16th steps
    }
    control = FakeControl(hello=HELLO, handlers=handlers)
    session, _ = make_session(control=control)

    assert session.patterns() == [PatternInfo(number=1, name="Pattern 1", beats=4.0),
                                  PatternInfo(number=3, name="Pattern 3", beats=16.0),
                                  PatternInfo(number=5, name="Pattern 5", beats=8.0)]
    assert len([op for op, _ in control.calls if op == "patterns.isPatternDefault"]) == 5  # stops at the last one


def test_the_playlist_is_the_opened_songs_clips(monkeypatch, tmp_path):
    project = tmp_path / "agent.flp"
    project.write_bytes(b"")
    clip = scene.ClipRow(track=1, start_beat=0.0, length_beats=16.0, pattern=1)
    monkeypatch.setattr(scene, "read_clips", lambda path: [clip] if path == project else [])
    session, _ = make_session(project=project)

    assert session.playlist() == [PlaylistClip(track=1, start=0.0, length=16.0, pattern=1, channel=None)]
