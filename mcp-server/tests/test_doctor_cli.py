"""fl-mcp-doctor is the box install's verification step: one JSON report on
stdout, exit 0 only when every plane is connected, and the session is closed
either way (it holds the MIDI ports FL's next client needs)."""

from __future__ import annotations

import json

import pytest

from fl_mcp import doctor
from fl_mcp.schemas import BridgeInstall, DoctorReport, PlaneStatus


class _Session:
    def __init__(self, report: DoctorReport) -> None:
        self.report = report
        self.closed = False

    def doctor(self) -> DoctorReport:
        return self.report

    def close(self) -> None:
        self.closed = True


def _report(status: str) -> DoctorReport:
    plane = PlaneStatus(connected=status == "ok", detail="probe")
    bridge = BridgeInstall(installed=False, path='/FL/Plugins/Fruity/Effects/FL MCP Bridge')
    return DoctorReport(status=status, control_plane=plane, note_plane=plane, project_file=plane, bridge=bridge)


@pytest.mark.parametrize(("status", "code"), [("ok", 0), ("not_ready", 1)])
def test_doctor_prints_the_report_and_exits_by_status(monkeypatch, capsys, status, code):
    session = _Session(_report(status))
    monkeypatch.setattr(doctor, "create_session", lambda: session)
    assert doctor.main() == code
    assert DoctorReport.model_validate(json.loads(capsys.readouterr().out)).status == status
    assert session.closed
