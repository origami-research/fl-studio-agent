"""Small, deterministic measurements for PCM WAV renders.

analyze_wav streams the file in fixed-size blocks, so its memory is O(block) however
long the render: a 90 s song analysed whole (4x-upsampled true peak, float64 copies)
took >3 GB and paged FL out on an 8 GB worker. Every measurement is an accumulator fed
block by block; a render shorter than one block measures exactly as a whole-file pass.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
from pydantic import BaseModel

# Frames decoded per read; also the true-peak block and the spectrum segment length.
_BLOCK = 1 << 15
_SPECTRUM_SEGMENT = 1 << 16
# Real signal each true-peak block is upsampled with on both sides, so the sinc tails
# that cross a block seam are present and the seam loses no inter-sample peak.
_TRUE_PEAK_CONTEXT = 1 << 13
_LOUDNESS_RATE = 48000


class SpectralBalance(BaseModel):
    """Mean-square level by conventional production bands, in dBFS."""

    low_dbfs: float
    mid_dbfs: float
    high_dbfs: float


class AudioAnalysis(BaseModel):
    bpm: float | None
    key: str | None
    integrated_lufs: float
    true_peak_dbfs: float
    spectral_balance: SpectralBalance


def analyze_wav(path: Path) -> AudioAnalysis:
    """Measure a PCM WAV without invoking a DAW or an external binary."""
    with path.open("rb") as file, wave.open(file, "rb") as wav:
        channels, width, sample_rate, count = _header(wav)
        loudness = _Loudness(sample_rate, channels, count)
        peak = _TruePeak(channels)
        envelope = _Envelope(sample_rate)
        spectrum = _Spectrum(sample_rate, count)
        decoded = 0
        for block in _blocks(wav, channels, width):
            decoded += len(block)
            loudness.add(block)
            peak.add(block)
            mono = block.mean(axis=1)
            envelope.add(mono)
            spectrum.add(mono)
    # The header is written last from the writer's own counter, so it can claim frames a failed
    # write never put on disk; the decoded count is the truth, and a short file is not a take.
    if decoded != count:
        raise ValueError(f"WAV header claims {count} frames but only {decoded} are present")
    return AudioAnalysis(
        bpm=envelope.bpm(),
        key=spectrum.key(),
        integrated_lufs=loudness.integrated(),
        true_peak_dbfs=_dbfs(peak.finish()),
        spectral_balance=spectrum.balance(),
    )


def wav_format(path: Path) -> tuple[int, int]:
    """(frames, sample rate) from the WAV header, without decoding any audio."""
    with path.open("rb") as file, wave.open(file, "rb") as wav:
        _, _, sample_rate, count = _header(wav)
    return count, sample_rate


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    """The whole file as float frames (channels last): the SDK's read_wav, not the analyzer."""
    with path.open("rb") as file, wave.open(file, "rb") as wav:
        channels, width, sample_rate, _ = _header(wav)
        return _decode(wav.readframes(wav.getnframes()), channels, width), sample_rate


def _header(wav: wave.Wave_read) -> tuple[int, int, int, int]:
    channels, width, sample_rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
    if channels <= 0:
        raise ValueError(f"WAV has invalid channel count: {channels}")
    if sample_rate <= 0:
        raise ValueError(f"WAV has invalid sample rate: {sample_rate}")
    if width not in {1, 2, 3, 4}:
        raise ValueError(f"unsupported WAV sample width: {width} bytes")
    if wav.getnframes() <= 0:
        raise ValueError("WAV has no complete audio frames")
    return channels, width, sample_rate, wav.getnframes()


def _blocks(wav: wave.Wave_read, channels: int, width: int):
    while raw := wav.readframes(_BLOCK):
        yield _decode(raw, channels, width)


def _decode(raw: bytes, channels: int, width: int) -> np.ndarray:
    if len(raw) % (width * channels):
        raise ValueError("WAV has incomplete audio frames")
    if width == 1:
        values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) / 128
    elif width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768
    elif width == 3:
        data = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        values = (data[:, 0].astype(np.int32) | (data[:, 1].astype(np.int32) << 8) | (data[:, 2].astype(np.int32) << 16))
        values = ((values ^ (1 << 23)) - (1 << 23)).astype(np.float64) / (1 << 23)
    else:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float64) / (1 << 31)
    if values.size == 0:
        raise ValueError("WAV has no complete audio frames")
    return values.reshape(-1, channels)


def _dbfs(value: float) -> float:
    return float(20 * np.log10(max(value, 1e-12)))


class _TruePeak:
    """4x band-limited interpolation, block by block.

    Each block is upsampled with _TRUE_PEAK_CONTEXT real frames on both sides; the zero
    padding after each window keeps its wrap-around away from the kept block, and the
    render's own ends see silence beyond them, as an isolated recording should.
    """

    def __init__(self, channels: int) -> None:
        self._buffer = np.empty((0, channels))
        self._offset = 0  # absolute frame index of _buffer[0]
        self._next = 0  # first frame of the next block to measure
        self._peak = 0.0

    def add(self, frames: np.ndarray) -> None:
        self._buffer = np.concatenate([self._buffer, frames])
        self._measure(final=False)

    def finish(self) -> float:
        self._measure(final=True)
        return self._peak

    def _measure(self, *, final: bool) -> None:
        end = self._offset + len(self._buffer)
        while self._next < end and (final or self._next + _BLOCK + _TRUE_PEAK_CONTEXT <= end):
            stop = min(end, self._next + _BLOCK)
            low, high = max(0, self._next - _TRUE_PEAK_CONTEXT), min(end, stop + _TRUE_PEAK_CONTEXT)
            window = np.pad(self._buffer[low - self._offset : high - self._offset], ((0, _TRUE_PEAK_CONTEXT), (0, 0)))
            upsampled = np.fft.irfft(np.fft.rfft(window, axis=0), n=window.shape[0] * 4, axis=0) * 4
            kept = upsampled[(self._next - low) * 4 : (stop - low) * 4]
            self._peak = max(self._peak, float(np.abs(kept).max()))
            self._next = stop
        keep_from = max(self._offset, self._next - _TRUE_PEAK_CONTEXT)
        self._buffer = self._buffer[keep_from - self._offset :]
        self._offset = keep_from


def _biquad(
    samples: np.ndarray, b: tuple[float, float, float], a: tuple[float, float], state: list[float]
) -> np.ndarray:
    """Direct form I; `state` ([x1, x2, y1, y2]) carries the filter across blocks."""
    out = np.empty_like(samples)
    x1, x2, y1, y2 = state
    b0, b1, b2 = b
    a1, a2 = a
    for index, x0 in enumerate(samples):
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[index] = y0
        x2, x1, y2, y1 = x1, x0, y1, y0
    state[:] = [x1, x2, y1, y2]
    return out


# ITU-R BS.1770 K-weighting at the standard 48 kHz render rate. Other rates are linearly
# resampled to 48 kHz first: the filter stays calibrated where it was specified.
_SHELF = (1.53512485958697, -2.69169618940638, 1.19839281085285), (-1.69065929318241, 0.73248077421585)
_HIGHPASS = (1.0, -2.0, 1.0), (-1.99004745483398, 0.99007225036621)


class _Loudness:
    """BS.1770 integrated loudness: 400 ms gating blocks every 100 ms, absolute and relative gates."""

    def __init__(self, sample_rate: int, channels: int, count: int) -> None:
        self._rate = sample_rate
        self._count = count
        # Target sample j sits at source frame j * step; `count` frames become `length`.
        self._length = max(1, round(count * _LOUDNESS_RATE / sample_rate))
        self._step = (count - 1) / (self._length - 1) if self._length > 1 else 0.0
        self._emitted = 0
        self._carry: np.ndarray | None = None  # last source frame, for interpolating across a seam
        self._consumed = 0  # source frames seen
        self._states = [([0.0] * 4, [0.0] * 4) for _ in range(channels)]
        self._hop = round(0.1 * _LOUDNESS_RATE)
        self._hops: list[float] = []  # channel-summed energy of each complete 100 ms hop
        self._partial = np.empty(0)
        self._total = 0.0
        self._weighted = 0

    def add(self, frames: np.ndarray) -> None:
        start = self._consumed
        self._consumed += len(frames)
        if self._rate != _LOUDNESS_RATE:
            frames, start = self._resample(frames, start)
        if not len(frames):
            return
        weighted = np.stack(
            [
                _biquad(_biquad(frames[:, c], _SHELF[0], _SHELF[1], shelf), _HIGHPASS[0], _HIGHPASS[1], highpass)
                for c, (shelf, highpass) in enumerate(self._states)
            ],
            axis=1,
        )
        energy = np.sum(weighted**2, axis=1)
        self._total += float(energy.sum())
        self._weighted += len(energy)
        energy = np.concatenate([self._partial, energy])
        whole = len(energy) // self._hop * self._hop
        self._hops.extend(energy[:whole].reshape(-1, self._hop).sum(axis=1).tolist())
        self._partial = energy[whole:]

    def _resample(self, frames: np.ndarray, start: int) -> tuple[np.ndarray, int]:
        if self._carry is not None:
            frames, start = np.concatenate([self._carry, frames]), start - 1
        self._carry = frames[-1:]
        last = start + len(frames) - 1
        if self._consumed == self._count:  # the last block owns every remaining target
            stop = self._length
        else:
            stop = min(self._length, int(np.floor(last / self._step)) + 1)
        targets = np.arange(self._emitted, stop) * self._step
        self._emitted += len(targets)
        source = np.arange(start, start + len(frames), dtype=np.float64)
        return np.stack([np.interp(targets, source, frames[:, c]) for c in range(frames.shape[1])], axis=1), start

    def integrated(self) -> float:
        hops = np.array(self._hops)
        if self._weighted < 4 * self._hop:
            energies = np.array([self._total / self._weighted])
        else:
            blocks = (self._weighted - 4 * self._hop) // self._hop + 1
            energies = (hops[:blocks] + hops[1 : blocks + 1] + hops[2 : blocks + 2] + hops[3 : blocks + 3]) / (4 * self._hop)
        levels = -0.691 + 10 * np.log10(np.maximum(energies, 1e-12))
        gated = energies[levels > -70]
        if not len(gated):
            return -120.0
        relative = -0.691 + 10 * np.log10(gated.mean()) - 10
        gated = gated[levels[levels > -70] > relative]
        return float(-0.691 + 10 * np.log10(max(gated.mean(), 1e-12)))


class _Envelope:
    """RMS envelope at 200 Hz; the tempo is the strongest onset periodicity in 60-240 BPM."""

    def __init__(self, sample_rate: int) -> None:
        self._hop = max(1, sample_rate // 200)
        self._rms: list[float] = []
        self._partial = np.empty(0)

    def add(self, mono: np.ndarray) -> None:
        mono = np.concatenate([self._partial, mono])
        whole = len(mono) // self._hop * self._hop
        self._rms.extend(np.sqrt(np.mean(mono[:whole].reshape(-1, self._hop) ** 2, axis=1)).tolist())
        self._partial = mono[whole:]

    def bpm(self) -> float | None:
        if not self._rms:
            return None
        envelope = np.array(self._rms)
        onset = np.maximum(0, np.diff(envelope, prepend=envelope[0]))
        if onset.max() < envelope.mean() * 0.1:
            return None
        # FFT autocorrelation keeps a multi-minute render linearithmic instead of
        # making its analysis quadratic in duration.
        fft_length = len(onset) * 2 - 1
        spectrum = np.fft.rfft(onset, n=fft_length)
        autocorrelation = np.fft.irfft(spectrum * spectrum.conj(), n=fft_length)[: len(onset)]
        low, high = round(200 * 60 / 240), round(200 * 60 / 60)
        if len(autocorrelation) <= high:
            return None
        lag = low + np.argmax(autocorrelation[low : high + 1])
        return round(200 * 60 / lag, 2)


class _Spectrum:
    """Mean Hann-windowed power spectrum of the mono mix (Welch, 50% overlap).

    A render no longer than one segment is one segment: exactly the whole-file spectrum.
    Longer renders average segments at every half segment plus one ending on the last
    frame, so the whole render counts.
    """

    def __init__(self, sample_rate: int, count: int) -> None:
        self._rate = sample_rate
        self._count = count
        self._size = min(count, _SPECTRUM_SEGMENT)
        self._window = np.hanning(self._size)
        self._power = np.zeros(self._size // 2 + 1)
        self._segments = 0
        self._buffer = np.empty(0)
        self._offset = 0  # absolute frame index of _buffer[0]
        self._next = 0  # first frame of the next segment
        self._last_end = 0

    def add(self, mono: np.ndarray) -> None:
        self._buffer = np.concatenate([self._buffer, mono])
        end = self._offset + len(self._buffer)
        while self._next + self._size <= end:
            self._take(self._next)
            self._next += max(1, self._size // 2)
        if end == self._count and self._last_end < end:
            self._take(end - self._size)
        keep_from = max(self._offset, min(self._next, end - self._size))
        self._buffer = self._buffer[keep_from - self._offset :]
        self._offset = keep_from

    def _take(self, start: int) -> None:
        segment = self._buffer[start - self._offset : start - self._offset + self._size]
        self._power += np.abs(np.fft.rfft(segment * self._window)) ** 2
        self._segments += 1
        self._last_end = start + self._size

    def key(self) -> str | None:
        if not np.any(self._power):
            return None
        frequencies = np.fft.rfftfreq(self._size, 1 / self._rate)
        valid = (frequencies >= 20) & (frequencies <= 5000)
        if not np.any(valid):
            return None
        frequency = frequencies[valid][np.argmax(self._power[valid])]
        if frequency <= 0:
            return None
        midi = round(69 + 12 * np.log2(frequency / 440))
        return ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")[midi % 12]

    def balance(self) -> SpectralBalance:
        power = self._power / self._segments
        frequencies = np.fft.rfftfreq(self._size, 1 / self._rate)

        def level(low: float, high: float) -> float:
            band = power[(frequencies >= low) & (frequencies < high)]
            # Parseval for a one-sided real FFT, corrected for the Hann window's
            # mean-square attenuation, gives a directly comparable dBFS RMS level.
            mean_square = 2 * band.sum() / (self._size**2 * np.mean(self._window**2))
            return float(10 * np.log10(max(mean_square, 1e-12)))

        return SpectralBalance(low_dbfs=level(20, 250), mid_dbfs=level(250, 4000), high_dbfs=level(4000, 20000))
