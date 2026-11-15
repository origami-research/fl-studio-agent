"""Download the CC0 sample packs into the engine's sound folder, file by file, checked.

The packs (VCSL and VSCO 2 Community Edition, both CC0 1.0; NOTICE.md) are a curated
subset of two public sample libraries: ``assets/sound-packs.json`` pins each pack's
upstream commit and lists every file with its upstream path, its library path
(``CC0/<pack>/<category>/<name>.wav``, what fl.sounds lists) and its SHA-256. Nothing
is vendored into this repository: fl-mcp-install fetches each file from
raw.githubusercontent.com at the pinned commit, and a file is kept only when its
bytes hash to the pinned value, so every machine with this engine version has the
same packs, byte for byte.

A rerun fetches only what is missing or differs, and removes files the manifest no
longer lists, so the folder is exactly this version's packs.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from collections.abc import Callable
from functools import cache
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any

from . import bridge
from .bridge import DOWNLOAD_ERRORS
from .paths import sound_library_dir
from .steps import InstallStep

RAW = "https://raw.githubusercontent.com"
PACKS_TOP = "CC0"

Fetch = Callable[[str], bytes]


def download(url: str) -> bytes:
    """One file over HTTPS, verified by the OS like the Bridge's download."""
    return bridge.fetch(url)


@cache
def manifest() -> dict[str, Any]:
    return json.loads(files("fl_mcp").joinpath("assets", "sound-packs.json").read_text(encoding="utf-8"))


def _url(repo: str, commit: str, src: str) -> str:
    return f"{RAW}/{repo}/{commit}/{urllib.parse.quote(src)}"


def _target(root: Path, path: str) -> Path:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != (PACKS_TOP,):
        raise ValueError(f"sound-packs.json lists {path!r} outside {PACKS_TOP}/")
    return root.joinpath(*relative.parts)


def current(path: Path, sha256: str) -> bool:
    """Whether ``path`` holds exactly the pinned bytes."""
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha256


def place(path: Path, sha256: str, get: Callable[[], bytes]) -> str | None:
    """Download a pinned file with ``get`` and write it to ``path`` whole, or say why not.

    A file that is not the pinned one never stays, even when the download fails."""
    path.unlink(missing_ok=True)
    try:
        data = get()
    except DOWNLOAD_ERRORS as exc:
        return str(exc)
    if hashlib.sha256(data).hexdigest() != sha256:
        return "SHA-256 differs from the pinned file"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
        partial.write_bytes(data)
        partial.replace(path)
    except OSError as exc:  # disk full, no permission: reported like a failed download
        return f"not written: {exc}"
    return None


def install(root: Path | None = None, fetch: Fetch | None = None) -> InstallStep:
    """Make ``root`` (the engine's sound folder) hold exactly this version's packs."""
    root = sound_library_dir() if root is None else root
    fetch = download if fetch is None else fetch
    spec = manifest()
    packs = {pack["id"]: pack for pack in spec["packs"]}
    wanted = {_target(root, entry["path"]): entry for entry in spec["files"]}
    fetched, failed = 0, []
    for path, entry in wanted.items():
        if current(path, entry["sha256"]):
            continue
        pack = packs[entry["pack"]]
        url = _url(pack["repo"], pack["commit"], entry["src"])
        problem = place(path, entry["sha256"], lambda url=url: fetch(url))
        if problem:
            failed.append(f"{entry['path']} ({problem})")
        else:
            fetched += 1
    removed = _remove_unlisted(root / PACKS_TOP, set(wanted))
    names = " and ".join(pack["name"] for pack in spec["packs"])
    if failed:
        return InstallStep(
            status="WARN",
            detail=f"{len(failed)} of {len(wanted)} CC0 pack files could not be downloaded; "
            f"rerun fl-mcp-install when online. First: {failed[0]}",
        )
    if not fetched and not removed:
        return InstallStep(status="OK", detail=f"{names} ({len(wanted)} files, version {spec['version']}); already current")
    return InstallStep(
        status="OK", detail=f"{names}: {fetched} files downloaded and checked, version {spec['version']}, in {root}"
    )


def _remove_unlisted(top: Path, wanted: set[Path]) -> int:
    """Remove files under ``top`` the manifest does not list (an older version's)."""
    removed = 0
    for directory, _, names in os.walk(top):
        for name in names:
            path = Path(directory, name)
            if path not in wanted:
                path.unlink()
                removed += 1
    return removed
