"""The stock sounds FL ships: plugin presets and samples, searchable by words.

FL keeps them as plain files under its install's ``Data/Patches`` folder:
``Plugin presets/<Generators|Effects>/<plugin>/.../<preset>.fst`` and
``Packs/.../<sample>.wav`` (also WavPack, FLAC, OGG, MP3, AIFF). Reading that
folder is the whole index, so the list is exactly what this FL install has.
A sound's ``path`` is relative to that folder and is what the loaders accept.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path, PurePosixPath

from fl_mcp.schemas import Sound, SoundKind

PRESETS_DIR = "Plugin presets"
SAMPLES_DIR = "Packs"
PRESET_SUFFIXES = frozenset({".fst"})
SAMPLE_SUFFIXES = frozenset({".wav", ".wv", ".flac", ".ogg", ".mp3", ".aif", ".aiff"})
SEARCH_LIMIT_MAX = 200


@functools.lru_cache(maxsize=4)
def _index(patches: Path) -> tuple[Sound, ...]:
    """Every stock preset and sample under ``patches``, read once per process."""
    found: list[Sound] = []
    for kind, folder, suffixes in (
        ("preset", PRESETS_DIR, PRESET_SUFFIXES),
        ("sample", SAMPLES_DIR, SAMPLE_SUFFIXES),
    ):
        root = patches / folder
        for directory, subdirs, files in os.walk(root):
            subdirs.sort()
            for file in sorted(files):
                path = Path(directory, file)
                if path.suffix.lower() not in suffixes:
                    continue
                relative = PurePosixPath(path.relative_to(patches).as_posix())
                # Plugin presets/<Generators|Effects>/<plugin>/...: the plugin is the third part.
                plugin = relative.parts[2] if kind == "preset" and len(relative.parts) > 3 else None
                found.append(Sound(kind=kind, name=path.stem, plugin=plugin, path=str(relative)))
    return tuple(found)


def search(
    patches: Path,
    query: str = "",
    *,
    kind: SoundKind | None = None,
    plugin: str | None = None,
    limit: int = 20,
) -> list[Sound]:
    """Stock sounds whose path holds every word of ``query`` (any case).

    ``kind`` keeps only presets or only samples; ``plugin`` keeps only that
    plugin's presets (exact name, any case). Results keep FL's folder order.
    """
    words = query.casefold().split()
    wanted_plugin = plugin.casefold() if plugin is not None else None
    out: list[Sound] = []
    for sound in _index(patches):
        if len(out) >= min(limit, SEARCH_LIMIT_MAX):
            break
        if kind is not None and sound.kind != kind:
            continue
        if wanted_plugin is not None and (sound.plugin or "").casefold() != wanted_plugin:
            continue
        haystack = sound.path.casefold()
        if all(word in haystack for word in words):
            out.append(sound)
    return out


def resolve(patches: Path, sound: str, kind: SoundKind) -> Path:
    """The file for a stock sound's ``path`` (as ``search`` lists it), checked.

    Raises ValueError when the path leaves FL's folder, has the wrong kind of
    file, or does not exist.
    """
    relative = PurePosixPath(sound.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{sound!r} is not a stock sound path; use a path from fl.sounds()")
    top, suffixes = (PRESETS_DIR, PRESET_SUFFIXES) if kind == "preset" else (SAMPLES_DIR, SAMPLE_SUFFIXES)
    if not relative.parts or relative.parts[0] != top or relative.suffix.lower() not in suffixes:
        raise ValueError(f"{sound!r} is not a stock {kind}; use a {kind} path from fl.sounds()")
    path = patches.joinpath(*relative.parts)
    if not path.is_file():
        raise ValueError(f"FL has no {kind} at {sound!r}; search fl.sounds() for one")
    return path
