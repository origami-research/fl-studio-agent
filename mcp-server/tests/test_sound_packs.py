"""fl-mcp-install's CC0 packs: every file is the pinned one, or it is not there.

fl.sounds lists pack samples by path and the sound index describes them by those bytes,
so every box must have the same bytes for the same engine version: a file is kept only
when it hashes to the manifest's SHA-256."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files

import pytest

from fl_mcp.fl import sound_packs

GOOD, OTHER = b"RIFF clap", b"RIFF tampered"
SPEC = {
    "version": "test",
    "packs": [{"id": "vcsl", "name": "VCSL", "repo": "sgossner/VCSL", "commit": "c0ffee"}],
    "files": [
        {"pack": "vcsl", "src": "Idiophones/Clap 1.wav", "path": "CC0/VCSL/Hand Percussion/Clap 1.wav",
         "sha256": hashlib.sha256(GOOD).hexdigest(), "bytes": len(GOOD)},
    ],
}
CLAP = "CC0/VCSL/Hand Percussion/Clap 1.wav"


@pytest.fixture(autouse=True)
def _spec(monkeypatch):
    monkeypatch.setattr(sound_packs, "manifest", lambda: SPEC)


def test_each_file_comes_from_its_pinned_commit_and_is_checked(tmp_path):
    urls = []
    step = sound_packs.install(tmp_path, fetch=lambda url: urls.append(url) or GOOD)

    assert step.status == "OK" and "1 files downloaded" in step.detail
    assert urls == ["https://raw.githubusercontent.com/sgossner/VCSL/c0ffee/Idiophones/Clap%201.wav"]
    assert (tmp_path / CLAP).read_bytes() == GOOD
    again = sound_packs.install(tmp_path, fetch=lambda url: pytest.fail("a current file was fetched again"))
    assert again.status == "OK" and again.detail.endswith("already current")


def test_a_file_that_is_not_the_pinned_one_never_stays(tmp_path):
    """A changed upstream file, or a local one edited since, is refused and removed, so no box
    plays bytes its version does not name."""
    (tmp_path / CLAP).parent.mkdir(parents=True)
    (tmp_path / CLAP).write_bytes(OTHER)

    step = sound_packs.install(tmp_path, fetch=lambda url: OTHER)

    assert step.status == "WARN" and "SHA-256 differs" in step.detail
    assert not (tmp_path / CLAP).exists()


def test_a_download_that_fails_is_advice_not_a_crash(tmp_path):
    def offline(url):
        raise OSError("offline")

    step = sound_packs.install(tmp_path, fetch=offline)

    assert step.status == "WARN" and "rerun fl-mcp-install" in step.detail


def test_a_file_that_cannot_be_written_is_advice_not_a_crash(tmp_path):
    """Disk full or no permission: install_all must still print its JSON report."""
    (tmp_path / "CC0").mkdir()
    (tmp_path / "CC0").chmod(0o500)
    try:
        step = sound_packs.install(tmp_path, fetch=lambda url: GOOD)
    finally:
        (tmp_path / "CC0").chmod(0o700)

    assert step.status == "WARN" and "not written" in step.detail


def test_files_the_manifest_no_longer_lists_are_removed(tmp_path):
    old = tmp_path / "CC0/VCSL/Old/Gone.wav"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old version")

    sound_packs.install(tmp_path, fetch=lambda url: GOOD)

    assert not old.exists() and (tmp_path / CLAP).is_file()


def test_the_shipped_manifest_stays_inside_the_packs_folder(tmp_path):
    """Paths come from a file in the wheel, but a bad entry must still never write outside CC0/."""
    shipped = json.loads(files("fl_mcp").joinpath("assets", "sound-packs.json").read_text(encoding="utf-8"))
    assert len({f["path"] for f in shipped["files"]}) == len(shipped["files"])
    for entry in shipped["files"]:
        assert sound_packs._target(tmp_path, entry["path"]).is_relative_to(tmp_path / "CC0")
    with pytest.raises(ValueError):
        sound_packs._target(tmp_path, "CC0/../../evil.wav")
