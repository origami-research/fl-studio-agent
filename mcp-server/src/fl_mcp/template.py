"""The canonical packaged FL Studio session template."""

from __future__ import annotations

import argparse
import hashlib
from importlib.resources import files
from pathlib import Path
from typing import Sequence


def template_path() -> Path:
    """Return the installed session template's absolute filesystem path."""
    return Path(files("fl_mcp").joinpath("assets/session-template.flp")).resolve()


def main(argv: Sequence[str] | None = None) -> int:
    """Print the template path, or its SHA-256 digest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", action="store_true", help="print the template SHA-256")
    args = parser.parse_args(argv)
    path = template_path()
    print(hashlib.sha256(path.read_bytes()).hexdigest() if args.sha else path)
    return 0
