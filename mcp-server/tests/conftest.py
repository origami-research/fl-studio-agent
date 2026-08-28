"""Lane K test seam: a fake FLSession wired to fakes, per docs/21.

``sdk.connect(fake)`` is the deliberate injection point. FL IO is faked; the
Store is REAL (on tmp_path) so the write-through path - record_write, deltas,
the action log - is the shipped code, not a mock of it.
"""

from __future__ import annotations

import pytest
from sdk_fakes import DEFAULT_CHANNELS, FakeControl, FakeSession

from fl_mcp.store import Store


@pytest.fixture
def control() -> FakeControl:
    return FakeControl()


@pytest.fixture
def fake_session(tmp_path, control) -> FakeSession:
    return FakeSession(store=Store("test", root=tmp_path), channels=DEFAULT_CHANNELS, control=control)
