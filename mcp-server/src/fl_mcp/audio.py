"""Reading WAVs: the samples for the SDK's read_wav (renders, and FL's stock samples),
and the loudness the render check uses to refuse a silent recording.

integrated_lufs streams the file in fixed-size blocks, so its memory is O(block) however
long the render: a 90 s song measured whole took >3 GB and paged FL out on an 8 GB
worker. A render shorter than one block measures exactly as a whole-file pass.

There is no analyzer here on purpose: the model reads the samples in run and measures
or plots what it wants itself (docs/38).
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np

# Frames decoded per read.
_BLOCK = 1 << 15
_LOUDNESS_RATE = 48000


def integrated_lufs(path: Path) -> float:
    """BS.1770 integrated loudness of a PCM WAV, streamed block by block."""
    with path.open("rb") as file, wave.open(file, "rb") as wav:
        channels, width, sample_rate, count = _header(wav)
        loudness = _Loudness(sample_rate, channels, count)
        decoded = 0
        for block in _blocks(wav, channels, width):
            decoded += len(block)
            loudness.add(block)
    # The header is written last from the writer's own counter, so it can claim frames a failed
    # write never put on disk; the decoded count is the truth, and a short file is not a take.
    if decoded != count:
        raise ValueError(f"WAV header claims {count} frames but only {decoded} are present")
    # Tenths of a dB: finer is below what anyone hears.
    return round(loudness.integrated(), 1)


def wav_format(path: Path) -> tuple[int, int]:
    """(frames, sample rate) from the WAV header, without decoding any audio."""
    with path.open("rb") as file, wave.open(file, "rb") as wav:
        _, _, sample_rate, count = _header(wav)
    return count, sample_rate


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """The whole file as float frames (channels last) and its sample rate: the SDK's read_wav.

    PCM WAV, as renders are; and FL's stock samples, which are Ogg Vorbis inside a WAV
    (format tag 0x674F and its siblings, the data chunk an Ogg stream: box run
    2026-11-03, where the stdlib, scipy and libsndfile all refused them as WAVs)."""
    try:
        with path.open("rb") as file, wave.open(file, "rb") as wav:
            channels, width, sample_rate, _ = _header(wav)
            return _decode(wav.readframes(wav.getnframes()), channels, width), sample_rate
    except wave.Error:
        stream = _ogg_in_wav(path.read_bytes())
        if stream is None:
            raise
    import soundfile

    samples, sample_rate = soundfile.read(io.BytesIO(stream), dtype="float64", always_2d=True)
    return samples, sample_rate


def decode_audio(blob: bytes) -> tuple[np.ndarray, int]:
    """An audio file's bytes as float frames (channels last) and its sample rate.

    Whatever libsndfile reads (WAV of any sample format, FLAC, Ogg, AIFF, MP3), and FL's
    Ogg-in-WAV stock samples. Raises ValueError for anything else or for no frames."""
    import soundfile

    stream = _ogg_in_wav(blob) or blob
    try:
        samples, sample_rate = soundfile.read(io.BytesIO(stream), dtype="float32", always_2d=True)
    except (RuntimeError, TypeError):  # libsndfile's errors are RuntimeErrors
        raise ValueError("not an audio file (WAV, FLAC, Ogg, AIFF or MP3)") from None
    if not len(samples):
        raise ValueError("the audio file has no frames")
    return samples, sample_rate


def _ogg_in_wav(blob: bytes) -> bytes | None:
    """The data chunk of a RIFF WAV when it holds an Ogg stream, else None."""
    if blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        return None
    offset = 12
    while offset + 8 <= len(blob):
        chunk, size = blob[offset : offset + 4], int.from_bytes(blob[offset + 4 : offset + 8], "little")
        if chunk == b"data":
            data = blob[offset + 8 : offset + 8 + size]
            return data if data[:4] == b"OggS" else None
        offset += 8 + size + (size & 1)
    return None


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
