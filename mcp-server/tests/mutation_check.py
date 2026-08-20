"""Verify the FL sandbox emulator actually reproduces real-world failures.

`tests/fl_env.py` claims to emulate FL Studio's embedded Python sandbox. That
claim is only worth something if a script with a known-bad pattern *fails* under
it. This script reverts the pyscript to each bug we hit for real and asserts the
test suite catches it.

Not part of the pytest run (it shells out to pytest repeatedly). Run manually
after changing fl_env.py or the pyscript's file handling:

    .venv/bin/python tests/mutation_check.py

Anchors are matched by regex against a *shape* rather than an exact snippet, so
routine refactors don't silently turn a mutation into a no-op. A SKIP is a
failure: it means the mutation never applied and proved nothing.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "fl-scripts" / "piano-roll" / "MCP Write Notes.pyscript"
MCP_SERVER = REPO / "mcp-server"

# (label, regex to find, replacement) — each reproduces a failure seen in FL.
MUTATIONS = [
    (
        "os.remove instead of tombstone (FL blocks remove)",
        re.compile(r"path = os\.path\.join\(root, \"pending\.json\"\)\n"
                   r"\s*if os\.path\.exists\(path\):\n"
                   r"\s*_write_text\(path, tombstone\)"),
        "os.remove(os.path.join(root, \"pending.json\"))",
    ),
    (
        "except OSError instead of Exception (FL raises SystemError)",
        re.compile(r"(?<=    try:\n        f = open\(path, \"r\"\)\n)    except Exception"),
        "    except OSError",
    ),
    (
        "apply(form) with no default (FL calls apply() bare)",
        re.compile(r"def apply\(form=None\):"),
        "def apply(form):",
    ),
    (
        "open(..., encoding=) (FL rejects the kwarg)",
        re.compile(r"f = open\(path, \"r\"\)"),
        'f = open(path, "r", encoding="utf-8")',
    ),
    (
        "spool outside FL's data tree (FL refuses to read it)",
        re.compile(r"SPOOL_SUBDIRS = \[[^\]]*\]", re.S),
        'SPOOL_SUBDIRS = [os.path.join("..", "..", "..", "Library", "FLMCP")]',
    ),
]


def main() -> int:
    original = SCRIPT.read_text()
    problems = []

    for label, pattern, replacement in MUTATIONS:
        mutated, count = pattern.subn(replacement, original, count=1)
        if count == 0:
            print(f"  SKIP    {label}  <- anchor no longer matches; mutation proved nothing")
            problems.append(label)
            continue

        SCRIPT.write_text(mutated)
        try:
            result = subprocess.run(
                [".venv/bin/python", "-m", "pytest", "-q", "--no-header", "-x"],
                cwd=MCP_SERVER,
                capture_output=True,
                text=True,
            )
        finally:
            SCRIPT.write_text(original)

        if result.returncode == 0:
            print(f"  MISSED  {label}")
            problems.append(label)
        else:
            print(f"  CAUGHT  {label}")

    print()
    if problems:
        print("Emulator is NOT faithful. Unproven or missed:")
        for label in problems:
            print(f"  - {label}")
        return 1
    print(f"Emulator reproduces all {len(MUTATIONS)} known real-world failures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
