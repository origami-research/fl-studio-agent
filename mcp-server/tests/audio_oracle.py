"""Oracle: the whole-file analyzer replaced by the streaming one (fl_mcp.audio before
block streaming), verbatim, so tests can prove the streaming measurements equal it."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
from pydantic import BaseModel


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
    samples, sample_rate = _read_wav(path)
    mono = samples.mean(axis=1)
    return AudioAnalysis(
        bpm=_estimate_bpm(mono, sample_rate),
        key=_estimate_key(mono, sample_rate),
        integrated_lufs=_integrated_lufs(samples, sample_rate),
        true_peak_dbfs=_dbfs(_true_peak(samples)),
        spectral_balance=_spectral_balance(mono, sample_rate),
    )


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with path.open("rb") as file, wave.open(file, "rb") as wav:
        channels, width, sample_rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    if channels <= 0:
        raise ValueError(f"WAV has invalid channel count: {channels}")
    if sample_rate <= 0:
        raise ValueError(f"WAV has invalid sample rate: {sample_rate}")
    if width not in {1, 2, 3, 4}:
        raise ValueError(f"unsupported WAV sample width: {width} bytes")
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
    elif width == 4:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float64) / (1 << 31)
    if values.size == 0 or values.size % channels:
        raise ValueError("WAV has no complete audio frames")
    return values.reshape(-1, channels), sample_rate


def _dbfs(value: float) -> float:
    return float(20 * np.log10(max(value, 1e-12)))


def _true_peak(samples: np.ndarray) -> float:
    # 4x band-limited interpolation. Padding avoids treating a render boundary as periodic.
    count = samples.shape[0]
    padded = np.pad(samples, ((0, count * 3), (0, 0)))
    spectrum = np.fft.rfft(padded, axis=0)
    upsampled = np.fft.irfft(spectrum, n=padded.shape[0] * 4, axis=0) * 4
    return float(np.abs(upsampled[: count * 4]).max())


def _biquad(samples: np.ndarray, b: tuple[float, float, float], a: tuple[float, float]) -> np.ndarray:
    out = np.empty_like(samples)
    x1 = x2 = y1 = y2 = 0.0
    b0, b1, b2 = b
    a1, a2 = a
    for index, x0 in enumerate(samples):
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[index] = y0
        x2, x1, y2, y1 = x1, x0, y1, y0
    return out


def _k_weight(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    # ITU-R BS.1770 coefficients for the standard 48 kHz render rate. Bilinear
    # transformation keeps the same analogue filters for other common rates.
    scale = 48000 / sample_rate
    shelf = (1.53512485958697, -2.69169618940638, 1.19839281085285), (-1.69065929318241, 0.73248077421585)
    highpass = (1.0, -2.0, 1.0), (-1.99004745483398, 0.99007225036621)
    if sample_rate != 48000:
        # The filter is intentionally calibrated at 48k; resampling here is
        # cheaper and less error-prone than carrying a second coefficient table.
        length = max(1, round(len(samples) * scale))
        source = np.linspace(0, len(samples) - 1, len(samples))
        target = np.linspace(0, len(samples) - 1, length)
        samples = np.interp(target, source, samples)
    return _biquad(_biquad(samples, shelf[0], shelf[1]), highpass[0], highpass[1])


def _integrated_lufs(samples: np.ndarray, sample_rate: int) -> float:
    weighted = np.stack([_k_weight(samples[:, channel], sample_rate) for channel in range(samples.shape[1])], axis=1)
    block = round(0.4 * 48000)
    hop = round(0.1 * 48000)
    if len(weighted) < block:
        energies = np.array([np.mean(np.sum(weighted**2, axis=1))])
    else:
        energies = np.array(
            [
                np.mean(np.sum(weighted[start : start + block] ** 2, axis=1))
                for start in range(0, len(weighted) - block + 1, hop)
            ]
        )
    levels = -0.691 + 10 * np.log10(np.maximum(energies, 1e-12))
    gated = energies[levels > -70]
    if not len(gated):
        return -120.0
    relative = -0.691 + 10 * np.log10(gated.mean()) - 10
    gated = gated[levels[levels > -70] > relative]
    return float(-0.691 + 10 * np.log10(max(gated.mean(), 1e-12)))


def _estimate_key(samples: np.ndarray, sample_rate: int) -> str | None:
    if not np.any(samples):
        return None
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    frequencies = np.fft.rfftfreq(len(samples), 1 / sample_rate)
    valid = (frequencies >= 20) & (frequencies <= 5000)
    if not np.any(valid):
        return None
    frequency = frequencies[valid][np.argmax(spectrum[valid])]
    if frequency <= 0:
        return None
    midi = round(69 + 12 * np.log2(frequency / 440))
    return ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")[midi % 12]


def _estimate_bpm(samples: np.ndarray, sample_rate: int) -> float | None:
    hop = max(1, sample_rate // 200)
    usable = len(samples) // hop * hop
    if usable == 0:
        return None
    envelope = np.sqrt(np.mean(samples[:usable].reshape(-1, hop) ** 2, axis=1))
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


def _spectral_balance(samples: np.ndarray, sample_rate: int) -> SpectralBalance:
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
    frequencies = np.fft.rfftfreq(len(samples), 1 / sample_rate)

    def level(low: float, high: float) -> float:
        band = spectrum[(frequencies >= low) & (frequencies < high)]
        # Parseval for a one-sided real FFT, corrected for the Hann window's
        # mean-square attenuation, gives a directly comparable dBFS RMS level.
        power = 2 * band.sum() / (len(samples) ** 2 * np.mean(window**2))
        return float(10 * np.log10(max(power, 1e-12)))

    return SpectralBalance(low_dbfs=level(20, 250), mid_dbfs=level(250, 4000), high_dbfs=level(4000, 20000))
