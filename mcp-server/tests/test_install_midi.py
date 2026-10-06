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
    assert status.startswith("OK")
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

    assert status.startswith("OK")
    assert "FLMCP Req #" not in text and "FLMCP Res #" not in text
    assert text.count('<Key Name="FLMCP Req">') == 1
    assert text.count('<Key Name="FLMCP Res">') == 1
    assert reg.with_name("reg.xml.bak").read_text(encoding="utf-8") == before


def test_running_fl_leaves_its_registry_untouched(reg, monkeypatch):
    reg.write_text(FRESH, encoding="utf-8")
    monkeypatch.setattr(install, "fl_process_running", lambda: True)

    assert install.configure_midi().startswith("WARN FL Studio is running")
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
    assert install.configure_midi().startswith("WARN")


def test_install_controller_copies_the_versioned_source(tmp_path, monkeypatch):
    settings = tmp_path / "Settings"
    settings.mkdir()
    monkeypatch.setattr(install, "fl_settings_dirs", lambda: [settings])

    installed = install.install_controller()

    assert installed.read_bytes() == controller_script_bytes()


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

    install.main(["--json"])

    steps = json.loads(capsys.readouterr().out)
    assert steps["note_script"]["status"] == "OK"
    assert steps["controller_script"]["status"] == "OK"
    assert steps["midi_assignment"]["status"] == "WARN"
    packaged = files("fl_mcp.fl_scripts").joinpath(install.SCRIPT_FILENAME).read_bytes()
    assert (settings / "Piano roll scripts" / install.SCRIPT_FILENAME).read_bytes() == packaged


def test_no_midi_section_warns_not_corrupt(reg):
    reg.write_text("<?xml version='1.0'?>\n<XMLReg></XMLReg>", encoding="utf-8")
    before = reg.read_text()
    assert install.configure_midi().startswith("WARN")
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
    assert status.startswith("WARN")


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


IL = install.IMAGE_LINE_KEY


def test_windows_assignment_lands_in_the_newest_fl_settings_key():
    """The values FL 2026 for Windows wrote for a hand assignment (live box
    2026-10-05), under the newest 'FL Studio <N>' key only."""
    reg = _FakeRegistry([rf"{IL}\FL Studio 21\General", rf"{IL}\FL Studio 26\General", rf"{IL}\Shared\Paths"])
    status = install.configure_midi_registry(reg)
    assert status.startswith("OK") and "FL Studio 26" in status
    assert reg.values(rf"{IL}\FL Studio 26\Devices\MIDI input\FLMCP Req") == {
        "Type": "-3", "ScriptFolder": "FLMCP Bridge Script", "IDString": "-", "Port": "1", "Enabled": "1",
    }
    assert reg.values(rf"{IL}\FL Studio 26\Devices\MIDI output\FLMCP Res") == {"Port": "1", "IDString": "-"}
    # The same-named output is parked off the control port, or FL links it to
    # the script at startup and the replies loop back into FL's own input.
    assert reg.values(rf"{IL}\FL Studio 26\Devices\MIDI output\FLMCP Req") == {"Port": "2", "IDString": "-"}
    assert not any("FL Studio 21\\Devices" in k for k in reg.keys)


def test_windows_assignment_keeps_fl_values_it_does_not_own():
    req = rf"{IL}\FL Studio 26\Devices\MIDI input\FLMCP Req"
    reg = _FakeRegistry([req])
    reg.keys[req] = {"Type": "0", "ScriptFolder": "", "ConnectionCounter": "4"}
    assert install.configure_midi_registry(reg).startswith("OK")
    assert reg.values(req)["ConnectionCounter"] == "4"
    assert reg.values(req)["Type"] == "-3"


def test_windows_assignment_warns_when_fl_never_ran_for_this_user():
    assert install.configure_midi_registry(_FakeRegistry([rf"{IL}\Shared\Paths"])).startswith("WARN")


def test_windows_assignment_fails_on_a_read_back_mismatch():
    reg = _FakeRegistry([rf"{IL}\FL Studio 26\General"], refuse="Enabled")
    assert install.configure_midi_registry(reg).startswith("FAIL")
