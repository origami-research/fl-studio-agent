"""Install the piano roll script into FL Studio's user settings directory.

FL only loads piano roll scripts from a fixed location under the user's
Image-Line data folder, so the script has to be copied there rather than
referenced in place.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .flpaths import fl_settings_dirs, piano_roll_scripts_dir

SCRIPT_FILENAME = "MCP Write Notes.pyscript"


def _source_script() -> Path:
    # repo layout: mcp-server/src/fl_mcp/install.py -> <repo>/fl-scripts/piano-roll/
    return Path(__file__).resolve().parents[3] / "fl-scripts" / "piano-roll" / SCRIPT_FILENAME


def script_dir() -> Path | None:
    return piano_roll_scripts_dir()


def script_installed_path() -> Path | None:
    target = script_dir()
    if target is None:
        return None
    path = target / SCRIPT_FILENAME
    return path if path.is_file() else None


def install() -> Path:
    source = _source_script()
    if not source.is_file():
        raise FileNotFoundError(f"script not found in repo: {source}")

    target_dir = script_dir()
    if target_dir is None:
        searched = "\n  ".join(str(p) for p in fl_settings_dirs()) or "(none)"
        raise FileNotFoundError(
            "Could not find FL Studio's 'Piano roll scripts' folder.\nSearched:\n  " + searched
        )

    dest = target_dir / SCRIPT_FILENAME
    shutil.copy2(source, dest)
    return dest


def main() -> None:
    try:
        dest = install()
    except FileNotFoundError as exc:
        print(f"Install failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Installed: {dest}")
    print("Restart FL Studio, then find it under the piano roll's Tools > Scripting menu.")


if __name__ == "__main__":
    main()
