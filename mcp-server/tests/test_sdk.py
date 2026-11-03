"""v2 sdk: immediate execution, confirmed results, plain-Python errors.

Ported from v1's test_kernel.py, rewritten to immediate semantics: what was
"queue then flush" is now "applies on the call". v1 coverage that immediate
mode drops by design (noted per docs/21 reporting duty):

- queued-not-applied-until-flush / cell() flush-on-exit / no-flush-on-raise:
  there is no queue; the write IS the application.
- flush-stops-on-first-rejection: each call is atomic; a rejection raises at
  the failing line and later calls simply never run.
- stale-handle generation machinery: no cached manifest/handles exist; every
  read re-resolves against the session and returns confirmed state.
- undo_last returning the undone ActionEntry + shadow stale-marking: v2
  ``undo()`` sends FL's own undo; the store keeps its provenance-labeled
  claim (a shadow claim is exactly that - docs/21 provenance rule).
- bootstrap/manifest persistence: no manifest; channels come from snapshot().
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest
from flpkit import effects_at
from sdk_fakes import DEFAULT_CHANNELS, FakeSession
from session_fakes import make_session

from fl_mcp import errors, sdk, theory
from fl_mcp.schemas import AutomationPoint, ChannelLevels, Note, Target, WriteResult
from fl_mcp.sdk import ChannelNotFound, connect
from fl_mcp.session.link import TRANSIENT_RETRY_DELAYS


@pytest.fixture
def fl(fake_session):
    return connect(fake_session)


def make_notes(*keys):
    return [Note(key=k, start=i, length=1) for i, k in enumerate(keys)]


# -- connect ------------------------------------------------------------------


def test_connect_uses_the_injected_session(fake_session):
    fl = connect(fake_session)
    assert [c.name for c in fl.channels] == ["Kick", "Bass", "Lead"]


# -- track resolution ----------------------------------------------------------


def test_track_resolves_by_index(fl):
    assert fl.track(2).channel == 2


def test_track_resolves_by_exact_name(fl):
    assert fl.track("Bass").channel == 1


def test_track_resolves_by_unambiguous_substring(fl):
    assert fl.track("bas").channel == 1


def test_track_exact_name_beats_substring(tmp_path, control):
    from fl_mcp.schemas import Channel
    from fl_mcp.store import Store

    session = FakeSession(
        store=Store("test", root=tmp_path),
        channels=[Channel(index=0, name="Bass"), Channel(index=1, name="Bass 2")],
        control=control,
    )
    assert connect(session).track("Bass").channel == 0


def test_track_unknown_name_lists_the_real_channels(fl):
    with pytest.raises(ChannelNotFound) as exc_info:
        fl.track("Nope")
    message = str(exc_info.value)
    assert "Kick" in message and "Bass" in message and "Lead" in message


def test_track_ambiguous_substring_raises(fl):
    with pytest.raises(ChannelNotFound) as exc_info:
        fl.track("a")  # matches Bass and Lead
    assert "ambiguous" in str(exc_info.value)


def test_track_out_of_range_index_raises(fl):
    with pytest.raises(ChannelNotFound):
        fl.track(9)


def test_channel_not_found_is_a_value_error(fl):
    with pytest.raises(ValueError):
        fl.track("Nope")


# -- writes: immediate, confirmed, atomic --------------------------------------


def test_write_applies_immediately_no_flush_exists(fl, fake_session):
    receipt = fl.track("Bass").write(make_notes("C4"))

    # v1 queued until flush(); v2 has no flush - the state is already there.
    assert fake_session.read_notes(Target(pattern=1, channel=1))[0].key == 60
    assert receipt.total == 1
    assert not hasattr(fl, "flush")
    assert not hasattr(fl, "cell")


def test_write_result_says_what_was_written_where_and_the_total(fl):
    fl.track("Bass").write(make_notes("C4"), mode="replace")
    result = fl.track("Bass").write(make_notes("E4", "G4"), mode="merge")

    # The whole target's state, not the request echo; named like the model named it.
    assert result == WriteResult(wrote=2, track="Bass", pattern=1, mode="merge", total=3)


def test_write_mode_defaults_to_merge(fl):
    fl.track("Bass").write(make_notes("C4"))
    receipt = fl.track("Bass").write(make_notes("E4"))
    assert receipt.total == 2


def test_write_normalizes_raw_note_inputs_at_the_sdk_boundary(fl):
    receipt = fl.track("Bass").write([{"key": "C4", "start": 0, "length": 1}], mode="replace")

    assert receipt.total == 1
    assert fl.track("Bass").notes()[0] == Note(key=60, start=0, length=1)


def test_invalid_note_range_raises_typed_sdk_error_before_write(fl, fake_session):
    with pytest.raises(errors.ToolError) as exc_info:
        fl.track("Bass").write([{"key": 128, "start": 0, "length": 1}])

    assert exc_info.value.category == "invalid_argument"
    assert "Track.write" in exc_info.value.message
    assert "key" in exc_info.value.message
    assert "Fix the argument" in exc_info.value.fix
    assert fake_session.store.last_action() is None


def test_invalid_write_mode_raises_typed_sdk_error_before_write(fl, fake_session):
    with pytest.raises(errors.ToolError) as exc_info:
        fl.track("Bass").write(make_notes("C4"), mode="append")

    assert exc_info.value.category == "invalid_argument"
    assert "Track.write" in exc_info.value.message
    assert "merge" in exc_info.value.message
    assert fake_session.store.last_action() is None


def test_rejected_write_raises_and_applies_nothing(fl, fake_session):
    fake_session.fail_next_write = errors.note_plane_unavailable(1, 2.0)

    with pytest.raises(errors.ToolError) as exc_info:
        fl.track("Bass").write(make_notes("C4"))

    assert exc_info.value.category == "note_plane_unavailable"
    assert fl.track("Bass").notes() == []
    assert fake_session.store.last_action() is None  # nothing recorded either


def test_rejection_halts_a_script_before_later_writes(fl, fake_session):
    """v1's flush-stops-on-first-rejection, in immediate form: the raise stops
    the caller's own code, so the Lead write below never happens."""
    fake_session.fail_next_write = errors.note_plane_unavailable(1, 2.0)

    with pytest.raises(errors.ToolError):
        fl.track("Bass").write(make_notes("C4"))
        fl.track("Lead").write(make_notes("E4"))  # unreached, exactly like plain Python

    assert fl.track("Lead").notes() == []


def test_clear_applies_immediately(fl):
    fl.track("Bass").write(make_notes("C4"))
    receipt = fl.track("Bass").clear()
    assert receipt.total == 0
    assert fl.track("Bass").notes() == []


def test_track_automation_returns_the_confirmed_points(fl):
    points = [AutomationPoint(position=0, value=0.2), AutomationPoint(position=4, value=0.8)]

    receipt = fl.track("Bass").set_automation(points)

    assert receipt.channel == 1
    assert receipt.points == points


def test_automation_points_normalize_and_validate_at_the_sdk_boundary(fl):
    receipt = fl.track("Bass").set_automation([{"position": 0, "value": 0.2}])
    assert receipt.points == [AutomationPoint(position=0, value=0.2)]

    with pytest.raises(errors.ToolError) as exc_info:
        fl.track("Bass").set_automation([{"position": 0, "value": 2.0}])
    assert exc_info.value.category == "invalid_argument"
    assert "Track.set_automation" in exc_info.value.message
    assert "value" in exc_info.value.message


# -- channel levels -----------------------------------------------------------


def test_track_set_channel_levels_validates_at_the_sdk_boundary(fl, fake_session):
    with pytest.raises(errors.ToolError) as exc_info:
        fl.track("Bass").set_channel_levels(volume=1.1)

    assert exc_info.value.category == "invalid_argument"
    assert "Track.set_channel_levels" in exc_info.value.message
    assert "volume" in exc_info.value.message
    assert fake_session.level_calls == []

    with pytest.raises(errors.ToolError) as exc_info:
        fl.track("Bass").set_channel_levels(pan=-1.1)

    assert exc_info.value.category == "invalid_argument"
    assert "pan" in exc_info.value.message
    assert fake_session.level_calls == []

    with pytest.raises(errors.ToolError) as exc_info:
        fl.track("Bass").set_channel_levels(pitch=49)

    assert exc_info.value.category == "invalid_argument"
    assert "pitch" in exc_info.value.message
    assert fake_session.level_calls == []


def test_track_set_channel_levels_returns_the_confirmed_receipt(fl, fake_session):
    confirmed = fl.track("Bass").set_channel_levels(volume=0.6, pan=-0.3, pitch=12)

    assert confirmed == ChannelLevels(channel=1, volume=0.6, pan=-0.3, pitch_semitones=12)
    assert fake_session.level_calls == [(1, 0.6, -0.3, 12)]


# -- effects ------------------------------------------------------------------


def test_add_effect_replays_the_checked_in_fl_authored_pair(tmp_path, monkeypatch):
    evidence = Path(__file__).parent / "evidence/plugin-effect"
    candidate = tmp_path / "candidate.flp"
    candidate.write_bytes((evidence / "baseline.flp").read_bytes())
    session, _ = make_session(project=candidate)
    reloads: list[bool] = []
    monkeypatch.setattr(session, "_reload_fl", lambda: reloads.append(True))

    confirmed = connect(session).add_effect(0, "Fruity Parametric EQ 2")

    expected = tuple(effects_at(evidence / "after-add-parametric-eq2-on-master.flp", 0, slot=1))
    assert confirmed == tuple(effects_at(candidate, 0, slot=1))
    assert confirmed == expected  # Each opaque effect chunk is byte-exact to the FL-authored pair.
    assert reloads == [True]


def test_track_pattern_is_validated_at_the_sdk_boundary(fl):
    with pytest.raises(errors.ToolError) as exc_info:
        fl.track("Bass", pattern=0)

    assert exc_info.value.category == "invalid_argument"
    assert "fl.track" in exc_info.value.message
    assert "pattern" in exc_info.value.message


def test_track_pattern_targets_are_independent(fl):
    fl.track("Bass").write(make_notes("C4"))
    sdk.Track(fl, 1, pattern=2).write(make_notes("G4"))

    assert [n.key for n in fl.track("Bass").notes()] == [60]
    assert [n.key for n in sdk.Track(fl, 1, pattern=2).notes()] == [67]


def test_writes_record_history_write_through(fl, fake_session):
    fl.track("Bass").write(make_notes("C4"), mode="replace")
    fl.track("Bass").clear()

    ops = [e.op for e in fake_session.store.history()]
    assert ops == ["add_notes", "clear_notes"]
    assert fake_session.store.last_action().backend == "project-write"


# -- reads return confirmed state ---------------------------------------------


def test_notes_returns_confirmed_state_from_the_store(fl):
    fl.track("Bass").write(make_notes("C4", "E4"))
    assert {n.key for n in fl.track("Bass").notes()} == {60, 64}


def test_notes_on_untouched_target_is_empty(fl):
    assert fl.track("Lead").notes() == []


def test_channels_lists_what_fl_has(fl):
    assert [c.name for c in fl.channels] == ["Kick", "Bass", "Lead"]


def test_check_reports_what_the_session_heard(fl):
    result = fl.check(bars=1, start_bar=2)
    assert (result.start_bar, result.bars) == (2, 1)
    assert result.summary == "1 of 1 channels sounded in bars 2-2."


# -- tempo ---------------------------------------------------------------------


def test_set_tempo_returns_the_readback_not_the_echo(fl):
    result = fl.set_tempo(123.4567)
    assert result == 123.457  # FL's BPM-x-1000 quantization, not the request
    assert result != 123.4567


def test_tempo_property_reads_confirmed_state(fl):
    fl.set_tempo(128)
    assert fl.tempo == 128.0


def test_tempo_unconfirmed_raises_instead_of_guessing(tmp_path):
    from fl_mcp.store import Store

    session = FakeSession(store=Store("test", root=tmp_path), channels=DEFAULT_CHANNELS, tempo=None)
    with pytest.raises(errors.ToolError) as exc_info:
        _ = connect(session).tempo
    assert exc_info.value.category == "control_plane_unavailable"


def test_render_returns_the_confirmed_wav_path(fl, tmp_path):
    output = tmp_path / "mix.wav"

    assert fl.render(output) == output
    assert output.is_file()


def test_save_sends_the_project_save_operation(fl, control):
    assert fl.save() is None
    assert control.ops == ["ctl.save"]


def test_read_wav_loads_synthetic_pcm(fl, tmp_path):
    path = tmp_path / "samples.wav"
    frames = np.array([[-32768, 0], [0, 32767]], dtype="<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(frames.tobytes())

    samples, sample_rate = fl.read_wav(path)

    assert sample_rate == 44100
    np.testing.assert_allclose(samples, [[-1.0, 0.0], [0.0, 32767 / 32768]])


# -- plugin params -------------------------------------------------------------


def test_params_limit_and_set_param_validate_at_the_sdk_boundary(fl):
    assert [param.name for param in fl.track("Bass").params(limit=1)] == ["Cutoff"]

    with pytest.raises(errors.ToolError) as exc_info:
        fl.track("Bass").params(limit=-1)
    assert exc_info.value.category == "invalid_argument"
    assert "Track.params" in exc_info.value.message
    assert "limit" in exc_info.value.message

    with pytest.raises(errors.ToolError) as exc_info:
        fl.track("Bass").set_param(0, 2.0)
    assert exc_info.value.category == "invalid_argument"
    assert "Track.set_param" in exc_info.value.message
    assert "value" in exc_info.value.message


def test_set_param_accepts_index_or_name_and_returns_readback(fl):
    assert fl.track("Bass").set_param(0, 0.75) == sdk.PluginParam(
        index=0, name="Cutoff", value=0.75, display="0.75", kind="continuous"
    )
    assert fl.track("Bass").set_param("Cutoff", 0.25) == sdk.PluginParam(
        index=0, name="Cutoff", value=0.25, display="0.25", kind="continuous"
    )


# -- transport -----------------------------------------------------------------


def test_undo_sends_fl_undo(fl, control):
    fl.undo()
    assert control.ops == ["general.undo"]


def test_undo_requires_control_plane(tmp_path):
    from fl_mcp.store import Store

    session = FakeSession(store=Store("test", root=tmp_path), channels=DEFAULT_CHANNELS, control=None)
    with pytest.raises(errors.ToolError) as exc_info:
        connect(session).undo()
    assert exc_info.value.category == "control_plane_unavailable"


def test_a_persistent_operation_unsafe_surfaces_once_retries_run_out(fl, control):
    """docs/14: FL rejects some mutating ops mid-engine-cycle with its own
    'Operation unsafe at current time'. The engine retries it (it applied
    nothing); only a refusal that outlives every retry reaches the model, as
    the dedicated retryable category with a plain sentence."""
    control.fail_with = "RuntimeError: Operation unsafe at current time"
    with pytest.raises(errors.ToolError) as exc_info:
        fl.undo()
    assert exc_info.value.category == "operation_unsafe"
    assert exc_info.value.retryable is True
    assert exc_info.value.message == (
        "FL Studio stayed busy for several seconds and did not take this action, so nothing changed."
    )
    assert control.ops == ["general.undo"] * (len(TRANSIENT_RETRY_DELAYS) + 1)


def test_other_control_failures_surface_as_fl_api_error(fl, control):
    control.fail_with = "TypeError: something real"
    with pytest.raises(errors.ToolError) as exc_info:
        fl.undo()
    assert exc_info.value.category == "fl_api_error"
    assert "something real" in exc_info.value.message


# -- theory integration (fl_mcp/theory.py, imported untouched) -----------------


def test_drums_pattern_writes_through_track(fl):
    # theory.drums returns v2 schemas.Note since the cutover, so its output
    # composes with Track.write directly - no conversion step exists anymore.
    receipt = fl.track("Kick").write(theory.drums("x---x---x---x---"), mode="replace")
    assert receipt.total == 4


def test_chord_composes_with_write(fl):
    notes = [Note(key=k, start=0, length=4) for k in theory.chord("C4", "maj7")]
    receipt = fl.track("Bass").write(notes, mode="replace")
    assert receipt.total == 4
    assert {n.key for n in fl.track("Bass").notes()} == {60, 64, 67, 71}


def test_transpose_round_trips_through_confirmed_reads(fl):
    fl.track("Lead").write(make_notes("C4", "E4"), mode="replace")
    shifted = theory.transpose(fl.track("Lead").notes(), 12)
    receipt = fl.track("Lead").write(shifted, mode="replace")
    assert receipt.total == 2
    assert {n.key for n in fl.track("Lead").notes()} == {72, 76}
