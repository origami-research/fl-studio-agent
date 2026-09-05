"""Pure helpers used by the Windows provisioning PowerShell entrypoint.

Keeping XML shaping here gives the Windows runbook a small, portable test
surface. PowerShell owns installation and filesystem privileges; this module
only parses its stable arguments and renders FL's MIDI registry fragment.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REQUEST_PORT = "FLMCP Req"
RESPONSE_PORT = "FLMCP Res"
SCRIPT_NAME = "FLMCP Bridge Script"
CONTROL_PORT = 1


@dataclass(frozen=True)
class ProvisionPaths:
    registry: Path
    installed_for: Path
    build: str


def parse_args(argv: Sequence[str] | None = None) -> ProvisionPaths:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--installed-for", required=True, type=Path)
    parser.add_argument("--build", required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"FL \d+\.\d+\.\d+ build \d+ \(x86_64\)", args.build):
        parser.error("--build must be the exact 'FL <version> build <number> (x86_64)' label")
    return ProvisionPaths(args.registry, args.installed_for, args.build)


def _set_int_value(data: str, key_name: str, value_name: str, value: int) -> tuple[str, bool]:
    pattern = (
        r'(<Key Name="' + re.escape(key_name) + r'">(?:(?!</Key>).)*?'
        r'<Value Name="' + re.escape(value_name) + r'" Type="2">)-?\d+(</Value>)'
    )
    return re.subn(pattern, r"\g<1>" + str(value) + r"\g<2>", data, count=1, flags=re.DOTALL)


def render_midi_registry(data: str) -> str:
    """Return the idempotently configured registry, or fail without a write."""
    input_key = (
        f'<Key Name="{REQUEST_PORT}">\n'
        '  <Value Name="Type" Type="2">-3</Value>\n'
        f'  <Value Name="ScriptFolder" Type="2">{SCRIPT_NAME}</Value>\n'
        '  <Value Name="IDString" Type="2">-</Value>\n'
        f'  <Value Name="Port" Type="2">{CONTROL_PORT}</Value>\n'
        '  <Value Name="Enabled" Type="2">1</Value>\n'
        '</Key>'
    )
    output_key = (
        f'<Key Name="{RESPONSE_PORT}">\n'
        f'  <Value Name="Port" Type="2">{CONTROL_PORT}</Value>\n'
        '  <Value Name="IDString" Type="2">-</Value>\n'
        '</Key>'
    )
    if f'<Key Name="{REQUEST_PORT}">' in data:
        data, ports = _set_int_value(data, REQUEST_PORT, "Port", CONTROL_PORT)
        data, enabled = _set_int_value(data, REQUEST_PORT, "Enabled", 1)
        if not ports or not enabled:
            raise ValueError(f"unexpected {REQUEST_PORT} registry shape")
    else:
        data, inserted = re.subn(
            r'(<Key Name="MIDI input">\s*)', r"\g<1>" + input_key + "\n", data, count=1
        )
        if not inserted:
            raise ValueError("MIDI input section is missing")
    if f'<Key Name="{RESPONSE_PORT}">' in data:
        data, port = _set_int_value(data, RESPONSE_PORT, "Port", CONTROL_PORT)
        if not port:
            raise ValueError(f"unexpected {RESPONSE_PORT} registry shape")
    else:
        data, inserted = re.subn(
            r'(<Key Name="MIDI output">\s*)', r"\g<1>" + output_key + "\n", data, count=1
        )
        if not inserted:
            raise ValueError("MIDI output section is missing")
    return data


def main(argv: Sequence[str] | None = None) -> None:
    paths = parse_args(argv)
    original = paths.registry.read_text(encoding="utf-8")
    rendered = render_midi_registry(original)
    paths.registry.write_text(rendered, encoding="utf-8")
    paths.installed_for.write_text(paths.build + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
