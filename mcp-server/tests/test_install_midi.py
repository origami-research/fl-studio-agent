"""Tests for install.configure_midi() reg.xml editing.

Covers the safety contract: idempotent, normalizes both ports, never edits
outside the FLMCP keys, and WARNs (never corrupts) on unexpected shapes -
including the reproduced cross-key overreach the audit flagged as CRITICAL.
"""


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
