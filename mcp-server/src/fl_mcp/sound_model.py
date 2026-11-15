"""The listening model behind fl.sounds: CLAP, which maps a sound and a description of
a sound into one space, so "punchy dry house kick" lands near kicks that sound like that.

The checkpoint is LAION's ``music_audioset_epoch_15_esc_90.14.pt`` (CC0 1.0; trained on
music, AudioSet and LAION-Audio-630K, the best of LAION's released checkpoints on music
and on sound effects), exported once to two ONNX encoders with int8 weights
(scripts/export_sound_model.py): a text encoder for queries and an audio encoder for
"sounds like this file" and for building the sound index. fl-mcp-install downloads them
(fl/sound_search.py); they run here, in the server, on the CPU with onnxruntime, never
in the run kernel.

The audio encoder computes its own log-mel spectrogram; its input is prepared here as
the checkpoint was trained: mono at 48 kHz, rounded to 16 bits, the first 10 s (a
shorter sound repeated to fill 10 s, then zero-padded). The index was built through
this same code, so a query and the index always agree.
"""

from __future__ import annotations

import functools
import importlib.util
import itertools
import json
import re
import threading
from math import gcd
from pathlib import Path

import numpy as np

SAMPLE_RATE = 48000
CLIP_SAMPLES = 10 * SAMPLE_RATE
# The checkpoint, pinned: Hugging Face repository, revision and file.
MODEL_ID = "lukewys/laion_clap@b3708341862f581175dba5c356a4ebf74a9b6651 music_audioset_epoch_15_esc_90.14.pt, int8 ONNX"
# The model folder's files (fl/sound_search.py downloads them by these names).
TEXT_MODEL = "clap-text-int8.onnx"
AUDIO_MODEL = "clap-audio-int8.onnx"
TOKENIZER = "clap-tokenizer.json"
MODEL_FILES = (TEXT_MODEL, AUDIO_MODEL, TOKENIZER)
# RoBERTa's limit; a query is a few words.
MAX_TOKENS = 77


# GPT-2's pre-tokenizer split, which RoBERTa's byte-level BPE uses; [^\W\d_] is a letter.
_WORDS = re.compile(r"""'s|'t|'re|'ve|'m|'ll|'d| ?[^\W\d_]+| ?\d+| ?(?:[^\s\w]|_)+|\s+(?!\S)|\s+""")


def _byte_chars() -> dict[int, str]:
    """GPT-2's byte -> printable character table: the alphabet byte-level BPE works in."""
    keep = [*range(ord("!"), ord("~") + 1), *range(ord("\xa1"), ord("\xac") + 1), *range(ord("\xae"), 256)]
    extra = iter(range(256, 512))
    return {b: chr(b) if b in keep else chr(next(extra)) for b in range(256)}


class Tokenizer:
    """RoBERTa's byte-level BPE (the CLAP text encoder's), read from its tokenizer.json:
    "<s>", the words' tokens, "</s>"; at most MAX_TOKENS ids."""

    def __init__(self, spec: dict) -> None:
        self.vocab: dict[str, int] = spec["model"]["vocab"]
        merges = [tuple(m.split(" ")) if isinstance(m, str) else tuple(m) for m in spec["model"]["merges"]]
        self.ranks = {pair: rank for rank, pair in enumerate(merges)}
        self.chars = _byte_chars()
        self.bos, self.eos = self.vocab["<s>"], self.vocab["</s>"]

    @functools.lru_cache(maxsize=4096)
    def _bpe(self, word: str) -> tuple[str, ...]:
        parts = list(word)
        while len(parts) > 1:
            pairs = set(itertools.pairwise(parts))
            best = min(pairs, key=lambda pair: self.ranks.get(pair, len(self.ranks)))
            if best not in self.ranks:
                break
            merged, i = [], 0
            while i < len(parts):
                if i < len(parts) - 1 and (parts[i], parts[i + 1]) == best:
                    merged.append(parts[i] + parts[i + 1])
                    i += 2
                else:
                    merged.append(parts[i])
                    i += 1
            parts = merged
        return tuple(parts)

    def ids(self, text: str) -> list[int]:
        out = [self.bos]
        for word in _WORDS.findall(text):
            mapped = "".join(self.chars[b] for b in word.encode("utf-8"))
            out += [self.vocab[token] for token in self._bpe(mapped)]
        return [*out[: MAX_TOKENS - 1], self.eos]


class ModelMissing(RuntimeError):
    """The model files are not installed, or onnxruntime is not available here."""


def waveform(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """The audio encoder's input for one sound: 10 s at 48 kHz, float32 (CLIP_SAMPLES,)."""
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim == 2:
        x = x.mean(axis=1)
    if sample_rate != SAMPLE_RATE:
        from scipy.signal import resample_poly

        g = gcd(SAMPLE_RATE, sample_rate)
        x = resample_poly(x, SAMPLE_RATE // g, sample_rate // g)
    if not len(x):
        raise ValueError("the sound has no frames")
    # 16-bit, as the checkpoint's own pipeline quantizes every sound.
    x = (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16) / 32767.0
    if len(x) > CLIP_SAMPLES:
        x = x[:CLIP_SAMPLES]
    else:  # "repeatpad", as the checkpoint was trained
        x = np.tile(x, CLIP_SAMPLES // len(x))
        x = np.pad(x, (0, CLIP_SAMPLES - len(x)))
    return x.astype(np.float32)


class Encoder:
    """The two ONNX encoders and the tokenizer, loaded from ``folder`` on first use."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._lock = threading.Lock()
        self._loaded: tuple | None = None

    def unavailable(self) -> str | None:
        """Why the model cannot run here, or None when it can; loads nothing."""
        missing = [name for name in MODEL_FILES if not (self.folder / name).is_file()]
        if missing:
            return f"the sound model is not installed ({', '.join(missing)} missing in {self.folder})"
        if importlib.util.find_spec("onnxruntime") is None:
            return "the sound model cannot run on this machine (onnxruntime has no wheel for it)"
        return None

    def _load(self) -> tuple:
        with self._lock:
            if self._loaded is None:
                if (problem := self.unavailable()) is not None:
                    raise ModelMissing(problem)
                try:
                    import onnxruntime
                except ImportError as exc:
                    raise ModelMissing(f"the sound model cannot run on this machine ({exc})") from None
                options = onnxruntime.SessionOptions()
                options.log_severity_level = 3
                cpu = ["CPUExecutionProvider"]
                text = onnxruntime.InferenceSession(str(self.folder / TEXT_MODEL), options, providers=cpu)
                audio = onnxruntime.InferenceSession(str(self.folder / AUDIO_MODEL), options, providers=cpu)
                tokenizer = Tokenizer(json.loads((self.folder / TOKENIZER).read_text(encoding="utf-8")))
                self._loaded = (text, audio, tokenizer)
            return self._loaded

    def text(self, query: str) -> np.ndarray:
        """A description's embedding, unit length (512 floats)."""
        text, _, tokenizer = self._load()
        ids = np.asarray([tokenizer.ids(query)], dtype=np.int64)
        mask = np.ones_like(ids)
        return text.run(None, {"input_ids": ids, "attention_mask": mask})[0][0]

    def audio(self, samples: np.ndarray, sample_rate: int) -> np.ndarray:
        """A sound's embedding, unit length (512 floats)."""
        _, audio, _ = self._load()
        return audio.run(None, {"waveform": waveform(samples, sample_rate)[None]})[0][0]


@functools.cache
def encoder(folder: Path) -> Encoder:
    """One encoder per model folder, shared by every search in this process."""
    return Encoder(folder)
