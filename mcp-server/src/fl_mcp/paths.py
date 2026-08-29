"""The v1 storage root (docs/11 "Storage layout").

    ~/Library/Application Support/FLMCP/     (macOS)
    %LOCALAPPDATA%\\FLMCP\\                    (Windows)
    ~/.local/share/FLMCP/                    (other POSIX)
    +-- spool/<session>/...        SpoolSession dirs (spool.py) - GC'd on start
    +-- projects/<project-key>/
        +-- shadow.json            records.py
        +-- actions.jsonl          actions.py
        +-- manifest.json          channel/pattern names and numbers
        +-- params/<plugin>.json   cached parameter names per plugin

Not to be confused with flpaths.py: that module locates FL Studio's own
sandboxed Settings tree, which the v0 piano-roll-script note plane needs
because FL's embedded Python can only open() files inside it. The Bridge
plugin (v1) is native code with no such restriction, so its spool lives in
the normal per-user app-data location instead.
"""

from __future__ import annotations

import sys
from pathlib import Path


def app_data_root() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "FLMCP"
    if sys.platform == "win32":
        import os

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
