"""Production sessions fail closed on an unprofiled FL build; development warns."""

from __future__ import annotations

import pytest
from session_fakes import NOTES, TARGET, make_session

from fl_mcp import errors


def test_unknown_build_refuses_session_live_operations(monkeypatch, tmp_path):
    monkeypatch.setattr("fl_mcp.session.facade.profile_gate", lambda: (False, "unsupported FL 99 build 1 (arm64)"))
    session, _ = make_session(project=tmp_path / "agent.flp", session_spool_root=tmp_path)
    with pytest.raises(errors.ToolError) as excinfo:
        session.write_notes(TARGET, NOTES, "merge")
    assert excinfo.value.category == "unsupported_fl_build"
    assert "unsupported FL 99 build 1 (arm64)" in excinfo.value.message


def test_unknown_build_warns_but_development_can_continue(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("fl_mcp.session.facade.profile_gate", lambda: (False, "unsupported FL 99 build 1 (arm64)"))
    session, _ = make_session(project=tmp_path / "agent.flp")
    assert session._profile_supported is False
    assert "unsupported FL 99" in caplog.text
