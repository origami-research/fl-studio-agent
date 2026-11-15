"""Lane K test seam: a fake FLSession wired to fakes, per docs/21.

``sdk.connect(fake)`` is the deliberate injection point. FL IO is faked; the
Store is REAL (on tmp_path) so the write-through path - record_write, deltas,
the action log - is the shipped code, not a mock of it.
"""

from __future__ import annotations

import sys

import pytest
from sdk_fakes import DEFAULT_CHANNELS, FakeControl, FakeSession

from fl_mcp import sounds
from fl_mcp.fl import sound_packs, sound_search
from fl_mcp.store import Store


def pytest_configure(config):
    config.addinivalue_line("markers", "platform(name): test requires one operating-system contract")


def pytest_collection_modifyitems(config, items):
    for item in items:
        marker = item.get_closest_marker("platform")
        if marker and marker.args and marker.args[0] != sys.platform:
            item.add_marker(pytest.mark.skip(reason=f"requires platform {marker.args[0]!r}"))


@pytest.fixture(autouse=True)
def _no_real_sound_folders(tmp_path_factory, monkeypatch):
    """fl-mcp-install's sound steps must never reach the network or a real sound
    folder from a test: an install_all test would otherwise download 360 MB, and
    the steps remove files their manifests do not list. A search never reads this
    machine's real sound index either."""
    root = tmp_path_factory.mktemp("sounds")
    search = tmp_path_factory.mktemp("sound-search")
    monkeypatch.setattr(sound_packs, "sound_library_dir", lambda: root)
    monkeypatch.setattr(sound_search, "sound_search_dir", lambda: search)
    monkeypatch.setattr(sounds, "sound_search_dir", lambda: search)

    def offline(url: str) -> bytes:
        raise OSError(f"no network in tests: {url}")

    monkeypatch.setattr(sound_packs, "download", offline)
    monkeypatch.setattr(sound_search, "download", offline)


@pytest.fixture
def control() -> FakeControl:
    return FakeControl()


@pytest.fixture
def fake_session(tmp_path, control) -> FakeSession:
    return FakeSession(store=Store("test", root=tmp_path), channels=DEFAULT_CHANNELS, control=control)
