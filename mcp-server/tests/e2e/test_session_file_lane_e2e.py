"""Live proof that the session file lane cannot restart FL during a match."""

from __future__ import annotations

import pytest

from fl_mcp import errors
from fl_mcp.fl.process import fl_pids

pytestmark = pytest.mark.e2e


def test_session_file_edit_keeps_the_live_fl_pid(live_fl, tmp_path):
    """The refusal happens before a project write or lifecycle call can kill FL."""
    session = live_fl.session
    session.session_spool_root = tmp_path
    before = fl_pids()
    assert before, "the live fixture must own an FL process"

    with pytest.raises(errors.ToolError) as excinfo, session.file_edit():
        pass

    assert excinfo.value.category == "file_lane_requires_reload"
    assert fl_pids() == before, "session file_edit changed the live FL process set"
