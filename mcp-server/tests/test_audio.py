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
