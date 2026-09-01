"""Where FL Studio and our own data live on this machine.

Two distinct trees, easy to conflate:

* FL's sandboxed Settings tree (``fl_settings_dirs``). FL's embedded Python
  has a restricted ``open()``: a piano roll script can *see* a file anywhere
  (``os.path.exists`` works) but can only *read* files inside FL's own data
  tree - anything else fails with ``<class '_io.FileIO'> returned NULL
  without setting an exception``. So the v0 script spool lives under
  'Image-Line/FL Studio/Settings' (verified on macOS FL 2025, docs/10).

* Our per-user app-data root (``app_data_root``, docs/11 "Storage layout"):

      ~/Library/Application Support/FLMCP/     (macOS)
      %LOCALAPPDATA%\\FLMCP\\                    (Windows)
      ~/.local/share/FLMCP/                    (other POSIX)
      +-- spool/<session>/...        SpoolSession dirs - GC'd on start
      +-- projects/<project-key>/    shadow.json, actions.jsonl, manifest.json,
                                     params/<plugin>.json

  The Bridge plugin and the store are native/host code with no sandbox
  restriction, so their data lives in the conventional location.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def fl_app() -> Path:
    """The FL Studio application bundle to drive.

    ``$FL_APP`` wins if set (the operator knows where it is); otherwise the
    newest installed ``/Applications/FL Studio*.app`` - so a machine with FL
    2026 is never targeted with a hardcoded 2025 path. One resolver, so the
    server and the e2e harness never disagree on which app to launch.
    """
    override = os.environ.get("FL_APP")
    if override:
        return Path(override)
    if sys.platform == "win32":
        program_files = os.environ.get("PROGRAMFILES", r"C:\\Program Files")
        installed = sorted(
            Path(program_files).glob("Image-Line/FL Studio*/FL64.exe"), reverse=True
        )
        return installed[0] if installed else Path(program_files) / "Image-Line" / "FL Studio" / "FL64.exe"
    installed = sorted(Path("/Applications").glob("FL Studio*.app"), reverse=True)
    return installed[0] if installed else Path("/Applications/FL Studio.app")


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
        one_drive = os.environ.get("ONEDRIVE")
        roots.append((Path(one_drive) if one_drive else home / "OneDrive") / "Documents" / "Image-Line")
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


def app_data_root() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "FLMCP"
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else home / "AppData" / "Local"
        return base / "FLMCP"
    return home / ".local" / "share" / "FLMCP"


def spool_dir() -> Path:
    return app_data_root() / "spool"


def projects_dir() -> Path:
    return app_data_root() / "projects"


def project_dir(project_key: str) -> Path:
    # A key containing a separator is a bug at the call site, not a layout
    # choice: pathlib REPLACES the base when joined with an absolute path, so
    # an absolute key would silently point the store at (say) the .flp file
    # itself - found live when a full project path was used as the key.
    if "/" in project_key or "\\" in project_key:
        raise ValueError(f"project_key must be a single path component, got {project_key!r}")
    return projects_dir() / project_key


def params_dir(project_key: str) -> Path:
    return project_dir(project_key) / "params"
