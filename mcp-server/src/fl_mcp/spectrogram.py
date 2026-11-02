"""A picture of a render the model can look at: fl.spectrogram and the analyze tool's image.

The model cannot hear, but it can see. One image shows what a few numbers cannot: where
each part of the spectrum sounds over the song, how loud each bar is, how wide the stereo
image is, and which bars hit full scale. It is read in one look for a few hundred tokens
(a 1000x420 image is about 560 at Claude's width x height / 750).

Colour is energy per 1/13 octave, so pink noise, the usual reference for a balanced mix,
draws as an even band: a bright bottom means the low end outweighs the rest, a dark top a
dull mix. The file is streamed in blocks like analyze_wav, so a long song costs only the
picture's own memory.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from fl_mcp.audio import _HIGHPASS, _LOUDNESS_RATE, _SHELF, _blocks, _header
from fl_mcp.schemas import RenderSpan

# FL's bars are 4 beats unless the song changes its time signature, which renders do not report.
BEATS_PER_BAR = 4
_FFT = 4096
_HOP = 1024
_LOW_HZ, _HIGH_HZ = 30.0, 16000.0
_ROWS = 120  # log-frequency rows, about 13 per octave
_RANGE_DB = 66.0  # the colour scale: this many dB below its top is black
_TOP_DB = -6.0  # energy per row, dBFS, drawn at full brightness
# Without a tempo there is no bar grid: loudness and width are shown per second.
_SECONDS_UNIT = 1.0
# A bar whose sample peak is this close to 0 dBFS is marked: it is clipping or at a limiter's ceiling.
_FULL_SCALE_DB = -0.5

# Layout, in pixels.
_WIDTH = 1000
_LEFT, _RIGHT, _TOP = 48, 78, 24
_SPECTRUM_H, _LOUDNESS_H, _WIDTH_H = 250, 74, 34
_GAP, _AXIS_H = 8, 22
_LOUDNESS_FLOOR = -40.0  # LUFS at the bottom of the loudness lane
_WIDTH_FLOOR = -40.0  # side-to-mid dB at the bottom of the width lane (mono)
_BANDS = ((60, "sub"), (250, "bass"), (500, "low mids"), (2000, "mids"), (6000, "presence"))
_BAND_NAMES = ("sub", "bass", "low mids", "mids", "presence", "air")
_HZ_TICKS = ((50, "50"), (100, "100"), (200, "200"), (500, "500"), (1000, "1k"), (2000, "2k"),
             (5000, "5k"), (10000, "10k"))  # fmt: skip
# Black, violet, red, orange, pale yellow: a perceptually ordered ramp that prints legibly.
_COLOURS = np.array(
    [[0, 0, 4], [40, 11, 84], [101, 21, 110], [159, 42, 99], [212, 72, 66], [245, 125, 21],
     [250, 193, 39], [252, 255, 164]],
    dtype=np.float64,
)  # fmt: skip


@dataclass(frozen=True)
class Grid:
    """The time axis: the song's bars when its render span is known, seconds otherwise."""

    unit_seconds: float
    bars: int | None  # the song's bar count; units past it are the render's decay tail

    @classmethod
    def of(cls, span: RenderSpan | None) -> Grid:
        if span is None:
            return cls(_SECONDS_UNIT, bars=None)
        return cls(60.0 / span.tempo_bpm * BEATS_PER_BAR, bars=max(1, int(np.ceil(span.beats / BEATS_PER_BAR - 1e-9))))


@dataclass(frozen=True)
class Picture:
    """What the image draws; measured once, drawn by draw()."""

    rows_db: np.ndarray  # (_ROWS, columns): energy per log-frequency row, dBFS, low rows first
    unit_lufs: np.ndarray  # loudness of each bar (or second), LUFS
    unit_width_db: np.ndarray  # side energy over mid energy per unit, dB; -inf for mono
    unit_full_scale: np.ndarray  # bool per unit: a sample peak within _FULL_SCALE_DB of 0 dBFS
    seconds: float
    grid: Grid
    high_hz: float


def spectrogram(path: Path, span: RenderSpan | None = None, title: str | None = None) -> Image.Image:
    """The picture of a WAV: spectrum over time, loudness and stereo width per bar.

    ``span`` is what FL said the render played (its tempo and beats), which puts bar
    numbers on the time axis; without it the axis is in seconds.
    """
    picture = measure(path, Grid.of(span))
    head = title if title is not None else path.name
    if span is not None:
        head += f"   {span.tempo_bpm:g} BPM, {picture.grid.bars} bars of {BEATS_PER_BAR} beats"
    return draw(picture, f"{head}   {_clock(picture.seconds)}")


def measure(path: Path, grid: Grid) -> Picture:
    with path.open("rb") as file, wave.open(file, "rb") as wav:
        channels, width, rate, count = _header(wav)
        columns = _WIDTH - _LEFT - _RIGHT
        units = max(1, int(np.ceil(count / rate / grid.unit_seconds - 1e-9)))
        stft = _Frames(rate, count, channels, columns, units, grid.unit_seconds)
        peaks = np.zeros(units)
        start = 0
        for block in _blocks(wav, channels, width):
            _unit_peaks(block, start, rate, grid.unit_seconds, peaks)
            stft.add(block)
            start += len(block)
    return Picture(
        rows_db=stft.rows_db(),
        unit_lufs=stft.unit_lufs(),
        unit_width_db=stft.unit_width_db(),
        unit_full_scale=20 * np.log10(np.maximum(peaks, 1e-12)) >= _FULL_SCALE_DB,
        seconds=count / rate,
        grid=grid,
        high_hz=min(_HIGH_HZ, rate / 2),
    )


def _unit_peaks(block: np.ndarray, start: int, rate: int, unit_seconds: float, peaks: np.ndarray) -> None:
    units = np.minimum(((start + np.arange(len(block))) / rate / unit_seconds).astype(int), len(peaks) - 1)
    first = np.flatnonzero(np.diff(units, prepend=-1))  # units ascend, so each run starts here
    peaks[units[first]] = np.maximum(peaks[units[first]], np.maximum.reduceat(np.abs(block).max(axis=1), first))


class _Frames:
    """Hann-windowed frames of the mid and side signals, folded into picture columns and units."""

    def __init__(self, rate: int, count: int, channels: int, columns: int, units: int, unit_seconds: float) -> None:
        self._rate, self._count, self._columns = rate, count, columns
        # BS.1770 sums the channels' mean squares: stereo L^2 + R^2 = 2 (M^2 + S^2); otherwise
        # the channels are read as one signal, exact when they are copies (a mono file).
        self._channel_weight = 2.0 if channels == 2 else float(channels)
        self._unit_seconds = unit_seconds
        self._window = np.hanning(_FFT)
        # Parseval for a one-sided spectrum of a Hann-windowed frame: bins to mean square.
        self._scale = 2.0 / (_FFT**2 * np.mean(self._window**2))
        frequencies = np.fft.rfftfreq(_FFT, 1 / rate)
        self._bands = _log_bands(frequencies, min(_HIGH_HZ, rate / 2))
        self._k = _k_weighting(frequencies)
        self._column_power = np.zeros((_ROWS, columns))
        self._column_frames = np.zeros(columns)
        self._unit = {name: np.zeros(units) for name in ("loud", "mid", "side", "frames")}
        # Frames are centred from the first sample to the last: half a frame of silence pads each end.
        self._buffer = np.zeros((_FFT // 2, 2))
        self._offset = -(_FFT // 2)  # absolute sample index of _buffer[0]
        self._next = self._offset  # first sample of the next frame
        self._consumed = 0

    def add(self, block: np.ndarray) -> None:
        mid = block.mean(axis=1)
        side = (block[:, 0] - block[:, 1]) / 2 if block.shape[1] > 1 else np.zeros(len(block))
        parts = [self._buffer, np.stack([mid, side], axis=1)]
        self._consumed += len(block)
        if self._consumed == self._count:
            parts.append(np.zeros((_FFT // 2, 2)))
        self._buffer = np.concatenate(parts)
        end = self._offset + len(self._buffer)
        starts = np.arange(self._next, end - _FFT + 1, _HOP)
        if len(starts):
            self._take(starts)
            self._next = int(starts[-1]) + _HOP
        self._buffer = self._buffer[self._next - self._offset :]
        self._offset = self._next

    def _take(self, starts: np.ndarray) -> None:
        index = (starts - self._offset)[:, None] + np.arange(_FFT)[None, :]
        mid = np.abs(np.fft.rfft(self._buffer[index, 0] * self._window, axis=1)) ** 2 * self._scale
        side = np.abs(np.fft.rfft(self._buffer[index, 1] * self._window, axis=1)) ** 2 * self._scale
        centres = (starts + _FFT // 2) / self._rate
        column = np.minimum((centres / (self._count / self._rate) * self._columns).astype(int), self._columns - 1)
        rows = mid @ self._bands.T  # (frames, rows): energy per log row
        np.add.at(self._column_power.T, column, rows)
        np.add.at(self._column_frames, column, 1)
        unit = np.minimum((centres / self._unit_seconds).astype(int), len(self._unit["frames"]) - 1)
        np.add.at(self._unit["loud"], unit, self._channel_weight * ((mid + side) @ self._k))
        np.add.at(self._unit["mid"], unit, mid[:, 1:].sum(axis=1))
        np.add.at(self._unit["side"], unit, side[:, 1:].sum(axis=1))
        np.add.at(self._unit["frames"], unit, 1)

    def rows_db(self) -> np.ndarray:
        power = self._column_power / np.maximum(self._column_frames, 1)
        # A song shorter than one frame per column (under ~20 s) leaves columns with no frame;
        # each takes its nearest frame's column instead of drawing as silence.
        have = np.flatnonzero(self._column_frames)
        if 0 < len(have) < self._columns:
            column = np.arange(self._columns)
            right = np.clip(np.searchsorted(have, column), 0, len(have) - 1)
            left = np.clip(right - 1, 0, len(have) - 1)
            nearest = np.where(np.abs(have[left] - column) <= np.abs(have[right] - column), have[left], have[right])
            power = power[:, nearest]
        return 10 * np.log10(np.maximum(power, 1e-12))

    def unit_lufs(self) -> np.ndarray:
        frames = np.maximum(self._unit["frames"], 1)
        return -0.691 + 10 * np.log10(np.maximum(self._unit["loud"] / frames, 1e-12))

    def unit_width_db(self) -> np.ndarray:
        with np.errstate(divide="ignore"):
            return 10 * np.log10(self._unit["side"] / np.maximum(self._unit["mid"], 1e-20))


def _log_bands(frequencies: np.ndarray, high_hz: float) -> np.ndarray:
    """(_ROWS, bins) weights summing each log-spaced row's energy.

    A row narrower than one FFT bin (the lowest octaves) takes its share of the
    bin it falls in, so pink noise reads level all the way down.
    """
    edges = np.geomspace(_LOW_HZ, high_hz, _ROWS + 1)
    spacing = frequencies[1] - frequencies[0]
    low_bin, high_bin = frequencies - spacing / 2, frequencies + spacing / 2
    overlap = np.minimum(edges[1:, None], high_bin[None, :]) - np.maximum(edges[:-1, None], low_bin[None, :])
    return np.clip(overlap, 0, None) / spacing


def _k_weighting(frequencies: np.ndarray) -> np.ndarray:
    """|H(f)|^2 of the BS.1770 K-weighting filters, as specified at 48 kHz."""
    z = np.exp(-2j * np.pi * np.minimum(frequencies, _LOUDNESS_RATE / 2) / _LOUDNESS_RATE)
    response = np.ones_like(z)
    for b, a in (_SHELF, _HIGHPASS):
        response *= (b[0] + b[1] * z + b[2] * z**2) / (1 + a[0] * z + a[1] * z**2)
    weight = np.abs(response) ** 2
    weight[0] = 0.0  # DC carries no loudness
    return weight


# -- drawing ---------------------------------------------------------------------------


def draw(picture: Picture, title: str) -> Image.Image:
    plot_w = _WIDTH - _LEFT - _RIGHT
    spectrum_top = _TOP
    loud_top = spectrum_top + _SPECTRUM_H + _GAP
    width_top = loud_top + _LOUDNESS_H + _GAP
    axis_top = width_top + _WIDTH_H
    image = Image.new("RGB", (_WIDTH, axis_top + _AXIS_H), (250, 250, 248))
    image.paste(_spectrum_image(picture.rows_db, plot_w), (_LEFT, spectrum_top))
    pen = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=11)
    ink, faint = (40, 40, 40), (150, 150, 150)
    pen.text((_LEFT, 5), title, fill=ink, font=font)

    def y_of(hz: float) -> float:
        span = np.log(picture.high_hz / _LOW_HZ)
        return spectrum_top + _SPECTRUM_H * (1 - np.log(hz / _LOW_HZ) / span)

    for hz, label in _HZ_TICKS:
        if hz < picture.high_hz:
            y = y_of(hz)
            pen.line([(_LEFT - 4, y), (_LEFT - 1, y)], fill=ink)
            pen.text((_LEFT - 6, y), label, fill=ink, font=font, anchor="rm")
    edges = [_LOW_HZ] + [hz for hz, _ in _BANDS if hz < picture.high_hz] + [picture.high_hz]
    for low, high, name in zip(edges, edges[1:], _BAND_NAMES, strict=False):
        if low > _LOW_HZ:
            pen.line([(_LEFT + plot_w, y_of(low)), (_LEFT + plot_w + 5, y_of(low))], fill=faint)
        pen.text((_LEFT + plot_w + 7, (y_of(low) + y_of(high)) / 2), name, fill=ink, font=font, anchor="lm")
    pen.text((4, spectrum_top - 2), "Hz", fill=faint, font=font)

    _lane(pen, font, picture, loud_top, _LOUDNESS_H, picture.unit_lufs, _LOUDNESS_FLOOR, 0.0,
          (70, 110, 180), "loudness", ((-10, "-10"), (-20, "-20"), (-30, "-30")))  # fmt: skip
    _lane(pen, font, picture, width_top, _WIDTH_H, picture.unit_width_db, _WIDTH_FLOOR, 0.0,
          (90, 150, 110), "stereo width", ())  # fmt: skip
    pen.text((_LEFT - 6, loud_top + 2), "LUFS", fill=faint, font=font, anchor="rt")
    pen.text((_LEFT - 6, width_top + _WIDTH_H - 2), "mono", fill=faint, font=font, anchor="rb")
    pen.text((_LEFT - 6, width_top + 2), "wide", fill=faint, font=font, anchor="rt")
    _time_axis(pen, font, picture, plot_w, spectrum_top, axis_top)
    return image


def _spectrum_image(rows_db: np.ndarray, plot_w: int) -> Image.Image:
    level = np.clip((rows_db - (_TOP_DB - _RANGE_DB)) / _RANGE_DB, 0, 1)[::-1]  # high rows on top
    position = level * (len(_COLOURS) - 1)
    low = np.floor(position).astype(int).clip(0, len(_COLOURS) - 2)
    fraction = (position - low)[..., None]
    rgb = _COLOURS[low] * (1 - fraction) + _COLOURS[low + 1] * fraction
    return Image.fromarray(rgb.astype(np.uint8), "RGB").resize((plot_w, _SPECTRUM_H), Image.Resampling.BILINEAR)


def _unit_x(picture: Picture, plot_w: int, unit: float) -> float:
    return _LEFT + plot_w * min(1.0, unit * picture.grid.unit_seconds / picture.seconds)


def _lane(pen, font, picture, top, height, values, floor, ceiling, colour, name, ticks) -> None:
    plot_w = _WIDTH - _LEFT - _RIGHT
    pen.rectangle([(_LEFT, top), (_LEFT + plot_w, top + height)], fill=(255, 255, 255), outline=(200, 200, 200))
    for value, label in ticks:
        y = top + height * (ceiling - value) / (ceiling - floor)
        pen.line([(_LEFT, y), (_LEFT + plot_w, y)], fill=(225, 225, 225))
        pen.text((_LEFT - 6, y), label, fill=(40, 40, 40), font=font, anchor="rm")
    for unit, value in enumerate(values):
        x0, x1 = _unit_x(picture, plot_w, unit), _unit_x(picture, plot_w, unit + 1)
        filled = np.clip((value - floor) / (ceiling - floor), 0, 1) if np.isfinite(value) else 0.0
        if filled > 0:
            pen.rectangle([(x0, top + height * (1 - filled)), (max(x0, x1 - 1), top + height)], fill=colour)
        if name == "loudness" and picture.unit_full_scale[unit]:
            pen.rectangle([(x0, top), (max(x0, x1 - 1), top + 4)], fill=(215, 40, 40))
    pen.text((_LEFT + plot_w + 7, top + height / 2), name, fill=(40, 40, 40), font=font, anchor="lm")
    if name == "loudness":
        pen.text((_LEFT + plot_w + 7, top + 4), "red: 0 dBFS", fill=(215, 40, 40), font=font, anchor="lt")


def _time_axis(pen, font, picture, plot_w, spectrum_top, axis_top) -> None:
    bars = picture.grid.bars
    units = bars if bars is not None else len(picture.unit_lufs)
    per_unit = plot_w * picture.grid.unit_seconds / picture.seconds
    # Bars are marked by phrase (every 4, 8, ...) once single bars are too narrow to number.
    steps = (1, 4, 8, 16, 32, 64, 128, 256) if bars is not None else (1, 2, 5, 10, 15, 30, 60, 120, 300)
    step = next((s for s in steps if s * per_unit >= 30), steps[-1])
    pen.text((4, axis_top + 4), "bar" if bars is not None else "time", fill=(150, 150, 150), font=font)
    for unit in range(0, units, step):
        x = _unit_x(picture, plot_w, unit)
        if unit:
            pen.line([(x, spectrum_top), (x, spectrum_top + _SPECTRUM_H)], fill=(205, 205, 205), width=1)
        pen.line([(x, axis_top), (x, axis_top + 4)], fill=(40, 40, 40))
        label = str(unit + 1) if bars is not None else _clock(unit * picture.grid.unit_seconds)
        pen.text((x + 2, axis_top + 4), label, fill=(40, 40, 40), font=font)
    if bars is not None and bars < len(picture.unit_lufs):  # the decay tail after the last bar
        x = _unit_x(picture, plot_w, bars)
        pen.line([(x, spectrum_top), (x, axis_top + 4)], fill=(120, 120, 120), width=1)
        pen.text((x + 2, axis_top + 4), "end", fill=(150, 150, 150), font=font)


def _clock(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 60}:{whole % 60:02d}"
