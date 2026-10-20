"""Every tool result says what the engine did, the way a coding assistant lists its edits.

A UI shows ``actions`` as "what the assistant did" rows, so each sentence must
be a change FL confirmed or a measurement the engine took: built from what
the call returned, never from what was asked, never for a read or a failure.
For run they come from the fl calls the server served while the cell ran,
so a cell that fails partway still reports the changes FL already has.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sdk_fakes import DEFAULT_CHANNELS, FakeSession
from session_fakes import RENDER_SPAN

from fl_mcp import errors, server
from fl_mcp.actions import GROUP_AT, MAX_ACTIONS, MAX_CHARS, ActionLog
from fl_mcp.audio import AudioAnalysis, SpectralBalance
from fl_mcp.flp import Effect
from fl_mcp.kernel.host import KernelHost
from fl_mcp.kernel.launch import PosixLauncher, Workspace
from fl_mcp.schemas import (
    AutomationPoint,
    AutomationResult,
    ChannelCheck,
    ChannelLevels,
    CheckResult,
    PluginParam,
    RenderReceipt,
    RenderSpan,
    Target,
    WriteResult,
)
from fl_mcp.store import Store

NAMES = {0: "808 Kick", 1: "808 Clap", 2: "808 HiHat", 3: "808 Snare", 4: "FLEX Bass"}


def log() -> ActionLog:
    return ActionLog(lambda channel: NAMES[channel])


def levels(channel: int, volume: float = 0.8, pan: float = 0.0, pitch: int = 0) -> ChannelLevels:
    return ChannelLevels(channel=channel, volume=volume, pan=pan, pitch_semitones=pitch)


def check_result(master: float | None = -1.8, start_bar: int = 9, bars: int = 2) -> CheckResult:
    heard = [ChannelCheck(channel=i, name=NAMES[i], sounded=True, peak_dbfs=-6.0, mixer_track=i + 1) for i in range(5)]
    return CheckResult(
        start_bar=start_bar, bars=bars, seconds=4.0, master_peak_dbfs=master, channels=heard, summary="."
    )


def analysis(**overrides: object) -> AudioAnalysis:
    fields = {
        "bpm": 122.04,
        "key": "A",
        "integrated_lufs": -9.84,
        "true_peak_dbfs": -4.0,
        "spectral_balance": SpectralBalance(low_dbfs=-10.0, mid_dbfs=-30.0, high_dbfs=-40.0),
    }
    return AudioAnalysis(**{**fields, **overrides})


def note_write(actions: ActionLog, channel: int, wrote: int, mode: str = "merge", total: int | None = None,
               pattern: int = 1, track: str | None = None) -> None:
    """A write the way the session reports it: FL's counts, for (channel, pattern)."""
    result = WriteResult(wrote=wrote, track=track or NAMES[channel], pattern=pattern, mode=mode, total=total)
    method = "clear_notes" if mode == "replace" and wrote == 0 and total == 0 else "write_notes"
    actions.called(method, {"target": Target(pattern=pattern, channel=channel)}, result)


def volume(actions: ActionLog, channel: int, value: float) -> None:
    actions.called("set_channel_levels", {"channel": channel, "volume": value}, levels(channel, volume=value))


# -- one sentence per confirmed effect, in the contract's style -------------------


def test_each_kind_of_change_reads_as_one_plain_sentence():
    actions = log()
    actions.called("set_tempo", {"bpm": 122.0}, 122.0)
    note_write(actions, 0, 64, total=64)
    note_write(actions, 4, 212, "replace", total=212)
    note_write(actions, 1, 1, total=3, pattern=2)
    note_write(actions, 2, 0, "replace", total=0)
    actions.called(
        "set_plugin_param",
        {"channel": 4, "param": "filter cutoff", "value": 0.68},
        PluginParam(index=3, name="filter cutoff", value=0.68, display="68%", kind="continuous"),
    )
    actions.called(
        "set_plugin_param",
        {"channel": 4, "param": "wave", "value": "Saw"},
        PluginParam(index=4, name="wave", value=0.5, display="Saw", kind="enum", options=["Sine", "Saw"]),
    )
    volume(actions, 0, 0.6)
    actions.called(
        "set_channel_levels", {"channel": 1, "pan": -0.15, "pitch_semitones": 2}, levels(1, pan=-0.15, pitch=2)
    )
    actions.called(
        "set_automation", {}, AutomationResult(channel=4, points=[AutomationPoint(position=0, value=0.5)] * 8)
    )
    eq = Effect(name="Fruity Parametric EQ 2", chunk=b"")
    actions.called("add_effect", {"insert_index": 0, "plugin_name": "Fruity Parametric EQ 2"}, (eq,))
    actions.called("check", {}, check_result())
    actions.called("render_wav", {}, RenderReceipt(path=Path("r.wav"), span=_song_span(34.2)))
    actions.called("render_wav", {}, RenderReceipt(path=Path("r.wav"), span=RENDER_SPAN))
    actions.called("export_offline", {}, Path("/renders/mix.wav"))
    actions.called("save_project", {}, None)
    actions.called("undo", {}, None)
    actions.measured(analysis(), "the render")
    assert actions.sentences() == [
        "Set tempo to 122 BPM",
        "Wrote 64 notes to 808 Kick",
        "Replaced the notes on FLEX Bass (212 notes)",
        "Wrote 1 note to 808 Clap in pattern 2",
        "Cleared the notes on 808 HiHat",
        "Set FLEX Bass filter cutoff to 0.68",
        "Set FLEX Bass wave to Saw",
        "Set 808 Kick volume to 0.60",
        "Set 808 Clap pan to -0.15",
        "Set 808 Clap pitch to +2 semitones",
        "Replaced the automation on FLEX Bass (8 points)",
        "Reopened FL with the changed song",
        "Added Fruity Parametric EQ 2 to the master",
        "Reopened FL with the changed song",
        "Checked bars 9-10: 5 of 5 tracks sound, master -1.8 dBFS",
        "Rendered 0:34",
        "Rendered 0:07 of pattern 1",
        "Exported the song to mix.wav",
        "Saved the song",
        "Undid the last action in FL",
        "Measured the render: peak -4.0 dBFS, -9.8 LUFS, lows 20 dB over mids, 122 BPM, key A",
    ]


def _song_span(seconds: float) -> RenderSpan:
    return RenderSpan(source="song", beats=seconds * 2, tempo_bpm=120.0, seconds=seconds)


def test_sentences_use_what_fl_returned_not_what_was_asked():
    """FL snaps values (tempo to 1/1000 BPM, a stepped control to its steps), a replace
    reports the count FL has afterwards, and an effect is named from FL's readback."""
    actions = log()
    actions.called("set_tempo", {"bpm": 122.00049}, 122.0)
    actions.called(
        "set_plugin_param",
        {"channel": 4, "param": "cutoff", "value": 0.6849},
        PluginParam(index=0, name="cutoff", value=0.7, display="0.7", kind="continuous"),
    )
    volume(actions, 0, 0.6)
    note_write(actions, 3, 16, "replace", total=12)  # FL kept 12: two pairs landed on the same step
    other = Effect(name="Fruity Limiter", chunk=b"")
    actions.called("add_effect", {"insert_index": 0, "plugin_name": "Fruity Parametric EQ 2"}, (other,))
    assert actions.sentences() == [
        "Set tempo to 122 BPM",
        "Set FLEX Bass cutoff to 0.70",
        "Set 808 Kick volume to 0.60",
        "Replaced the notes on 808 Snare (12 notes)",
        "Changed the effects on the master; it now has Fruity Limiter",
        "Reopened FL with the changed song",
    ]


def test_reads_add_no_action():
    actions = log()
    for method in ("read_notes", "plugin_params", "channel_names", "snapshot", "tempo"):
        actions.called(method, {}, [])
    assert actions.sentences() == []


def test_check_variants_say_what_the_meters_could_tell():
    shared = check_result(master=None, start_bar=3, bars=1)
    shared.channels[0].sounded = None
    shared.channels[1].sounded = False
    actions = log()
    actions.called("check", {}, shared)
    assert actions.sentences() == ["Checked bar 3: 3 of 5 tracks sound, 1 not measurable alone, master silent"]


def test_measurements_never_print_impossible_numbers():
    silent = analysis(true_peak_dbfs=float("-inf"), integrated_lufs=float("-inf"))
    quiet_lows = analysis(
        bpm=None, key=None, spectral_balance=SpectralBalance(low_dbfs=-35.0, mid_dbfs=-30.0, high_dbfs=-40.0)
    )
    actions = log()
    actions.measured(silent, "the render")
    actions.measured(quiet_lows, "mix.wav")
    assert actions.sentences() == [
        "Measured the render: it is silent",
        "Measured mix.wav: peak -4.0 dBFS, -9.8 LUFS, lows 5 dB under mids",
    ]


def test_a_sentence_that_cannot_be_written_never_fails_the_call():
    """FL already has the change; a missing label must not turn success into an error."""

    def unreadable(channel: int) -> str:
        raise RuntimeError("names unavailable")

    actions = ActionLog(unreadable)
    volume(actions, 0, 0.6)
    actions.called("set_tempo", {"bpm": 100.0}, 100.0)
    assert actions.sentences() == ["Set tempo to 100 BPM"]


# -- grouping: changes of one kind in a row read as one sentence ---------------------


def test_a_run_of_one_kind_collapses_into_one_sentence():
    actions = log()
    actions.called("set_tempo", {"bpm": 122.0}, 122.0)
    for channel, value in [(0, 0.6), (1, 0.42), (2, 0.3), (3, 0.5), (4, 0.7)]:
        volume(actions, channel, value)
    for channel in range(5):
        note_write(actions, channel, 16, "replace", total=16)
    actions.called("render_wav", {}, RenderReceipt(path=Path("r.wav"), span=_song_span(34.0)))
    assert actions.sentences() == [
        "Set tempo to 122 BPM",
        "Set volume on 5 tracks: 808 Kick 0.60, 808 Clap 0.42, 808 HiHat 0.30, 808 Snare 0.50, FLEX Bass 0.70",
        "Replaced the notes on 5 tracks: 808 Kick (16), 808 Clap (16), 808 HiHat (16), 808 Snare (16), "
        "FLEX Bass (16)",
        "Rendered 0:34",
    ]


def test_only_changes_in_a_row_group_so_the_order_and_final_state_stay_true():
    """Review of #257: grouping across the whole call moved later changes ahead of a
    clear, a different-mode write or an undo, and told the wrong final state."""
    cleared = log()
    for channel in (0, 3, 2):
        note_write(cleared, channel, 16, "replace", total=16)
    note_write(cleared, 0, 0, "replace", total=0)
    note_write(cleared, 0, 8, "replace", total=8)
    assert cleared.sentences() == [
        "Replaced the notes on 3 tracks: 808 Kick (16), 808 Snare (16), 808 HiHat (16)",
        "Cleared the notes on 808 Kick",
        "Replaced the notes on 808 Kick (8 notes)",  # FL has 8, and the list ends saying so
    ]
    mixed = log()
    note_write(mixed, 0, 4)
    note_write(mixed, 0, 16, "replace", total=16)
    note_write(mixed, 0, 4, total=20)
    note_write(mixed, 0, 4, total=24)
    assert mixed.sentences() == [
        "Wrote 4 notes to 808 Kick",
        "Replaced the notes on 808 Kick (16 notes)",
        "Wrote 4 notes to 808 Kick",
        "Wrote 4 notes to 808 Kick",
    ]
    undone = log()
    for bpm in (100.0, 110.0):
        undone.called("set_tempo", {"bpm": bpm}, bpm)
    undone.called("undo", {}, None)
    undone.called("set_tempo", {"bpm": 120.0}, 120.0)
    assert undone.sentences() == [
        "Set tempo to 100 BPM",
        "Set tempo to 110 BPM",
        "Undid the last action in FL",
        "Set tempo to 120 BPM",
    ]


def test_an_interleaved_pass_keeps_every_change_in_order():
    actions = log()
    for channel in range(3):
        note_write(actions, channel, 16, "replace", total=16)
        volume(actions, channel, 0.5)
    assert actions.sentences() == [
        "Replaced the notes on 808 Kick (16 notes)", "Set 808 Kick volume to 0.50",
        "Replaced the notes on 808 Clap (16 notes)", "Set 808 Clap volume to 0.50",
        "Replaced the notes on 808 HiHat (16 notes)", "Set 808 HiHat volume to 0.50",
    ]


def test_two_of_a_kind_stay_separate():
    actions = log()
    for channel in (0, 1):
        volume(actions, channel, 0.5)
    assert actions.sentences() == ["Set 808 Kick volume to 0.50", "Set 808 Clap volume to 0.50"]
    assert GROUP_AT == 3


def test_a_run_keeps_each_targets_latest_value_and_adds_up_merges():
    actions = log()
    for value in (0.2, 0.4, 0.6):
        volume(actions, 0, value)
    for wrote in (16, 8, 4):
        note_write(actions, 0, wrote)
    for bpm in (100.0, 110.0, 120.0):
        actions.called("set_tempo", {"bpm": bpm}, bpm)
    assert actions.sentences() == ["Set 808 Kick volume to 0.60", "Wrote 28 notes to 808 Kick", "Set tempo to 120 BPM"]


def test_changes_are_told_apart_by_channel_and_pattern_not_by_name():
    """Review of #257: two channels named alike merged into one item and miscounted."""
    names = {0: "Sampler", 1: "Kick", 2: "Sampler"}
    actions = ActionLog(lambda channel: names[channel])
    for channel, value in [(0, 0.9), (1, 0.5), (2, 0.3)]:
        volume(actions, channel, value)
    for pattern in (1, 2, 3):
        note_write(actions, 1, 8, "replace", total=8, pattern=pattern, track="Kick")
    assert actions.sentences() == [
        "Set volume on 3 tracks: Sampler (channel 0) 0.90, Kick 0.50, Sampler (channel 2) 0.30",
        "Replaced the notes on 1 track: Kick (8), Kick in pattern 2 (8), Kick in pattern 3 (8)",
    ]


def test_parameter_sweeps_group_per_track():
    actions = log()
    for index, (name, value) in enumerate([("cutoff", 0.68), ("resonance", 0.3), ("attack", 0.1)]):
        actions.called(
            "set_plugin_param",
            {"channel": 4, "param": name, "value": value},
            PluginParam(index=index, name=name, value=value, display=str(value), kind="continuous"),
        )
    assert actions.sentences() == ["Set 3 parameters on FLEX Bass: cutoff 0.68, resonance 0.30, attack 0.10"]


def test_repeats_are_counted_and_the_list_is_bounded():
    """Review of #257: `while True: fl.undo()` grew the list without bound (time in fl
    calls is not charged to the cell)."""
    actions = log()
    for _ in range(5):
        actions.called("undo", {}, None)
    for _ in range(3):
        actions.called("save_project", {}, None)
    assert actions.sentences() == ["Undid 5 actions in FL", "Saved the song (3 times)"]

    flood = log()
    for i in range(1000):
        flood.called("set_tempo", {"bpm": 100.0 + i}, 100.0 + i)
        flood.called("undo", {}, None)
    listed = flood.sentences()
    assert len(listed) == MAX_ACTIONS + 1 == 201
    assert listed[:2] == ["Set tempo to 100 BPM", "Undid the last action in FL"]
    assert listed[-1] == "and 1800 more actions"


def test_a_file_edit_blocks_changes_count_only_once_fl_reopened_with_them():
    """Review of #257: changes inside fl.file_edit() reach FL when the block closes;
    a failed reopen must not list them."""
    applied = log()
    applied.hold()
    applied.called(
        "set_automation", {}, AutomationResult(channel=4, points=[AutomationPoint(position=0, value=0.5)] * 2)
    )
    assert applied.sentences() == [], "nothing is listed while the block is open"
    applied.release(applied=True)
    assert applied.sentences() == ["Replaced the automation on FLEX Bass (2 points)", "Reopened FL with the changed song"]

    failed = log()
    failed.hold()
    failed.hold()
    failed.called(
        "set_automation", {}, AutomationResult(channel=4, points=[AutomationPoint(position=0, value=0.5)])
    )
    failed.release(applied=True)
    failed.release(applied=False)
    assert failed.sentences() == []


def test_groups_list_every_item_and_only_pathological_sentences_are_cut():
    """The UI shows one line and expands on click, so a sentence carries every item;
    only a runaway size (a name thousands of characters long) is cut."""
    actions = ActionLog(lambda channel: f"Track number {channel} with a long descriptive name")
    for channel in range(12):
        volume(actions, channel, 0.5)
    note_write(actions, 0, 4, track="A" * 5000)
    grouped, runaway = actions.sentences()
    assert grouped == "Set volume on 12 tracks: " + ", ".join(
        f"Track number {channel} with a long descriptive name 0.50" for channel in range(12)
    )
    assert len(runaway) == MAX_CHARS == 2000 and runaway.endswith("...")


# -- run: the server records every fl call it serves for the cell --------------------


@pytest.fixture
def session(tmp_path) -> FakeSession:
    return FakeSession(store=Store("actions", root=tmp_path / "store"), channels=DEFAULT_CHANNELS)


@pytest.fixture
def host(session, tmp_path):
    kernel = KernelHost(session, Workspace.create(tmp_path / "kernel"), PosixLauncher(), os.environ)
    yield kernel
    kernel.close()


def run(host: KernelHost, code: str):
    return server.run(code, _ctx(kernel=host))


def test_a_cell_reports_its_writes_and_keeps_them_past_a_mid_cell_error(host, session):
    result = run(
        host,
        "fl.set_tempo(122)\n"
        "kick = theory.drums('x...x...x...x...' * 4, key='C1')\n"
        "fl.track('Kick').write(kick, mode='replace')\n"
        "fl.track('Bass').set_channel_levels(volume=0.6)\n"
        "fl.track('Bass').set_param('Cutoff', 0.68)\n"
        "fl.track('Lead').notes()\n"  # a read: no action
        "fl.track('Nope')\n"  # raises: the cell stops here
        "fl.set_tempo(90)\n",
    )
    assert result.outcome == "error" and "No channel matching 'Nope'" in result.traceback
    assert result.actions == [
        "Set tempo to 122 BPM",
        "Replaced the notes on Kick (16 notes)",
        "Set Bass volume to 0.60",
        "Set Bass Cutoff to 0.68",
    ]


def test_a_refused_call_is_not_an_action(host, session):
    session.fail_next_write = errors.ToolError(
        "note_plane_unavailable", "FL Studio refused the note edit, so nothing was written.", "Retry it."
    )
    result = run(
        host,
        "try:\n"
        "    fl.track('Kick').write([Note(key=36, start=0, length=1)])\n"
        "except Exception as exc:\n"
        "    print('refused')\n"
        "fl.track('Kick').write([Note(key=36, start=0, length=1)])\n",
    )
    assert result.outcome == "ok" and result.stdout == "refused\n"
    assert result.actions == ["Wrote 1 note to Kick"]


def test_a_cell_with_only_reads_or_no_fl_calls_has_no_actions(host):
    assert run(host, "fl.tempo, fl.channels, fl.track('Bass').params()").actions == []
    assert run(host, "1 + 1").actions == []


def test_actions_belong_to_the_cell_that_made_them(host):
    assert run(host, "fl.set_tempo(100)").actions == ["Set tempo to 100 BPM"]
    assert run(host, "fl.set_tempo(101)").actions == ["Set tempo to 101 BPM"]


def test_a_lost_kernel_still_reports_what_fl_already_has(host):
    result = run(host, "fl.track('Kick').write([Note(key=36, start=0, length=1)])\nimport os\nos._exit(3)")
    assert result.outcome == "kernel_lost"
    assert result.actions == ["Wrote 1 note to Kick"]


def test_an_interrupted_cell_still_reports_what_fl_already_has(session, tmp_path):
    kernel = KernelHost(
        session, Workspace.create(tmp_path / "slow"), PosixLauncher(), os.environ, cell_timeout=1.0, interrupt_grace=2.0
    )
    try:
        result = run(kernel, "fl.set_tempo(100)\nfl.track('Kick').write([Note(36, 0, 1)])\nwhile True:\n    pass")
    finally:
        kernel.close()
    assert result.outcome == "interrupted"
    assert result.actions == ["Set tempo to 100 BPM", "Wrote 1 note to Kick"]


def test_check_and_render_from_code_read_like_the_tools(host):
    result = run(host, "fl.check(bars=1, start_bar=2)\nfl.render('mix.wav')")
    assert result.actions == ["Checked bar 2: 1 of 1 tracks sound, master -6.0 dBFS", "Rendered 0:07 of pattern 1"]


# -- the other tools -----------------------------------------------------------------


def _ctx(**context: object) -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=dict(context)))


def test_the_check_tool_reports_what_it_measured(session):
    checked = server.check(_ctx(session=session), bars=2, start_bar=9)
    assert checked.actions == ["Checked bars 9-10: 1 of 1 tracks sound, master -6.0 dBFS"]
    assert checked.summary == "1 of 1 channels sounded in bars 9-10."


class _Measured(FakeSession):
    """Render writes a real (silent-but-valid) WAV; analyze returns fixed numbers."""

    def render_wav(self, output: Path) -> RenderReceipt:
        import wave

        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes(b"\x00\x00" * 441)
        return RenderReceipt(path=output, span=_song_span(34.0))

    def analyze(self, wav: Path) -> AudioAnalysis:
        return analysis()


@pytest.fixture
def measured(tmp_path) -> SimpleNamespace:
    session = _Measured(store=Store("measured", root=tmp_path / "store"), channels=DEFAULT_CHANNELS)
    kernel = SimpleNamespace(workspace=Workspace.create(tmp_path / "kernel"))
    return _ctx(fl=session, session=session, kernel=kernel)


def test_the_render_tool_reports_the_render(measured):
    assert server.render(measured).actions == ["Rendered 0:34"]


def test_analyze_reports_its_measurement_and_the_render_it_needed(measured, tmp_path):
    fresh = server.analyze(measured)
    assert fresh.actions == [
        "Rendered 0:34",
        "Measured the render: peak -4.0 dBFS, -9.8 LUFS, lows 20 dB over mids, 122 BPM, key A",
    ]
    assert server.analyze(measured).actions == [
        "Measured the render: peak -4.0 dBFS, -9.8 LUFS, lows 20 dB over mids, 122 BPM, key A"
    ]
    named = server.analyze(measured, wav=str(tmp_path / "mix.wav"))
    assert named.actions == ["Measured mix.wav: peak -4.0 dBFS, -9.8 LUFS, lows 20 dB over mids, 122 BPM, key A"]
