from __future__ import annotations

import json
import shutil
from pathlib import Path

from fl_mcp import dev_session
from fl_mcp.core.carriers.udp import BridgeEndpoint


def test_default_project_is_a_disposable_bridge_template():
    project, copied = dev_session._project(None)
    assert copied == project
    assert project.name == "fl_mcp_bridge.flp"
    template = Path(dev_session.__file__).resolve().parents[3] / "harness" / "projects" / "fl_mcp_bridge.flp"
    assert project.read_bytes() == template.read_bytes()
    shutil.rmtree(project.parent)


def test_no_repl_launches_waits_and_tears_down_owned_fl(monkeypatch, tmp_path):
    spool = tmp_path / "spool"
    project = tmp_path / "song.flp"
    calls: list[object] = []
    endpoint = BridgeEndpoint(proto=2, port=9000, token="a" * 32, pid=4242)

    monkeypatch.setattr(
        dev_session, "launch_fl", lambda path, *, spool_root: calls.append((path, spool_root))
    )
    monkeypatch.setattr(dev_session, "_wait_for_bridge", lambda root: endpoint)
    monkeypatch.setattr(dev_session, "_wait_for_hello", lambda root: calls.append("hello"))
    monkeypatch.setattr(dev_session.sdk, "connect", lambda: calls.append("connect"))
    monkeypatch.setattr(dev_session, "fl_kill_pid", lambda pid: calls.append(("kill", pid)))

    assert dev_session.main(["--project", str(project), "--spool", str(spool), "--no-repl"]) == 0
    assert calls == [(project.resolve(), spool.resolve()), "hello", "connect", ("kill", 4242)]
    assert not (spool / dev_session.MARKER_FILE).exists()


def test_exec_prints_trailing_result_and_restores_environment(monkeypatch, tmp_path, capsys):
    spool = tmp_path / "spool"
    project = tmp_path / "song.flp"
    endpoint = BridgeEndpoint(proto=2, port=9000, token="a" * 32, pid=7)
    monkeypatch.delenv(dev_session.ENV_PROJECT, raising=False)
    monkeypatch.delenv(dev_session.ENV_SPOOL, raising=False)
    monkeypatch.setattr(dev_session, "launch_fl", lambda *args, **kwargs: None)
    monkeypatch.setattr(dev_session, "_wait_for_bridge", lambda root: endpoint)
    monkeypatch.setattr(dev_session, "_wait_for_hello", lambda root: None)
    monkeypatch.setattr(dev_session.sdk, "connect", lambda: object())
    monkeypatch.setattr(dev_session, "fl_kill_pid", lambda pid: None)

    assert dev_session.main(["--project", str(project), "--spool", str(spool), "--exec", "1 + 2"]) == 0
    assert capsys.readouterr().out == "3\n"
    assert dev_session.ENV_PROJECT not in dev_session.os.environ
    assert dev_session.ENV_SPOOL not in dev_session.os.environ


def test_teardown_requires_its_own_marker(monkeypatch, tmp_path):
    marker = tmp_path / dev_session.MARKER_FILE
    marker.write_text(json.dumps({"token": "other"}))
    killed: list[int] = []
    monkeypatch.setattr(dev_session, "fl_kill_pid", killed.append)

    dev_session._teardown(marker, "ours", 99)

    assert killed == []
    assert marker.exists()
