"""Locating FL Studio's user data directories.

Why this matters: FL Studio's embedded Python has a restricted ``open()``. A
piano roll script can *see* a file anywhere (``os.path.exists`` works) but can
only *read* files inside FL's own data tree — attempting otherwise fails with
``<class '_io.FileIO'> returned NULL without setting an exception``.

So the spool has to live under 'Image-Line/FL Studio/Settings', not in a
conventional application-data location. Verified on macOS with FL Studio 2025;
see docs/10-v0-vertical-slice.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Subdirectories of FL's Settings folder that hold the spool.
#
# FL's permissions are asymmetric and narrower for writes than reads. Measured
# on FL Studio 2025 (from the script's own report of which roots accepted a
# write):
#
#   Settings/Piano roll scripts/         read + write
#   Settings/Piano roll scripts/FLMCP/   read + write   <- preferred
#   Settings/FLMCP/                      read only      <- unusable, dropped
#
# So write access follows 'Piano roll scripts' recursively, not all of
# 'Settings'. The dedicated subfolder is preferred to keep spool files out of
# the folder FL scans for scripts; the bare folder is the fallback.
SPOOL_SUBDIRS = ("Piano roll scripts/FLMCP", "Piano roll scripts")


def fl_settings_dirs() -> list[Path]:
    """Candidate 'Image-Line/FL Studio/Settings' directories, most likely first."""
    home = Path.home()
    roots = [home / "Documents" / "Image-Line"]
    if sys.platform == "win32":
        roots.append(home / "OneDrive" / "Documents" / "Image-Line")
    else:
        roots.append(home / "Image-Line")
    return [r / "FL Studio" / "Settings" for r in roots if (r / "FL Studio" / "Settings").is_dir()]


def primary_settings_dir() -> Path | None:
    dirs = fl_settings_dirs()
    return dirs[0] if dirs else None


def piano_roll_scripts_dir() -> Path | None:
    for settings in fl_settings_dirs():
        target = settings / "Piano roll scripts"
        if target.is_dir():
            return target
    return None
