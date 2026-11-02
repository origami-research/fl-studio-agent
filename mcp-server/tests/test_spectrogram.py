"""fl.spectrogram: the picture the model looks at instead of listening.

Each test pins a reading the docstrings promise the model, on synthetic audio whose
answer is known: bars line up with FL's tempo, the loudness lane reads LUFS, the width
lane tells mono from wide, red marks full scale, and pink noise draws as an even band.
"""

from __future__ import annotations

import wave

import numpy as np
import pytest

from fl_mcp.schemas import RenderSpan
from fl_mcp.spectrogram import _HIGH_HZ, _LOW_HZ, _ROWS, Grid, measure, spectrogram

RATE = 48000


def _write(path, frames, rate=RATE):
    frames = frames if frames.ndim == 2 else frames[:, None]
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(frames.shape[1])
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes((np.clip(frames, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def _span(bars, tempo=120.0):
    beats = bars * 4
    return RenderSpan(source="pattern", pattern=1, beats=beats, tempo_bpm=tempo, seconds=beats * 60 / tempo)


def _seconds(seconds, rate=RATE):
    return np.arange(round(seconds * rate)) / rate


def test_units_are_flls_bars_and_the_loudness_lane_reads_lufs(tmp_path):
    # 8 bars at 120 BPM (2 s each): silent for two bars, then a 997 Hz tone at -20 dBFS in
    # both channels, which BS.1770 calls -20 LUFS; plus the render's 0.5 s decay tail.
    t = _seconds(16.5)
    tone = np.where(t >= 4.0, 0.1 * np.sin(2 * np.pi * 997 * t), 0.0)
    picture = measure(_write(tmp_path / "song.wav", np.stack([tone, tone], axis=1)), Grid.of(_span(8)))

    assert picture.grid.bars == 8
    assert len(picture.unit_lufs) == 9  # 8 bars and the tail
    assert picture.unit_lufs[0] < -90
    assert picture.unit_lufs[1] < -40  # only the analysis window's edge reaches into the tone
    assert picture.unit_lufs[2:8] == pytest.approx(-20.0, abs=0.2)


def test_the_width_lane_tells_mono_from_wide_and_red_marks_full_scale(tmp_path):
    t = _seconds(8.0)
    left = 0.3 * np.sin(2 * np.pi * 440 * t)
    right = np.where(t < 4.0, left, -left)  # identical, then opposite polarity
    right[round(6.5 * RATE)] = 1.0  # one full-scale sample in bar 4
    picture = measure(_write(tmp_path / "width.wav", np.stack([left, right], axis=1)), Grid.of(_span(4)))

    assert picture.unit_width_db[0] < -60  # mono: no side signal at all
    assert picture.unit_width_db[2] > 20  # out of phase: all side
    assert list(picture.unit_full_scale[:4]) == [False, False, False, True]


def test_pink_noise_draws_as_an_even_band(tmp_path):
    """Colour is energy per log-frequency row, so pink noise (equal energy per octave),
    the usual reference for a balanced mix, is level from bass to air."""
    rng = np.random.default_rng(7)
    count = 20 * RATE
    spectrum = np.fft.rfft(rng.standard_normal(count))
    frequencies = np.fft.rfftfreq(count, 1 / RATE)
    spectrum[1:] /= np.sqrt(frequencies[1:])
    spectrum[0] = 0
    pink = np.fft.irfft(spectrum, n=count)
    pink *= 0.1 / np.sqrt(np.mean(pink**2))
    picture = measure(_write(tmp_path / "pink.wav", pink), Grid.of(None))

    rows = picture.rows_db.mean(axis=1)
    centres = np.geomspace(_LOW_HZ, _HIGH_HZ, _ROWS + 1)
    inside = (centres[:-1] > 60) & (centres[1:] < 12000)
    assert np.ptp(rows[inside]) < 3.0


def test_a_tone_lights_the_row_of_its_frequency(tmp_path):
    t = _seconds(4.0)
    picture = measure(_write(tmp_path / "a2.wav", 0.3 * np.sin(2 * np.pi * 110 * t)), Grid.of(None))

    loudest = int(np.argmax(picture.rows_db.mean(axis=1)))
    edges = np.geomspace(_LOW_HZ, _HIGH_HZ, _ROWS + 1)
    assert edges[loudest] <= 110 <= edges[loudest + 1]


def test_a_short_loop_fills_every_column(tmp_path):
    """A 4 s loop has fewer frames than the picture has columns; none may draw as silence."""
    t = _seconds(4.0)
    picture = measure(_write(tmp_path / "loop.wav", 0.3 * np.sin(2 * np.pi * 440 * t)), Grid.of(None))

    assert picture.rows_db.max(axis=0).min() > -60


def test_without_a_render_span_the_axis_is_seconds(tmp_path):
    picture = measure(_write(tmp_path / "outside.wav", np.zeros(round(3.2 * RATE))), Grid.of(None))

    assert picture.grid.bars is None
    assert len(picture.unit_lufs) == 4  # one unit per second, the last partial


@pytest.mark.parametrize("rate", [44100, 48000])
def test_a_long_render_draws_one_small_image(tmp_path, rate):
    """Streamed in blocks: many blocks give one picture of the fixed size, a few hundred tokens."""
    t = _seconds(40.0, rate)
    song = 0.2 * np.sin(2 * np.pi * 55 * t) * (np.sin(2 * np.pi * t / 2) > 0)
    image = spectrogram(_write(tmp_path / "long.wav", np.stack([song, song], axis=1), rate), _span(20))

    assert image.size == (1000, 420)
    assert image.mode == "RGB"
    assert image.width * image.height / 750 < 700  # Claude's image token estimate
