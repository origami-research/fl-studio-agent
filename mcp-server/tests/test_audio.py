from __future__ import annotations

import subprocess
import sys
import wave

import audio_oracle
import numpy as np
import pytest

from fl_mcp import audio
from fl_mcp.audio import analyze_wav


def test_analysis_identifies_a440_and_its_loudness(tmp_path):
    sample_rate = 48000
    samples = 0.1 * np.sin(2 * np.pi * 440 * np.arange(sample_rate * 2) / sample_rate)
    path = tmp_path / "a4.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes((samples * 32767).astype("<i2").tobytes())

    result = analyze_wav(path)

    assert result.key == "A"
    assert result.bpm is None
    assert result.integrated_lufs == pytest.approx(-23.7, abs=0.3)
    assert result.true_peak_dbfs == pytest.approx(-20.0, abs=0.1)
    assert result.spectral_balance.mid_dbfs > result.spectral_balance.low_dbfs


def test_empty_wav_raises_clear_boundary_error(tmp_path):
    path = tmp_path / "empty.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)

    with pytest.raises(ValueError, match="no complete audio frames"):
        analyze_wav(path)


def test_wav_shorter_than_its_header_is_refused(tmp_path):
    # The Bridge writes the header last from its own frame counter, so a failed write (disk
    # full) leaves a header claiming frames that are not on disk; that is not a take.
    sample_rate, claimed, present = 48000, 131072, 33768
    samples = 0.25 * np.sin(2 * np.pi * 220 * np.arange(claimed) / sample_rate)
    path = tmp_path / "truncated.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes((np.repeat(samples[:, None], 2, axis=1) * 32767).astype("<i2").tobytes())
    data_start = path.stat().st_size - claimed * 4
    path.write_bytes(path.read_bytes()[: data_start + present * 4])

    with pytest.raises(ValueError, match=f"claims {claimed} frames but only {present} are present"):
        analyze_wav(path)


def test_very_short_wav_has_no_bpm_or_key_instead_of_index_error(tmp_path):
    path = tmp_path / "click.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(np.array([1000], dtype="<i2").tobytes())

    result = analyze_wav(path)

    assert result.bpm is None
    assert result.key is None


def test_low_sample_rate_wav_without_key_band_is_analyzed_without_argmax_error(tmp_path):
    path = tmp_path / "low-rate.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(10)
        wav.writeframes(np.full(100, 1000, dtype="<i2").tobytes())

    result = analyze_wav(path)

    assert result.key is None




def _write(path, samples, sample_rate):
    frames = samples if samples.ndim == 2 else samples[:, None]
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(frames.shape[1])
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes((np.clip(frames, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def _music(seconds, sample_rate, seed=3):
    # Kick-like pulses at 120 BPM, a held A2 bass over a quieter chord, and noise: every
    # analyzer stage has signal, and the bass is the one dominant pitch.
    rng = np.random.default_rng(seed)
    t = np.arange(round(seconds * sample_rate)) / sample_rate
    beat = np.exp(-((t % 0.5) * 30)) * np.sin(2 * np.pi * 55 * t)
    chord = sum(0.05 * np.sin(2 * np.pi * f * t) for f in (277.18, 329.63, 440.0))
    left = 0.4 * beat + 0.25 * np.sin(2 * np.pi * 110 * t) + chord + 0.01 * rng.standard_normal(len(t))
    return np.stack([left, 0.8 * left + 0.02 * rng.standard_normal(len(t))], axis=1)


def _pulse_between_samples(length, centre):
    # A band-limited pulse whose true peak (1.0) falls between two samples.
    n = np.arange(length)
    return np.sinc(n - centre) * np.exp(-(((n - centre) / 400.0) ** 2))


@pytest.mark.parametrize(("seconds", "sample_rate"), [(0.5, 48000), (1.2, 44100), (1.0, 22050)])
def test_streaming_analysis_equals_the_whole_file_analyzer_on_short_renders(tmp_path, seconds, sample_rate):
    # Shorter than one spectrum segment: every measurement is the whole-file one.
    path = _write(tmp_path / "short.wav", _music(seconds, sample_rate), sample_rate)

    new, old = analyze_wav(path), audio_oracle.analyze_wav(path)
    assert new.key == old.key
    assert new.bpm == old.bpm
    assert new.integrated_lufs == pytest.approx(old.integrated_lufs, rel=1e-9)
    assert new.spectral_balance.model_dump() == pytest.approx(old.spectral_balance.model_dump(), rel=1e-9)
    # Both upsample a zero-padded window; the padding differs (one block's context versus
    # three times the render), which moves the interpolated peak by under 0.001 dB.
    assert new.true_peak_dbfs == pytest.approx(old.true_peak_dbfs, abs=1e-3)


@pytest.mark.parametrize("sample_rate", [48000, 44100])
def test_streaming_analysis_of_a_long_render_keeps_each_measurement(tmp_path, sample_rate):
    # Many blocks: loudness and tempo are block-exact (filter state and partial hops carry
    # over), true peak agrees to 0.01 dB, and the averaged spectrum keeps key and balance.
    path = _write(tmp_path / "long.wav", _music(9.0, sample_rate), sample_rate)
    new, old = analyze_wav(path), audio_oracle.analyze_wav(path)

    assert new.integrated_lufs == pytest.approx(old.integrated_lufs, abs=1e-9)
    assert new.bpm == old.bpm
    assert new.true_peak_dbfs == pytest.approx(old.true_peak_dbfs, abs=0.01)
    assert new.key == old.key == "A"
    for band in ("low_dbfs", "mid_dbfs", "high_dbfs"):
        assert getattr(new.spectral_balance, band) == pytest.approx(getattr(old.spectral_balance, band), abs=0.5)


@pytest.mark.parametrize(
    "case",
    ["pulse_on_first_seam", "pulse_on_second_seam", "pulse_at_render_start", "pulse_at_render_end", "noise", "abrupt_tone"],
)
def test_block_true_peak_matches_the_whole_signal_meter(case):
    block = audio._BLOCK
    length = block * 3 + 1234
    rng = np.random.default_rng(7)
    if case == "pulse_on_first_seam":
        mono = 0.9 * _pulse_between_samples(length, block - 0.5)
    elif case == "pulse_on_second_seam":
        mono = 0.9 * _pulse_between_samples(length, 2 * block - 0.5) + 0.01 * rng.standard_normal(length)
    elif case == "pulse_at_render_start":
        mono = 0.9 * _pulse_between_samples(length, 0.5)
    elif case == "pulse_at_render_end":
        mono = 0.9 * _pulse_between_samples(length, length - 1.5)
    elif case == "noise":
        mono = 0.3 * rng.standard_normal(length)
    else:  # a tone cut off at both ends rings past its sample peak (Gibbs) - kept as is
        mono = 0.5 * np.sin(np.pi / 2 * np.arange(length) + np.pi / 4)
    samples = np.stack([mono, -0.5 * mono], axis=1)

    # Both are band-limited estimates of the same peak; they agree to under 0.01 dB
    # (full-band noise, whose sinc tails reach furthest past a seam, is the widest case).
    assert audio._true_peak(samples) == pytest.approx(audio_oracle._true_peak(samples), rel=1e-3)


def test_true_peak_finds_an_inter_sample_peak_across_many_blocks():
    sample_rate = 44100
    count = sample_rate * 30
    n = np.arange(count)
    fade = np.minimum(1.0, np.minimum(n, count - 1 - n) / sample_rate)  # 1 s fades: no edge ringing
    # fs/4 at 45 degrees: every sample sits at 0.5/sqrt(2) of the envelope, the true peak is 0.5.
    tone = 0.5 * fade * np.sin(np.pi / 2 * n + np.pi / 4)
    samples = np.stack([tone, -tone], axis=1)
    assert np.abs(samples).max() < 0.36

    assert audio._true_peak(samples) == pytest.approx(0.5, abs=1e-3)


@pytest.mark.skipif(sys.platform == "win32", reason="resource.getrusage is POSIX-only")
def test_analysis_memory_does_not_grow_with_render_length(tmp_path):
    # A 32-bar take at 85 BPM is ~90 s; the whole-file analyzer needed >3 GB for it and
    # paged FL out on an 8 GB worker. Streamed, a 40 s render (the whole-file pass peaked
    # ~1.5 GB here) stays within a few blocks. Measured in a fresh interpreter: its peak
    # RSS growth across the call (tracemalloc would slow the per-sample filter ~10x).
    path = _write(tmp_path / "take.wav", _music(40.0, 44100), 44100)
    probe = (
        "import resource, sys; from pathlib import Path; from fl_mcp.audio import analyze_wav\n"
        "unit = 1 if sys.platform == 'darwin' else 1024\n"
        "before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit\n"
        "result = analyze_wav(Path(sys.argv[1]))\n"
        "print((resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit - before) // 2**20, result.integrated_lufs)\n"
    )
    out = subprocess.run([sys.executable, "-c", probe, str(path)], capture_output=True, text=True, check=True).stdout
    growth_mib, lufs = out.split()

    assert float(lufs) > -30
    assert int(growth_mib) < 64
