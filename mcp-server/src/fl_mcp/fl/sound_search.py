"""Download what fl.sounds ranks by: the sound model and the sound index, checked.

``assets/sound-search.json`` pins each file (sound_model.MODEL_FILES and the index,
sound_index.INDEX_FILE) by its name here, the URL it is downloaded from (this
repository's GitHub releases), its size and its SHA-256. The model is
LAION's CLAP music checkpoint (CC0, NOTICE.md) exported to ONNX; the index is built
per FL build by scripts/build_sound_index.py and holds numbers
and paths only, none of FL's audio. A file is kept only when its bytes hash to the
pinned value, so every machine with this engine version ranks sounds the same way.

A rerun fetches only what is missing or differs and removes files the manifest no
longer lists, so the folder is exactly this version's files. Files the release does not
publish (HTTP 404: the release is still a draft) are not a failure: fl.sounds then
matches by name, and fl-mcp-doctor's sound_search line says so.
"""

from __future__ import annotations

import json
import urllib.error
from functools import cache
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from typing import Any

from .paths import sound_search_dir
from .sound_packs import Fetch, current, download, place
from .steps import InstallStep


@cache
def manifest() -> dict[str, Any]:
    return json.loads(files("fl_mcp").joinpath("assets", "sound-search.json").read_text(encoding="utf-8"))


def install(root: Path | None = None, fetch: Fetch | None = None) -> InstallStep:
    """Make ``root`` (the sound search folder) hold exactly this version's model and index."""
    root = sound_search_dir() if root is None else root
    fetch = download if fetch is None else fetch
    spec = manifest()
    wanted = {root / entry["name"]: entry for entry in spec["files"]}
    fetched, failed, unpublished = 0, [], set()

    def get(url: str) -> bytes:
        try:
            return fetch(url)
        except urllib.error.HTTPError as exc:
            if exc.code == HTTPStatus.NOT_FOUND:  # GitHub's answer for a draft release's files
                unpublished.add(url)
            raise

    for path, entry in wanted.items():
        if current(path, entry["sha256"]):
            continue
        problem = place(path, entry["sha256"], lambda url=entry["url"]: get(url))
        if problem:
            failed.append(f"{entry['name']} ({problem})")
        else:
            fetched += 1
    removed = 0
    if root.is_dir():
        for path in root.iterdir():
            if path.is_file() and path not in wanted:
                path.unlink()
                removed += 1
    what = f"the sound model and the sound index for FL {spec['fl_build']} (version {spec['version']})"
    if failed and len(unpublished) == len(failed):
        # Not a failure and nothing to fix: the release is not public (yet), and name
        # matching is the search this install has. A rerun picks the files up once it is.
        return InstallStep(
            status="OK",
            detail=f"fl.sounds matches by name only: {what} are not published for download",
        )
    if failed:
        return InstallStep(
            status="WARN",
            detail=f"{len(failed)} of {len(wanted)} sound search files could not be downloaded, so fl.sounds "
            f"matches by name only; rerun fl-mcp-install when online. First: {failed[0]}",
        )
    if not fetched and not removed:
        return InstallStep(status="OK", detail=f"{what}; already current")
    return InstallStep(status="OK", detail=f"{what}: {fetched} files downloaded and checked, in {root}")
