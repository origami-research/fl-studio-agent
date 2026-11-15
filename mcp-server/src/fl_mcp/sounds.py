"""The sound library: FL's stock sounds and the CC0 packs, searched by how they sound.

Two sources, one listing and one path form (a sound's ``path``, "/"-separated, is
what the loaders and ``fl.read_wav`` take):

- FL's stock content, plain files under its install's ``Data/Patches`` folder:
  ``Plugin presets/<Generators|Effects>/<plugin>/.../<preset>.fst`` and
  ``Packs/.../<sample>.wav`` (also WavPack, FLAC, OGG, MP3, AIFF). Reading that
  folder is the listing, so it is exactly what this FL install has.
- The CC0 sample packs fl-mcp-install downloads (fl/sound_packs.py), under the
  engine's data folder: ``CC0/<pack>/<category>/<sample>.wav``.

A search ranks by sound: the sound index (sound_index.py) holds each sample's and
each instrument preset's CLAP embedding, and a query is embedded with the same model
(sound_model.py), from words ("punchy dry house kick") or from a reference sound
("sounds like this file"). A sound's score is its cosine similarity to the query,
plus up to NAME_WEIGHT when its path holds the query's words. A library file the index does not hold (an effect preset,
a file newer than the index) can only match by name: every word of the query in its
path; those come first, then the rest by sound.
"""

from __future__ import annotations

import difflib
import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import numpy as np

from fl_mcp import sound_index, sound_model
from fl_mcp.fl.paths import fl_patches_dir, sound_library_dir, sound_search_dir
from fl_mcp.schemas import LoadKind, Sound

PRESETS_DIR = "Plugin presets"
SAMPLES_DIR = "Packs"
PACKS_DIR = "CC0"
# Plugin presets/<group>/<plugin>/...: what loading a preset of each group does to a channel.
PRESET_GROUPS: dict[str, Literal["instrument", "effect"]] = {"Generators": "instrument", "Effects": "effect"}
PRESET_SUFFIXES = frozenset({".fst"})
SAMPLE_SUFFIXES = frozenset({".wav", ".wv", ".flac", ".ogg", ".mp3", ".aif", ".aiff"})
SEARCH_LIMIT_MAX = 200
# The first folder of a sample's path, and which root it is under.
SAMPLE_TOPS = (SAMPLES_DIR, PACKS_DIR)
# A sound whose path holds the query's words gets up to this much on top of its similarity by
# sound: about the spread between a close and a middling candidate. CLAP alone does not know
# "phonk" or "808", and the folders do: on FL 26.1.6.5639, "phonk cowbell" ranked one cowbell in
# its top 5 by sound alone and four with this (2026-11-13).
NAME_WEIGHT = 0.1
_WORD = re.compile(r"[a-z0-9#]+")


@dataclass(frozen=True)
class Library:
    """Where the library's files are on this machine: FL's Data/Patches, the engine's
    sound folder (absent until fl-mcp-install downloads the packs), and the sound
    model and index (likewise)."""

    stock: Path
    packs: Path
    search: Path

    def root(self, top: str) -> Path:
        return self.packs if top == PACKS_DIR else self.stock


def library() -> Library:
    return Library(stock=fl_patches_dir(), packs=sound_library_dir(), search=sound_search_dir())


def search_mode(lib: Library) -> str:
    """One line for fl-mcp-doctor: whether fl.sounds ranks by sound on this machine, or
    matches by name only and why (no index or model installed: the download is not
    published, or failed; or onnxruntime has no wheel for this machine)."""
    try:
        index = sound_index.load(lib.search / sound_index.INDEX_FILE)
    except (OSError, ValueError) as exc:
        return f"by name only: the sound index cannot be read ({exc})"
    if index is None:
        return f"by name only: the sound index is not installed in {lib.search}"
    if (problem := sound_model.encoder(lib.search).unavailable()) is not None:
        return f"by name only: {problem}"
    return f"by sound: {len(index.paths)} sounds indexed for FL {index.meta.get('fl_build', '(unknown build)')}"


def is_library_path(path: str) -> bool:
    """Whether ``path`` names a library sound (as fl.sounds lists it), not a file the model wrote."""
    parts = PurePosixPath(path.replace("\\", "/")).parts
    return bool(parts) and parts[0] in (PRESETS_DIR, *SAMPLE_TOPS)


@functools.lru_cache(maxsize=4)
def listing(lib: Library) -> tuple[Sound, ...]:
    """Every stock preset and sample, and pack sample, read once per process, in folder order."""
    files: list[Sound] = []
    for kind, root, folder, suffixes in (
        ("preset", lib.stock, PRESETS_DIR, PRESET_SUFFIXES),
        ("sample", lib.stock, SAMPLES_DIR, SAMPLE_SUFFIXES),
        ("sample", lib.packs, PACKS_DIR, SAMPLE_SUFFIXES),
    ):
        for directory, subdirs, names in os.walk(root / folder):
            subdirs.sort()
            for file in sorted(names):
                path = Path(directory, file)
                if path.suffix.lower() not in suffixes:
                    continue
                relative = PurePosixPath(path.relative_to(root).as_posix())
                # Plugin presets/<Generators|Effects>/<plugin>/...: the plugin is the third part.
                plugin = relative.parts[2] if kind == "preset" and len(relative.parts) > 3 else None
                files.append(Sound(kind=kind, name=path.stem, plugin=plugin, path=str(relative)))
    return tuple(files)


def search(
    lib: Library,
    query: str = "",
    *,
    like: np.ndarray | None = None,
    kind: LoadKind | None = None,
    plugin: str | None = None,
    limit: int = 20,
    exclude: str | None = None,
) -> list[Sound]:
    """The library's sounds that best match ``query`` (words describing a sound) and/or
    ``like`` (a reference sound's embedding, reference_embedding), best first.

    ``kind`` keeps only presets or samples; ``plugin`` only that plugin's presets
    (exact name, any case); ``exclude`` leaves out one path (the reference itself).
    Without the sound index or model (not installed, or a platform onnxruntime does
    not run on) a query matches by name only and every score is None; ``like``
    needs them and raises ValueError.
    """
    words = query.casefold().split()
    wanted_plugin = plugin.casefold() if plugin is not None else None
    pool = [
        s for s in listing(lib)
        if (kind is None or s.kind == kind)
        and (wanted_plugin is None or (s.plugin or "").casefold() == wanted_plugin)
        and s.path != exclude
    ]
    limit = min(limit, SEARCH_LIMIT_MAX)
    index = sound_index.load(lib.search / sound_index.INDEX_FILE)
    target = None if like is None else np.asarray(like, dtype=np.float32)
    if words and index is not None:
        try:
            text = sound_model.encoder(lib.search).text(query)
        except sound_model.ModelMissing:
            text = None
        if text is not None:
            target = text if target is None else target + text
    if like is not None and index is None:
        raise ValueError("the sound index is not installed here, so nothing can be matched by sound; run fl-mcp-install")
    named = [s for s in pool if words and all(w in s.path.casefold() for w in words)]
    if target is None:
        return [_described(s, index) for s in named[:limit]] if words else [_described(s, index) for s in pool[:limit]]
    assert index is not None
    indexed = [s for s in pool if s.path in index.row]
    out = [_described(s, index) for s in named if s.path not in index.row][:limit]
    if indexed and len(out) < limit:
        rows = np.fromiter((index.row[s.path] for s in indexed), dtype=np.int64, count=len(indexed))
        scores = index.embeddings[rows] @ target
        if words:
            scores = scores + NAME_WEIGHT * np.fromiter((_named(words, s.path) for s in indexed), dtype=np.float32)
        for i in np.argsort(-scores, kind="stable")[: limit - len(out)]:
            out.append(_described(indexed[i], index, round(float(scores[i]), 3)))
    return out


def _named(words: list[str], path: str) -> float:
    """The share of ``words`` that begin a word of ``path`` ("kick" names "Kicks/808 Kick.wav",
    "pad" does not name "Drumpad")."""
    line = _words_of(path)
    return sum(f" {w}" in line for w in words) / len(words)


@functools.lru_cache(maxsize=1 << 15)
def _words_of(path: str) -> str:
    """A path's words, each after a space: " packs drums kicks 808 kick wav". Cached: a search
    reads every indexed path (10,546), and splitting them anew took 60 to 130 ms a query."""
    return "".join(f" {t}" for t in _WORD.findall(path.casefold()))


def _described(sound: Sound, index: sound_index.SoundIndex | None, score: float | None = None) -> Sound:
    """``sound`` with its index measurements and its search score."""
    row = None if index is None else index.row.get(sound.path)
    if row is None:
        return sound if score is None else sound.model_copy(update={"score": score})
    return sound.model_copy(update={"score": score, **index.measures_of(row)})


def reference_embedding(lib: Library, path: str | None = None, audio: tuple[np.ndarray, int] | None = None) -> np.ndarray:
    """The embedding of a reference sound: a library ``path`` (its index row, or the file
    embedded now) or decoded ``audio`` (frames, sample rate). Raises ValueError when it
    cannot be embedded here."""
    index = sound_index.load(lib.search / sound_index.INDEX_FILE)
    if index is None:
        raise ValueError("the sound index is not installed here, so nothing can be matched by sound; run fl-mcp-install")
    if path is not None:
        if path in index.row:
            return index.embeddings[index.row[path]]
        kind: LoadKind = "preset" if path.replace("\\", "/").startswith(PRESETS_DIR + "/") else "sample"
        if kind == "preset":
            raise ValueError(f"{path!r} has no sound in the index; pass a sample or an audio file (an fl.audition render)")
        from fl_mcp.audio import read_wav

        audio = read_wav(resolve(lib, path, "sample"))
    assert audio is not None
    try:
        return sound_model.encoder(lib.search).audio(*audio)
    except sound_model.ModelMissing as exc:
        raise ValueError(f"{exc}; run fl-mcp-install") from None


def did_you_mean(lib: Library, wanted: str, kind: LoadKind) -> str:
    """" Did you mean ...?" naming the closest library paths of ``kind``, or "".

    A bare name ("909 Kick") is compared with names, a path with paths, so a guessed
    name and a mistyped path both find the sound that exists."""
    by_name = "/" not in wanted.replace("\\", "/")
    keys = {(s.name if by_name else s.path).casefold(): s.path for s in listing(lib) if s.kind == kind}
    close = difflib.get_close_matches(wanted.casefold(), list(keys), n=3, cutoff=0.6)
    return " Did you mean " + " or ".join(repr(keys[c]) for c in close) + "?" if close else ""


def resolve(lib: Library, sound: str, kind: LoadKind) -> Path:
    """The file for a library sound's ``path`` (as ``search`` lists it), checked.

    Raises ValueError when the path leaves its folder, has the wrong kind of file,
    or does not exist (naming the closest sounds that do).
    """
    relative = PurePosixPath(sound.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{sound!r} is not a library sound path; use a path from fl.sounds()")
    tops, suffixes = ((PRESETS_DIR,), PRESET_SUFFIXES) if kind == "preset" else (SAMPLE_TOPS, SAMPLE_SUFFIXES)
    if not relative.parts or relative.parts[0] not in tops or relative.suffix.lower() not in suffixes:
        mine = " or a file you wrote" if kind == "sample" else ""
        raise ValueError(f"{sound!r} is not a {kind} in the library{mine}; use one from fl.sounds().{did_you_mean(lib, sound, kind)}")
    root = lib.root(relative.parts[0])
    path = root.joinpath(*relative.parts)
    # A part like "D:x.wav" makes a Windows path leave the folder; only a path inside it is a library sound.
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"{sound!r} is not a library sound path; use a path from fl.sounds()")
    if not path.is_file():
        where = "the CC0 packs have" if relative.parts[0] == PACKS_DIR else "FL has"
        raise ValueError(f"{where} no {kind} at {sound!r}.{did_you_mean(lib, sound, kind) or ' Search fl.sounds() for one.'}")
    return path


def preset_role(sound: str) -> tuple[Literal["instrument", "effect"], str]:
    """What a stock preset path is for, and its plugin: ``Plugin presets/Generators/<plugin>/...``
    is an instrument, ``Plugin presets/Effects/<plugin>/...`` an effect.

    Raises ValueError for any other group (VST presets: which plugin and role they
    load as was not checked live).
    """
    parts = PurePosixPath(sound.replace("\\", "/")).parts
    if len(parts) < 4 or parts[0] != PRESETS_DIR or parts[1] not in PRESET_GROUPS:
        raise ValueError(f"{sound!r} is not an instrument or effect preset FL ships; VST presets cannot be loaded")
    return PRESET_GROUPS[parts[1]], parts[2]
