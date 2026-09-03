"""Create the project the agent writes into.

Derived from a stock FL template so it has real instrument channels to play
notes on, then saved where the autonomous backend looks for it. Nothing here
touches the GUI.

    python -m fl_mcp.make_agent_project
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from fl_mcp.fl import paths as flpaths
from fl_mcp.template import template_path

from .session import DEFAULT_PROJECT_NAME  # THE definition; session resolves the same path


def destination() -> Path:
    settings = flpaths.primary_settings_dir()
    if settings is None:
        raise FileNotFoundError(
            "FL Studio settings folder not found "
            "(~/Documents/Image-Line/FL Studio/Settings)"
        )
    projects = settings.parent / "Projects"
    projects.mkdir(parents=True, exist_ok=True)
    return projects / DEFAULT_PROJECT_NAME


def create(force: bool = False) -> Path:
    target = destination()
    if target.is_file() and not force:
        return target
    shutil.copy2(template_path(), target)
    return target


def main() -> int:
    try:
        path = create(force="--force" in sys.argv)
    except FileNotFoundError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    print(f"Agent project: {path}")
    print("\nNotes written by the agent go here, and FL is opened on it.")
    print("Use --force to reset it back to the packaged session template.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
