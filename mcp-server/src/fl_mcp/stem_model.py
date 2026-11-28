"""The stem model behind fl.separate: it splits a mix into vocals, drums, bass and the rest.

The model is Deezer's Spleeter 4stems (MIT, code and weights; trained by Deezer on its
own catalogue), one U-Net per stem over the mix's magnitude spectrogram, exported once to
ONNX (scripts/export_stem_model.py). fl-mcp-install downloads it with the sound model
(fl/sound_search.py); it runs here, in the server, on the CPU with onnxruntime, never in
the run kernel.

Around the network this is Spleeter's own pipeline, so a short sound splits as Spleeter
splits it: a 4096-sample STFT every 1024 samples at 44.1 kHz with 4096 zeros in front,
the magnitude of its lowest BINS bins in 512-frame windows, and a ratio mask per stem
(each estimate squared over their sum), applied to the mix's complex STFT and inverted.
Above BINS (16.5 kHz) each stem takes its mask's average over the frequencies below
("average", one of Spleeter's two settings; its default drops them), so the masks sum
to one in every bin and the stems sum back to the mix.

A sound longer than one window (CHUNK, 11.8 s) is split window by window: windows
overlap by FADE and are crossfaded linearly, so memory stays at one window's and the
stems still sum to the mix. A run's time budget (budget.py) is checked after every
window from the second on, from the time the windows after the first took: the first
also loads the model on a process's first split.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import threading
import time
from collections.abc import Callable, Iterator
from math import gcd
from pathlib import Path

import numpy as np

from fl_mcp import budget

SAMPLE_RATE = 44100
STEMS = ("vocals", "drums", "bass", "other")
# The checkpoint, pinned: Spleeter's 4stems release asset and its sha256 (checksum.json).
MODEL_ID = (
    "deezer/spleeter v1.4.0 4stems.tar.gz "
    "sha256 3adb4a50ad4eb18c7c4d65fcf4cf2367a07d48408a5eb7d03cd20067429dfaa8, float32 ONNX"
)
# The model folder's file (fl/sound_search.py downloads it by this name).
MODEL_FILE = "spleeter-4stems.onnx"

FRAME = 4096
HOP = 1024
FRAMES = 512
# Spleeter's 4stems-16kHz setting: the network sees up to 16.5 kHz.
BINS = 1536
EPSILON = 1e-10
# One network window's worth of audio: FRAMES frames, the first FRAME samples of which
# are the zeros in front.
CHUNK = FRAMES * HOP - FRAME
FADE = CHUNK // 4
STEP = CHUNK - FADE
# Periodic Hann, as tf.signal; at a quarter-frame hop its squares sum to 1.5 everywhere.
WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(FRAME) / FRAME)).astype(np.float32)
_OVERLAP = float((WINDOW**2).sum() / HOP)
_RAMP_UP = ((np.arange(FADE) + 0.5) / FADE).astype(np.float32)[:, None]

# The network: the mix's magnitude (1, FRAMES, BINS, 2) to each stem's estimate, in
# STEMS order (4, 1, FRAMES, BINS, 2). The ONNX graph's input is "mix", its outputs STEMS.
Network = Callable[[np.ndarray], np.ndarray]


class ModelMissing(RuntimeError):
    """The model file is not installed, or onnxruntime is not available here."""


class Model:
    """The ONNX network, loaded from ``folder`` on first use."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._lock = threading.Lock()
        self._session = None

    def unavailable(self) -> str | None:
        """Why the model cannot run here, or None when it can; loads nothing."""
        if not (self.folder / MODEL_FILE).is_file():
            return f"the stem model is not installed ({MODEL_FILE} missing in {self.folder})"
        if importlib.util.find_spec("onnxruntime") is None:
            return "the stem model cannot run on this machine (onnxruntime has no wheel for it)"
        return None

    def _load(self):
        with self._lock:
            if self._session is None:
                if (problem := self.unavailable()) is not None:
                    raise ModelMissing(problem)
                try:
                    import onnxruntime
                except ImportError as exc:
                    raise ModelMissing(f"the stem model cannot run on this machine ({exc})") from None
                options = onnxruntime.SessionOptions()
                options.log_severity_level = 3
                self._session = onnxruntime.InferenceSession(
                    str(self.folder / MODEL_FILE), options, providers=["CPUExecutionProvider"]
                )
            return self._session

    def __call__(self, magnitude: np.ndarray) -> np.ndarray:
        return np.stack(self._load().run(list(STEMS), {"mix": magnitude}))


@functools.cache
def model(folder: Path) -> Model:
    """One model per folder, shared by every call in this process."""
    return Model(folder)


def _stft(x: np.ndarray) -> np.ndarray:
    """One window's STFT, as Spleeter takes it: (FRAMES, FRAME // 2 + 1, channels) complex."""
    padded = np.zeros(((FRAMES - 1) * HOP + FRAME, x.shape[1]), dtype=np.float32)
    padded[FRAME : FRAME + len(x)] = x
    frames = np.lib.stride_tricks.sliding_window_view(padded, FRAME, axis=0)[::HOP]  # (FRAMES, ch, FRAME)
    return np.fft.rfft(frames * WINDOW, axis=-1).transpose(0, 2, 1)


def _istft(spectrum: np.ndarray, length: int) -> np.ndarray:
    """The inverse of _stft for each stem: (stems, FRAMES, bins, ch) to (stems, length, ch)."""
    frames = np.fft.irfft(spectrum, n=FRAME, axis=2) * WINDOW[:, None]  # (stems, FRAMES, FRAME, ch)
    out = np.zeros((spectrum.shape[0], (FRAMES - 1) * HOP + FRAME, spectrum.shape[3]), dtype=np.float32)
    for i in range(FRAMES):
        out[:, i * HOP : i * HOP + FRAME] += frames[:, i]
    return out[:, FRAME : FRAME + length] / _OVERLAP


def _window(x: np.ndarray, net: Network) -> np.ndarray:
    """One window of stereo audio (at most CHUNK frames) split: (stems, frames, 2)."""
    spectrum = _stft(x)
    magnitude = np.abs(spectrum[None, :, :BINS]).astype(np.float32)
    power = np.asarray(net(magnitude), dtype=np.float32)[:, 0] ** 2  # (stems, FRAMES, BINS, 2)
    masks = (power + EPSILON / len(STEMS)) / (power.sum(axis=0) + EPSILON)
    above = np.broadcast_to(
        masks.mean(axis=2, keepdims=True), (*masks.shape[:2], spectrum.shape[1] - BINS, masks.shape[3])
    )
    masks = np.concatenate([masks, above], axis=2)
    return _istft(masks * spectrum[None], len(x))


def split(samples: np.ndarray, net: Network) -> Iterator[np.ndarray]:
    """Stereo 44.1 kHz ``samples`` (frames, 2) split into STEMS, in order: blocks of
    (stems, frames, 2) that join into the whole sound. Between windows, refuses (budget.py)
    when the windows left would not finish in what is left of the run."""
    n = len(samples)
    starts = [0]
    while starts[-1] + CHUNK < n:
        starts.append(starts[-1] + STEP)
    timed_from = None
    tail = None
    for k, start in enumerate(starts):
        out = _window(samples[start : start + CHUNK], net)
        if tail is not None:
            out[:, :FADE] = out[:, :FADE] * _RAMP_UP + tail
        if k == len(starts) - 1:
            yield out
            return
        tail = out[:, -FADE:] * _RAMP_UP[::-1]
        yield out[:, :-FADE]
        # The first window also loads the model on this process's first split (39 s from a
        # freshly restored disk, against about 3 s a window after it), so the rest is
        # estimated from the windows after it; the load's time is already gone from the run.
        if timed_from is None:
            timed_from = time.monotonic()
            continue
        per_window = (time.monotonic() - timed_from) / k
        budget.require(
            per_window * (len(starts) - k - 1),
            "Splitting the rest of the audio",
            "split the audio into shorter parts and separate each",
            outcome="so it was stopped and no stems were written",
        )


def stereo(samples: np.ndarray, rate: int) -> np.ndarray:
    """Mono or stereo audio at any rate as the network's input: stereo at 44.1 kHz,
    float32. Raises ValueError for more than two channels."""
    if samples.shape[1] > 2:
        raise ValueError(f"the audio has {samples.shape[1]} channels; pass mono or stereo")
    x = np.asarray(samples, dtype=np.float32)
    if rate != SAMPLE_RATE:
        from scipy.signal import resample_poly

        g = gcd(SAMPLE_RATE, rate)
        x = resample_poly(x, SAMPLE_RATE // g, rate // g, axis=0).astype(np.float32)
    return np.repeat(x, 2, axis=1) if x.shape[1] == 1 else x


def write(samples: np.ndarray, rate: int, net: Network, paths: dict[str, Path]) -> None:
    """Split ``samples`` (frames, channels) at ``rate`` and write each stem to its path
    in ``paths`` (keyed by STEMS): 16-bit WAV at 44.1 kHz with the input's channels.
    Each file appears whole or not at all, and none appears unless all do."""
    import soundfile

    channels = samples.shape[1]
    x = stereo(samples, rate)
    partial = {stem: path.with_name(path.name + ".partial") for stem, path in paths.items()}
    try:
        with contextlib.ExitStack() as stack:
            files = [
                stack.enter_context(
                    soundfile.SoundFile(partial[stem], "w", SAMPLE_RATE, channels, "PCM_16", format="WAV")
                )
                for stem in STEMS
            ]
            for block in split(x, net):
                if channels == 1:
                    block = block.mean(axis=2, keepdims=True)
                for file, stem in zip(files, np.clip(block, -1.0, 1.0), strict=True):
                    file.write(stem)
        for stem in STEMS:
            partial[stem].replace(paths[stem])
    except BaseException:
        for path in partial.values():
            path.unlink(missing_ok=True)
        raise
