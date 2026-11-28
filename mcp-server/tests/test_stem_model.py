"""fl.separate's pipeline around the stem network (stem_model.py), with stand-in networks.

The real network is a 157 MB download that CI does not have, so these hold what the code
around it promises: the stems add back up to the mix in every window and across the
crossfades, the network sees frames and frequencies the right way round, the files appear
whole or not at all, and a split that would outlast the run stops between windows.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
import soundfile
from session_fakes import make_session

from fl_mcp import budget, errors, sdk, stem_model
from fl_mcp.kernel import wire
from fl_mcp.stem_model import BINS, CHUNK, FRAME, SAMPLE_RATE, STEMS

# Long enough for three windows, so two crossfades.
LONG = 2 * stem_model.STEP + stem_model.FADE + 1000


def _random(magnitude: np.ndarray) -> np.ndarray:
    """Estimates with no relation to the mix: whatever the network says, the masks sum to one."""
    rng = np.random.default_rng(int(magnitude.sum()) % 1000)
    return rng.uniform(0.0, 1.0, (len(STEMS), *magnitude.shape)).astype(np.float32)


def _all_vocals(magnitude: np.ndarray) -> np.ndarray:
    return np.stack([magnitude, *[np.zeros_like(magnitude)] * (len(STEMS) - 1)])


def _low_is_bass(magnitude: np.ndarray) -> np.ndarray:
    """Bins below 500 Hz are bass, the rest other: what a network that read the input's
    axes the wrong way round could not reproduce."""
    out = np.zeros((len(STEMS), *magnitude.shape), np.float32)
    cut = int(500 * FRAME / SAMPLE_RATE)
    out[STEMS.index("bass"), :, :, :cut] = 1.0
    out[STEMS.index("other"), :, :, cut:] = 1.0
    return out


def _noise(frames: int, channels: int = 2) -> np.ndarray:
    return (np.random.default_rng(1).standard_normal((frames, channels)) * 0.1).astype(np.float32)


def _joined(samples: np.ndarray, net) -> np.ndarray:
    return np.concatenate(list(stem_model.split(samples, net)), axis=1)


@pytest.mark.parametrize("frames", [1000, CHUNK, LONG])
def test_the_stems_add_up_to_the_mix_whatever_the_network_says(frames):
    mix = _noise(frames)
    stems = _joined(mix, _random)

    assert stems.shape == (len(STEMS), frames, 2)
    np.testing.assert_allclose(stems.sum(axis=0), mix, atol=1e-5)


def test_one_stem_that_takes_everything_is_the_mix_across_windows():
    """The STFT, its inverse and the crossfades reconstruct the input exactly."""
    mix = _noise(LONG)
    stems = _joined(mix, _all_vocals)

    np.testing.assert_allclose(stems[0], mix, atol=1e-5)
    assert np.abs(stems[1:]).max() < 1e-5


def test_the_network_sees_frequencies_along_its_third_axis():
    t = np.arange(SAMPLE_RATE * 3) / SAMPLE_RATE
    low, high = np.sin(2 * np.pi * 110 * t), np.sin(2 * np.pi * 3000 * t)
    mix = np.stack([low + high] * 2, axis=1).astype(np.float32) * 0.4
    stems = _joined(mix, _low_is_bass)
    bass, other = stems[STEMS.index("bass")][:, 0], stems[STEMS.index("other")][:, 0]

    assert np.corrcoef(bass, low)[0, 1] > 0.999 and np.corrcoef(other, high)[0, 1] > 0.999


def test_the_network_gets_one_window_of_the_lowest_bins():
    seen = []

    def net(magnitude):
        seen.append(magnitude.shape)
        return _random(magnitude)

    list(stem_model.split(_noise(LONG), net))
    assert seen == [(1, stem_model.FRAMES, BINS, 2)] * 3


def test_mono_and_other_rates_come_back_as_mono_at_44k(tmp_path):
    mono = _noise(22050, channels=1)
    paths = {stem: tmp_path / f"x-{stem}.wav" for stem in STEMS}
    stem_model.write(mono, 22050, _all_vocals, paths)

    vocals, rate = soundfile.read(paths["vocals"], always_2d=True)
    assert rate == SAMPLE_RATE and vocals.shape == (44100, 1)
    assert soundfile.info(paths["vocals"]).subtype == "PCM_16"
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(p.name for p in paths.values())


def test_more_than_two_channels_is_refused():
    with pytest.raises(ValueError, match="3 channels"):
        stem_model.stereo(_noise(100, channels=3), SAMPLE_RATE)


def test_a_split_that_would_outlast_the_run_stops_between_windows_and_leaves_no_files(tmp_path):
    def slow(magnitude):
        time.sleep(0.2)
        return _random(magnitude)

    paths = {stem: tmp_path / f"x-{stem}.wav" for stem in STEMS}
    nearly_spent = budget.RunBudget(60.0, time.monotonic() + 0.3)
    with budget.serving(nearly_spent), pytest.raises(errors.ToolError) as refused:
        stem_model.write(_noise(LONG), SAMPLE_RATE, slow, paths)

    assert refused.value.category == "run_budget_exceeded"
    assert "so it was stopped and no stems were written" in refused.value.message
    assert list(tmp_path.iterdir()) == []


def test_the_first_window_loading_the_model_does_not_refuse_a_split_that_fits():
    """On a process's first split the first network call also loads the model (39 s from a
    freshly restored disk, against about 3 s a window after it), so the estimate must not
    extrapolate the rest from that window: a 4.5 min file was refused as needing 736 s
    with 573 s left, and split in 77 s once the model was loaded (2026-11-27)."""
    calls = []

    def loads_then_fast(magnitude):
        calls.append(1)
        time.sleep(1.0 if len(calls) == 1 else 0.01)
        return _random(magnitude)

    six_windows = 5 * stem_model.STEP + CHUNK
    enough = budget.RunBudget(60.0, time.monotonic() + 4.0)
    with budget.serving(enough):
        stems = _joined(_noise(six_windows), loads_then_fast)

    assert len(calls) == 6 and stems.shape == (len(STEMS), six_windows, 2)


def test_the_model_says_why_it_cannot_run(tmp_path, monkeypatch):
    model = stem_model.Model(tmp_path)
    assert "not installed" in model.unavailable()
    (tmp_path / stem_model.MODEL_FILE).write_bytes(b"")
    monkeypatch.setattr(stem_model.importlib.util, "find_spec", lambda name: None)
    assert "cannot run on this machine" in model.unavailable()
    with pytest.raises(stem_model.ModelMissing, match="cannot run"):
        model(np.zeros((1, stem_model.FRAMES, BINS, 2), np.float32))


# -- the session, the SDK and the wire ----------------------------------------------------


def test_the_session_writes_one_file_per_stem_named_after_output(tmp_path, monkeypatch):
    class Ready:
        def unavailable(self):
            return None

        def __call__(self, magnitude):
            return _all_vocals(magnitude)

    monkeypatch.setattr(stem_model, "model", lambda folder: Ready())
    session, _ = make_session()
    wav = tmp_path / "in.wav"
    soundfile.write(wav, _noise(5000), SAMPLE_RATE)

    paths = session.separate(wav.read_bytes(), tmp_path / "song.wav")
    assert {stem: p.name for stem, p in paths.items()} == {stem: f"song-{stem}.wav" for stem in STEMS}
    assert all(p.is_file() for p in paths.values())
    with pytest.raises(errors.ToolError, match="not an audio file") as raised:
        session.separate(b"nope", tmp_path / "song.wav")
    assert raised.value.category == "invalid_argument"


def test_without_the_model_the_session_says_how_to_get_it(tmp_path, monkeypatch):
    monkeypatch.setattr(stem_model, "model", lambda folder: stem_model.Model(tmp_path))
    session, _ = make_session()
    with pytest.raises(errors.ToolError, match="not installed") as raised:
        session.separate(b"RIFF", tmp_path / "song.wav")
    assert "fl-mcp-install" in raised.value.fix


def test_the_sdk_sends_the_file_and_names_the_stems_after_it(tmp_path):
    sent = []

    class Session:
        def separate(self, audio, output):
            sent.append((audio, output))
            return {}

    fl = sdk.FL(Session())
    upload = tmp_path / "My Song (final mix).mp3"
    upload.write_bytes(b"ID3")

    fl.separate(upload)
    fl.separate(str(upload), "take.wav")
    assert sent == [(b"ID3", Path("My Song _final mix.wav")), (b"ID3", Path("take.wav"))]
    with pytest.raises(errors.ToolError, match="audio file in your workspace"):
        fl.separate(tmp_path / "missing.wav")


def test_the_wire_carries_the_audio_and_the_stems_paths():
    call = wire.encode_call("separate", (b"\x00\x01", Path("song.wav")), {})
    assert wire.decode_call(call) == {"audio": b"\x00\x01", "output": Path("song.wav")}
    paths = {stem: Path(f"/renders/song-{stem}.wav") for stem in STEMS}
    assert wire.decode_result("separate", wire.encode_result("separate", paths)) == paths
