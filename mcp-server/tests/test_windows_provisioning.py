"""Offline guardrails for the Windows provisioning argument and XML seam."""

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[2] / "scripts" / "windows" / "provisioning.py"
SPEC = spec_from_file_location("windows_provisioning", MODULE)
assert SPEC and SPEC.loader
provisioning = module_from_spec(SPEC)
sys.modules[SPEC.name] = provisioning
SPEC.loader.exec_module(provisioning)

FRESH = '''<XMLReg><Key Name="MIDI input"></Key><Key Name="MIDI output"></Key></XMLReg>'''


def test_provisioning_arguments_require_an_exact_build_label(tmp_path):
    paths = provisioning.parse_args(
        ["--registry", str(tmp_path / "reg.xml"), "--installed-for", str(tmp_path / "build"),
         "--build", "FL 26.1.5 build 5384 (x86_64)"]
    )
    assert paths.build == "FL 26.1.5 build 5384 (x86_64)"
    with pytest.raises(SystemExit):
        provisioning.parse_args(["--registry", "r", "--installed-for", "b", "--build", "26.1.5"])


def test_midi_registry_template_is_idempotent_and_does_not_touch_other_devices():
    source = FRESH.replace(
        '</Key><Key Name="MIDI output">',
        '</Key><Key Name="Keyboard"><Value Name="Port" Type="2">7</Value></Key><Key Name="MIDI output">',
    )
    rendered = provisioning.render_midi_registry(source)
    assert rendered.count('<Key Name="FLMCP Req">') == 1
    assert rendered.count('<Key Name="FLMCP Res">') == 1
    assert '<Key Name="Keyboard"><Value Name="Port" Type="2">7</Value></Key>' in rendered
    assert provisioning.render_midi_registry(rendered) == rendered
