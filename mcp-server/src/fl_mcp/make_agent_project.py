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

from . import flpaths
from .autonomous import DEFAULT_PROJECT_NAME


def _templates_dir() -> Path:
    import os

    app = Path(os.environ.get("FL_APP", "/Applications/FL Studio 2025.app"))
    return app / "Contents/Resources/FL/Data/Templates"


# Chosen for its channel palette: Chords, Bass, Lead and Drums, on real
# instruments (Sytrus on the lead). The previous default only had four 808 drum
# samples plus one bass, so any melody had exactly one place to go and it was
# the wrong timbre for most requests.
#
# Candidates are tried in order so a missing template degrades to a working
# project rather than an error.
TEMPLATE_CANDIDATES = (
    "Utility/Create a chord progression/Create a chord progression.flp",
    "Utility/Patch bank/Patch bank.flp",
    "Minimal/Basic with limiter/Basic with limiter.flp",
)


def stock_template() -> Path:
    base = _templates_dir()
    for relative in TEMPLATE_CANDIDATES:
        candidate = base / relative
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"No usable FL template found under {base}. "
        "Set FL_APP if FL Studio is installed elsewhere."
    )


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
    shutil.copy2(stock_template(), target)
    return target


def main() -> int:
    try:
        path = create(force="--force" in sys.argv)
    except FileNotFoundError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    print(f"Agent project: {path}")
    print("\nNotes written by the agent go here, and FL is opened on it.")
    print("Use --force to reset it back to the stock template.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
