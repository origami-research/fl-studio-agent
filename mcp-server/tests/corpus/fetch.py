"""Download the corpus into ./data/ (best-effort, offline-safe)."""
from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from .manifest import CORPUS, CORRUPT

DATA = Path(__file__).resolve().parent / "data"


def fetch_all(timeout: float = 20.0) -> dict[str, Path]:
    """Fetch any missing corpus files. Returns {name: path} for those present.

    Never raises on a network failure - a file that can't be fetched is simply
    absent, and tests skip it. Files already on disk are not re-downloaded.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    present: dict[str, Path] = {}
    for name, (url, _src, _lic) in {**CORPUS, **CORRUPT}.items():
        dest = DATA / name
        if not dest.exists():
            try:
                with urllib.request.urlopen(url, timeout=timeout) as resp:
                    dest.write_bytes(resp.read())
            except (urllib.error.URLError, TimeoutError, OSError):
                continue
        if dest.exists():
            present[name] = dest
    return present
