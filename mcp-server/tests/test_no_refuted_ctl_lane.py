"""Prevent the refuted file-based control lane from returning."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERSEDED_HEADING = "## Superseded"
FORBIDDEN = (
    "Ctl" + "SpoolControl",
    "ctl_" + "cmd_",
    "ctl_" + "resp_",
    "ctl." + "poll",
    "ctl." + "selftest",
)


def test_refuted_control_lane_names_are_absent_outside_supersession_record():
    tracked_files = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    matches: list[str] = []
    for relative_path in tracked_files:
        path = REPO_ROOT / relative_path
        if not path.is_file():
            continue
        content = path.read_text(errors="ignore")
        if relative_path == "docs/34-per-session-control.md":
            content = content.split(SUPERSEDED_HEADING, maxsplit=1)[0]
        for forbidden in FORBIDDEN:
            if forbidden in content:
                matches.append(f"{relative_path}: {forbidden}")
    assert not matches, "refuted control lane names remain:\n" + "\n".join(matches)
