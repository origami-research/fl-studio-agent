"""Windows edges are selected at the FL boundary, never in session callers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from fl_mcp.fl import paths, process


def test_windows_paths(monkeypatch, tmp_path):
    program_files = tmp_path / "Program Files"
    app = program_files / "Image-Line" / "FL Studio 2026" / "FL64.exe"
    app.parent.mkdir(parents=True)
    app.touch()
    home = tmp_path / "home"
    settings = home / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    settings.mkdir(parents=True)
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PROGRAMFILES", str(program_files))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))

    assert paths.fl_app() == app
    assert paths.primary_settings_dir() == settings
    assert paths.app_data_root() == tmp_path / "LocalAppData" / "FLMCP"


def test_windows_processes_use_exact_image_names(monkeypatch):
    monkeypatch.setattr(process.sys, "platform", "win32")
    monkeypatch.setattr(process, "fl_app", lambda: Path(r"C:\\FL\\FL64.exe"))
    process.fl_executable_name.cache_clear()
    seen: list[list[str]] = []

    def run(args, **kwargs):
        seen.append(args)
        if args[0] == "tasklist":
            return subprocess.CompletedProcess(args, 0, '"FL64.exe","1234","Console","1","10 K"\n')
        return subprocess.CompletedProcess(args, 0, "")

    monkeypatch.setattr(process.subprocess, "run", run)
    monkeypatch.setattr(process.os, "getpid", lambda: 99)

    assert process.fl_pids() == [1234]
    process.fl_kill()
    assert seen[-1] == ["taskkill", "/PID", "1234", "/F", "/T"]
    assert process.fl_kill_pid(1234) is True
    assert seen[-1] == ["taskkill", "/PID", "1234", "/F", "/T"]


def test_windows_launch_spawns_the_executable_and_waits_for_its_bridge_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(process.sys, "platform", "win32")
    monkeypatch.setattr(process, "fl_app", lambda: Path(r"C:\\FL\\FL64.exe"))
    started: list[tuple[list[str], str, str, bool]] = []
    monkeypatch.setattr(
        process.subprocess,
        "Popen",
        lambda args, **kwargs: started.append(
            (
                args,
                kwargs["env"]["FLMCP_SPOOL_ROOT"],
                kwargs["env"]["FLMCP_AGENT_PROJECT"],
                kwargs["start_new_session"],
            )
        ),
    )
    monkeypatch.setattr(process, "wait_for_bridge_pid", lambda *args: 1234)

    assert process.launch_fl(tmp_path / "song.flp", spool_root=tmp_path / "spool") == 1234

    assert started == [
        (
            [r"C:\\FL\\FL64.exe", str(tmp_path / "song.flp")],
            str(tmp_path / "spool"),
            str(tmp_path / "song.flp"),
            True,
        )
    ]
