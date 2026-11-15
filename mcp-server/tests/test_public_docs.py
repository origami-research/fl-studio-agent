"""The public mirror's dead-link check (scripts/oss/public_docs.py) reads the public tree only.

The verification syncs a .venv into the public checkout; a package there carries a copy of
NOTICE.md whose relative links do not resolve beside it, and the 0.1.11 publish refused on it.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "oss" / "public_docs.py"


def _public_docs():
    spec = importlib.util.spec_from_file_location("public_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dead_links_reads_tracked_markdown_only(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("[ok](../NOTICE.md) [gone](missing.md) [web](https://x.test/a.md)\n")
    (tmp_path / "NOTICE.md").write_text("[note](docs/note.md)\n")
    licenses = tmp_path / ".venv" / "pkg.dist-info" / "licenses"
    licenses.mkdir(parents=True)
    (licenses / "NOTICE.md").write_text("[note](docs/note.md)\n")  # a copy, untracked: not the public tree
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "docs/note.md", "NOTICE.md"], check=True)

    assert _public_docs().dead_links(tmp_path) == ["docs/note.md:1: missing.md"]
