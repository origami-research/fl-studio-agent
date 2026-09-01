from __future__ import annotations

import wave

import numpy as np
import pytest

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
