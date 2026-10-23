"""Tests for install.configure_midi() reg.xml editing.

Covers the safety contract: idempotent, normalizes both ports, never edits
outside the FLMCP keys, and WARNs (never corrupts) on unexpected shapes -
including the reproduced cross-key overreach the audit flagged as CRITICAL.
"""

import json
from importlib.resources import files

import pytest

from fl_mcp.controller_version import controller_script_bytes
from fl_mcp.fl import install


@pytest.fixture
def reg(tmp_path, monkeypatch):
    p = tmp_path / "reg.xml"
    monkeypatch.setattr(install, "reg_xml_path", lambda: p)
    return p


FRESH = """<?xml version="1.0"?>
<XMLReg>
  <Key Name="Settings">
    <Key Name="MIDI">
      <Key Name="MIDI input">
              </Key>
              <Key Name="MIDI output">
              </Key>
    </Key>
  </Key>
</XMLReg>"""


def test_fresh_insert(reg):
    reg.write_text(FRESH, encoding="utf-8")
    status = install.configure_midi()
    assert status.status == "OK"
    text = reg.read_text()
    assert text.count('<Key Name="FLMCP Req">') == 1
    assert text.count('<Key Name="FLMCP Res">') == 1
    assert f'<Value Name="Port" Type="2">{install.CONTROL_PORT}</Value>' in text


def test_idempotent_and_no_dup(reg):
    reg.write_text(FRESH, encoding="utf-8")
    install.configure_midi()
    install.configure_midi()
    install.configure_midi()
    text = reg.read_text()
    assert text.count('<Key Name="FLMCP Req">') == 1
    assert text.count('<Key Name="FLMCP Res">') == 1


def test_removes_numbered_virtual_port_duplicates_and_keeps_canonical_assignment(reg):
    reg.write_text(
        FRESH.replace(
            '<Key Name="MIDI input">',
            '''<Key Name="MIDI input">
                <Key Name="FLMCP Req #2"><Value Name="Port" Type="2">9</Value></Key>
                <Key Name="FLMCP Req #9"><Value Name="Port" Type="2">8</Value></Key>''',
        ).replace(
            '<Key Name="MIDI output">',
            '''<Key Name="MIDI output">
                <Key Name="FLMCP Res #2"><Value Name="Port" Type="2">9</Value></Key>''',
        ),
        encoding="utf-8",
    )
    before = reg.read_text(encoding="utf-8")

    status = install.configure_midi()
    text = reg.read_text(encoding="utf-8")

    assert status.status == "OK"
    assert "FLMCP Req #" not in text and "FLMCP Res #" not in text
    assert text.count('<Key Name="FLMCP Req">') == 1
    assert text.count('<Key Name="FLMCP Res">') == 1
    assert reg.with_name("reg.xml.bak").read_text(encoding="utf-8") == before


def test_running_fl_leaves_its_registry_untouched(reg, monkeypatch):
    reg.write_text(FRESH, encoding="utf-8")
    monkeypatch.setattr(install, "fl_process_running", lambda: True)

    assert str(install.configure_midi()).startswith("WARN FL Studio is running")
    assert reg.read_text(encoding="utf-8") == FRESH


def test_normalizes_both_ports(reg):
    reg.write_text(FRESH, encoding="utf-8")
    install.configure_midi()
    # Tamper both ports to a wrong/stale value, then re-run.
    text = reg.read_text().replace(
        f'<Value Name="Port" Type="2">{install.CONTROL_PORT}</Value>',
        '<Value Name="Port" Type="2">-1</Value>',
    )
    reg.write_text(text)
    install.configure_midi()
    text = reg.read_text()
    # Both FLMCP keys back to CONTROL_PORT; no stray -1 left in an FLMCP key.
    assert text.count(f'<Value Name="Port" Type="2">{install.CONTROL_PORT}</Value>') >= 2


def test_missing_reg_warns(reg):
    # reg fixture points reg_xml_path() at a nonexistent path.
    assert install.configure_midi().status == "WARN"


def test_install_controller_copies_the_versioned_source(tmp_path, monkeypatch):
    settings = tmp_path / "Settings"
    settings.mkdir()
    monkeypatch.setattr(install, "fl_settings_dirs", lambda: [settings])

    first = install.install_controller()
    again = install.install_controller()

    installed = settings / "Hardware" / install.CONTROLLER_SUBDIR / install.CONTROLLER_FILENAME
    assert installed.read_bytes() == controller_script_bytes()
    # Rerunning is the documented fix for a stale controller, so it must say what it did.
    assert first.detail.endswith("(installed)")
    assert again.detail.endswith("(already current)")


def test_rerun_leaves_an_assigned_reg_xml_and_its_backup_alone(reg):
    """A second install must not rewrite reg.xml or replace the backup of the
    user's pre-install registry with a copy of our own output."""
    reg.write_text(FRESH, encoding="utf-8")
    install.configure_midi()
    backup = reg.with_name("reg.xml.bak")
    assert backup.read_text() == FRESH
    assigned = reg.read_text()

    step = install.configure_midi()

    assert step.status == "OK" and "already current" in step.detail
    assert reg.read_text() == assigned
    assert backup.read_text() == FRESH


def _fake_fl(tmp_path, monkeypatch):
    """An FL user-data tree under tmp_path with the Bridge step stubbed (it has its own tests)."""
    settings = tmp_path / "Image-Line" / "FL Studio" / "Settings"
    (settings / "Piano roll scripts").mkdir(parents=True)
    monkeypatch.setattr(install, "fl_settings_dirs", lambda: [settings])
    monkeypatch.setattr(install, "piano_roll_scripts_dir", lambda: settings / "Piano roll scripts")
    monkeypatch.setattr(install, "reg_xml_path", lambda: tmp_path / "missing-reg.xml")
    monkeypatch.setattr(install, "fl_process_running", lambda: False)
    monkeypatch.setattr(install.bridge, "install_release", lambda: install.InstallStep(status="OK", detail="stub"))
    from fl_mcp import make_agent_project

    monkeypatch.setattr(make_agent_project.flpaths, "primary_settings_dir", lambda: settings)


def test_windows_install_checks_the_loopmidi_ports(tmp_path, monkeypatch):
    """WinMM has no virtual ports: without loopMIDI's two ports the server can
    never reach FL, so the installer says exactly how to create them."""
    from fl_mcp.core.carriers import wire

    _fake_fl(tmp_path, monkeypatch)
    monkeypatch.setattr(install.sys, "platform", "win32")
    monkeypatch.setattr(install, "configure_midi", lambda: install.InstallStep(status="OK", detail="stub"))

    def missing():
        raise wire.ControlUnavailable("loopMIDI port 'FLMCP Req' is missing.")

    monkeypatch.setattr(wire, "windows_midi_control", missing)

    step = install.install_all()["midi_ports"]

    assert step.status == "WARN"
    assert "FLMCP Req" in step.detail and "winget install TobiasErichsen.loopMIDI" in step.detail


def test_install_all_needs_only_the_installed_package(tmp_path, monkeypatch, capsys):
    """The box has no repository checkout: every FL-side file must come out of
    the wheel (fl_mcp.fl_scripts), and --json is the machine-readable result
    the Windows arena install step records."""
    settings = tmp_path / "Settings"
    (settings / "Piano roll scripts").mkdir(parents=True)
    monkeypatch.setattr(install, "fl_settings_dirs", lambda: [settings])
    monkeypatch.setattr(install, "piano_roll_scripts_dir", lambda: settings / "Piano roll scripts")
    monkeypatch.setattr(install, "reg_xml_path", lambda: tmp_path / "missing-reg.xml")
    monkeypatch.setattr(install, "fl_process_running", lambda: False)

    from fl_mcp import make_agent_project

    monkeypatch.setattr(make_agent_project.flpaths, "primary_settings_dir", lambda: settings)
    # The box pins its own Bridge DLL (provision.ps1), so session-install.ps1 passes --no-bridge.
    install.main(["--json", "--no-bridge"])

    steps = json.loads(capsys.readouterr().out)
    assert steps["agent_project"]["status"] == "OK"
    assert (tmp_path / "Projects" / "FLMCP Agent.flp").is_file()
    assert steps["note_script"]["status"] == "OK"
    assert steps["controller_script"]["status"] == "OK"
    assert steps["midi_assignment"]["status"] == "WARN"
    packaged = files("fl_mcp.fl_scripts").joinpath(install.SCRIPT_FILENAME).read_bytes()
    assert (settings / "Piano roll scripts" / install.SCRIPT_FILENAME).read_bytes() == packaged


def test_no_midi_section_warns_not_corrupt(reg):
    reg.write_text("<?xml version='1.0'?>\n<XMLReg></XMLReg>", encoding="utf-8")
    before = reg.read_text()
    assert install.configure_midi().status == "WARN"
    assert reg.read_text() == before  # untouched


def test_cross_key_overreach_regression(reg):
    """CRITICAL regression: an FLMCP Req key with no Port value must NOT cause
    the normalize to reach across </Key> and rewrite another device's port."""
    xml = """<?xml version="1.0"?>
<XMLReg><Key Name="Settings"><Key Name="MIDI">
      <Key Name="MIDI input">
                <Key Name="FLMCP Req">
                  <Value Name="Type" Type="2">-3</Value>
                </Key>
                <Key Name="My Real Keyboard">
                  <Value Name="Port" Type="2">7</Value>
                </Key>
              </Key>
              <Key Name="MIDI output">
              </Key>
</Key></Key></XMLReg>"""
    reg.write_text(xml, encoding="utf-8")
    status = install.configure_midi()
    text = reg.read_text()
    # The unrelated device's port MUST be untouched...
    assert '<Key Name="My Real Keyboard">\n                  <Value Name="Port" Type="2">7</Value>' in text
    # ...and the function must have WARNed rather than silently "succeeded".
    assert status.status == "WARN"


class _FakeRegistry:
    """HKEY_CURRENT_USER as a dict of key path -> REG_SZ values."""

    def __init__(self, keys=(), refuse: str | None = None):
        self.keys: dict[str, dict[str, str]] = {k: {} for k in keys}
        self._refuse = refuse

    def subkeys(self, path):
        prefix = path + "\\"
        return sorted({k[len(prefix):].split("\\")[0] for k in self.keys if k.startswith(prefix)})

    def values(self, path):
        return dict(self.keys.get(path, {}))

    def set_values(self, path, values):
        stored = self.keys.setdefault(path, {})
        stored.update({n: v for n, v in values.items() if n != self._refuse})

    def delete_key(self, path):
        self.keys.pop(path, None)


IL = install.IMAGE_LINE_KEY


# HKCU\Software\Image-Line\FL Studio 26\Devices as FL rewrote it on a clean exit.
_FL_PERSISTED_DEVICES = {
    r"MIDI input\FLMCP Req": {"Type": "-3", "ScriptFolder": "FLMCP Bridge Script", "IDString": "-",
                              "Port": "1", "Enabled": "1", "ConnectionCounter": "4"},
    r"MIDI input\FLMCP Res": {"Type": "0", "ScriptFolder": "", "IDString": "-", "ConnectionCounter": "1",
                              "Port": "-1", "Enabled": "0"},
    r"MIDI output\FLMCP Req": {"Port": "2", "IDString": "-", "Sync": "0", "SendAllNotesOff": "1"},
    r"MIDI output\FLMCP Res": {"Port": "1", "IDString": "-", "Sync": "0", "SendAllNotesOff": "1"},
    r"MIDI output\Microsoft GS Wavetable Synth": {"Port": "-1", "Sync": "0", "SendAllNotesOff": "1", "IDString": "-"},
    r"MIDI output\Microsoft MIDI Mapper": {"Port": "-1", "Sync": "0", "SendAllNotesOff": "1", "IDString": "-"},
}


def test_windows_assignment_resets_the_newest_fl_settings_key_to_the_installer_set():
    """FL started from its own exit rewrite links no output to the controller;
    the installer's key set (the fields FL writes for a hand assignment) links."""
    reg = _FakeRegistry([rf"{IL}\FL Studio 21\General", rf"{IL}\FL Studio 26\General", rf"{IL}\Shared\Paths"])
    for sub, values in _FL_PERSISTED_DEVICES.items():
        reg.keys[rf"{IL}\FL Studio 26\Devices\{sub}"] = dict(values)
    step = install.configure_midi_registry(reg)
    assert step.status == "OK" and "FL Studio 26" in step.detail
    prefix = rf"{IL}\FL Studio 26\Devices" + "\\"
    assert {k[len(prefix):]: v for k, v in reg.keys.items() if k.startswith(prefix)} == {
        r"MIDI input\FLMCP Req": {"Type": "-3", "ScriptFolder": "FLMCP Bridge Script", "IDString": "-",
                                  "Port": "1", "Enabled": "1"},
        r"MIDI output\FLMCP Res": {"Port": "1", "IDString": "-"},
        # Parked off the control port, or FL links it to the script and the replies loop back.
        r"MIDI output\FLMCP Req": {"Port": "2", "IDString": "-"},
    }
    assert not any("FL Studio 21\\Devices" in k for k in reg.keys)


def test_windows_assignment_keeps_a_device_someone_assigned():
    """Only what FL merely remembers goes: a real output with a port, and any
    input, stay as their owner set them."""
    keyboard_in = rf"{IL}\FL Studio 26\Devices\MIDI input\My Keyboard"
    synth_out = rf"{IL}\FL Studio 26\Devices\MIDI output\My Synth"
    reg = _FakeRegistry([rf"{IL}\FL Studio 26\General"])
    reg.keys[keyboard_in] = {"Type": "0", "Port": "-1", "Enabled": "1"}
    reg.keys[synth_out] = {"Port": "3", "IDString": "-"}
    assert install.configure_midi_registry(reg).status == "OK"
    assert reg.values(keyboard_in) == {"Type": "0", "Port": "-1", "Enabled": "1"}
    assert reg.values(synth_out) == {"Port": "3", "IDString": "-"}


def test_windows_assignment_warns_when_fl_never_ran_for_this_user():
    assert install.configure_midi_registry(_FakeRegistry([rf"{IL}\Shared\Paths"])).status == "WARN"


def test_windows_assignment_fails_on_a_read_back_mismatch():
    reg = _FakeRegistry([rf"{IL}\FL Studio 26\General"], refuse="Enabled")
    assert install.configure_midi_registry(reg).status == "FAIL"
