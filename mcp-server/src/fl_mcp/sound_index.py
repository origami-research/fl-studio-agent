"""The sound index: for every library sound, what it sounds like, so fl.sounds can rank
by sound and not only by name.

Per sound (a library path, as fl.sounds lists it): its CLAP embedding (sound_model.py)
and a few plain measurements. A sample is measured as it is; an instrument preset by
its preview, fl.audition's default phrase played through the preset on the Audition
channel (scripts/build_sound_index.py renders one per preset on a machine with FL).

The index holds no audio, only these numbers and the paths; it is built once per FL
build and downloaded by fl-mcp-install (fl/sound_search.py). One ``.npz`` file:

- ``paths``: the library paths; ``embeddings``: float16, one unit row per path;
- one float32 array per measurement in MEASURES, NaN where it does not apply;
- ``meta``: JSON: the index version, the FL build, the model and how previews were made.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

INDEX_FILE = "sound-index.npz"
VERSION = 1
# Each measurement and what it means; Sound carries them under these names.
MEASURES = {
    "seconds": "length in seconds",
    "pitch": "the steady pitch of a sample, as a MIDI note (60 = C4); None when it has none or for a preset",
    "decay": "seconds from the loudest moment until 30 dB quieter (or the end)",
    "brightness": "spectral centroid in Hz",
    "low": "share of the energy below 150 Hz (0..1)",
    "high": "share of the energy above 5 kHz (0..1)",
}
_FRAME = 2048
_HOP = 512
# The first 10 s say what a sound is, as for the embedding.
_MEASURE_SECONDS = 10
# The mean voicing probability below which a sound has no pitch (see _pitch).
_VOICING = 0.15


def measure(samples: np.ndarray, sample_rate: int, *, pitch: bool = True) -> dict[str, float | None]:
    """The plain measurements of one sound (MEASURES). ``pitch=False`` for a preset's
    preview, which plays several notes."""
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim == 2:
        x = x.mean(axis=1)
    seconds = len(x) / sample_rate
    x = x[: _MEASURE_SECONDS * sample_rate]
    if len(x) < _FRAME:
        x = np.pad(x, (0, _FRAME - len(x)))
    frames = np.lib.stride_tricks.sliding_window_view(x, _FRAME)[::_HOP]
    power = np.abs(np.fft.rfft(frames * np.hanning(_FRAME), axis=1)) ** 2
    spectrum = power.sum(axis=0)
    freqs = np.fft.rfftfreq(_FRAME, 1 / sample_rate)
    total = float(spectrum.sum())
    out: dict[str, float | None] = {"seconds": round(seconds, 3), "pitch": None}
    if total <= 0:
        return out | {"decay": 0.0, "brightness": None, "low": None, "high": None}
    out["brightness"] = round(float((freqs * spectrum).sum() / total), 1)
    out["low"] = round(float(spectrum[freqs < 150].sum() / total), 3)
    out["high"] = round(float(spectrum[freqs > 5000].sum() / total), 3)
    out["decay"] = round(_decay(x, sample_rate), 3)
    if pitch:
        out["pitch"] = _pitch(x, sample_rate)
    return out


def _decay(x: np.ndarray, sample_rate: int) -> float:
    hop = max(1, sample_rate // 100)  # 10 ms
    n = len(x) // hop
    if n == 0:
        return 0.0
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
    level = 20 * np.log10(np.maximum(rms, 1e-9))
    peak = int(level.argmax())
    quiet = np.nonzero(level[peak:] < level[peak] - 30)[0]
    end = peak + int(quiet[0]) if len(quiet) else n
    return (end - peak) * hop / sample_rate


def _pitch(x: np.ndarray, sample_rate: int) -> float | None:
    """The median pitch of the sound's voiced frames when most of its sounding frames are
    voiced and YIN is confident on them (probabilistic YIN), as a MIDI note to a tenth;
    None for an unpitched sound. Noise reads as "voiced" on half its frames, but with a
    mean voicing probability near 0.01; a 40 Hz sub-bass decaying over 0.5 s reads 0.29."""
    import librosa

    y = librosa.resample(x[: 3 * sample_rate], orig_sr=sample_rate, target_sr=22050)
    f0, voiced, probability = librosa.pyin(y, fmin=30, fmax=2000, sr=22050, frame_length=2048)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0][: len(voiced)]
    sounding = rms > rms.max() * 0.1 if len(rms) and rms.max() > 0 else np.zeros(len(voiced), bool)
    if sounding.sum() < 3 or voiced[sounding].mean() < 0.5 or np.nanmean(probability[sounding]) < _VOICING:
        return None
    return round(float(librosa.hz_to_midi(np.nanmedian(f0[sounding & voiced]))), 1)


@dataclass(frozen=True)
class SoundIndex:
    paths: tuple[str, ...]
    embeddings: np.ndarray  # (n, 512) float32, unit rows
    measures: dict[str, np.ndarray]
    meta: dict[str, Any]

    @functools.cached_property
    def row(self) -> dict[str, int]:
        return {path: i for i, path in enumerate(self.paths)}

    def measures_of(self, i: int) -> dict[str, float | None]:
        # Stored as float32; reported as measured (to 4 decimals), not as 0.30000001.
        return {name: (None if np.isnan(v := values[i]) else round(float(v), 4)) for name, values in self.measures.items()}


def write(path: Path, rows: list[tuple[str, np.ndarray, dict[str, float | None]]], meta: dict[str, Any]) -> None:
    """Write an index of ``rows`` (path, embedding, measurements), sorted by path."""
    rows = sorted(rows, key=lambda row: row[0])
    arrays: dict[str, np.ndarray] = {
        "paths": np.asarray([row[0] for row in rows], dtype=str),
        "embeddings": np.stack([row[1] for row in rows]).astype(np.float16),
        "meta": np.asarray(json.dumps({"version": VERSION, **meta}, sort_keys=True)),
    }
    for name in MEASURES:
        values = [row[2].get(name) for row in rows]
        arrays[name] = np.asarray([np.nan if v is None else v for v in values], dtype=np.float32)
    partial = path.with_name(path.name + ".partial")
    with partial.open("wb") as file:
        np.savez_compressed(file, **arrays)
    partial.replace(path)


def load(path: Path) -> SoundIndex | None:
    """The index at ``path``, or None when there is none; read again when the file changes."""
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    return _load(path, stamp)


@functools.lru_cache(maxsize=2)
def _load(path: Path, _stamp: int) -> SoundIndex:
    with np.load(path, allow_pickle=False) as data:
        meta = json.loads(str(data["meta"]))
        if meta.get("version") != VERSION:
            raise ValueError(f"{path} is a version {meta.get('version')} sound index; this engine reads version {VERSION}")
        embeddings = data["embeddings"].astype(np.float32)
        embeddings /= np.maximum(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-9)
        return SoundIndex(
            paths=tuple(str(p) for p in data["paths"]),
            embeddings=embeddings,
            measures={name: data[name] for name in MEASURES},
            meta=meta,
        )
