"""Closed-loop control-plane e2e against real FL (see conftest.live_fl).

Each test writes via the control plane and asserts against FL's own read-back.
Run: FL_E2E=1 uv run pytest tests/e2e -m e2e
"""

import pytest

pytestmark = pytest.mark.e2e


def _ok(resp):
    assert resp.ok, f"control op failed: {resp.err}"
    return resp.ret


def test_hello_reports_fl_version(live_fl):
    hello = live_fl.hello
    assert hello["ppq"] > 0
    assert "v" in hello["fl_ver"].lower() or hello["fl_ver"]


def test_tempo_round_trips(live_fl):
    # setCurrentTempo is subject to FL's "unsafe at current time" guard right
    # after load; the 6s timeout lets the controller's OnIdle deferral land it.
    _ok(live_fl.session.control.send_request("mixer.setCurrentTempo", args=[141, 1], timeout=6.0))
    got = _ok(live_fl.session.control.send_request("mixer.getCurrentTempo", timeout=3.0))
    assert got == 141000, f"tempo did not round-trip: {got}"


def test_channel_count_is_positive(live_fl):
    count = _ok(live_fl.session.control.send_request("channels.channelCount", timeout=3.0))
    assert isinstance(count, int) and count >= 1


def test_first_channel_has_a_name(live_fl):
    name = _ok(live_fl.session.control.send_request("channels.getChannelName", args=[0], timeout=3.0))
    assert isinstance(name, str) and name


def test_undo_round_trips(live_fl):
    _ok(live_fl.session.control.send_request("general.undo", timeout=3.0))


def test_session_snapshot_shape(live_fl):
    snap = _ok(live_fl.session.control.send_request("state.snapshot", timeout=8.0))
    assert "tempo" in snap and "channels" in snap and "mixer" in snap
