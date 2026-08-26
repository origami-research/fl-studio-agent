"""Install FL Studio MCP into FL's user settings, with no FL UI required.

Three file operations, all done with FL closed:
  1. the piano-roll note script  -> Settings/Piano roll scripts/
  2. the controller script        -> Settings/Hardware/FLMCP/
  3. the MIDI controller assignment (input+output ports) -> reg.xml

FL only loads these from fixed locations under the user's Image-Line data
folder, so they are copied there rather than referenced in place. The reg.xml
edit is what removes the last manual step (assigning the controller in FL's
MIDI settings); it was validated by hand on 2026-08-26 (docs/12/14) and is
codified here.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from .flpaths import fl_settings_dirs, piano_roll_scripts_dir

SCRIPT_FILENAME = "MCP Write Notes.pyscript"
CONTROLLER_FILENAME = "device_FLMCP.py"
CONTROLLER_SUBDIR = "FLMCP"  # under Settings/Hardware/
REG_XML = Path.home() / "Library" / "Preferences" / "Image-Line" / "reg.xml"

# The controller assignment reg.xml expects. ScriptFolder must match the
# controller script's `# name=` header (FL keys the device by that name).
REQ_PORT_NAME = "FLMCP Req"
RES_PORT_NAME = "FLMCP Res"
SCRIPT_NAME = "FLMCP Bridge Script"
CONTROL_PORT = 1  # any matching int; input and output must agree


def _source_script() -> Path:
    # repo layout: mcp-server/src/fl_mcp/install.py -> <repo>/fl-scripts/piano-roll/
    return Path(__file__).resolve().parents[3] / "fl-scripts" / "piano-roll" / SCRIPT_FILENAME


def _source_controller() -> Path:
    return Path(__file__).resolve().parents[3] / "fl-scripts" / "controller" / CONTROLLER_FILENAME


def script_dir() -> Path | None:
    return piano_roll_scripts_dir()


def script_installed_path() -> Path | None:
    target = script_dir()
    if target is None:
        return None
    path = target / SCRIPT_FILENAME
    return path if path.is_file() else None


def install() -> Path:
    source = _source_script()
    if not source.is_file():
        raise FileNotFoundError(f"script not found in repo: {source}")

    target_dir = script_dir()
    if target_dir is None:
        searched = "\n  ".join(str(p) for p in fl_settings_dirs()) or "(none)"
        raise FileNotFoundError(
            "Could not find FL Studio's 'Piano roll scripts' folder.\nSearched:\n  " + searched
        )

    dest = target_dir / SCRIPT_FILENAME
    shutil.copy2(source, dest)
    return dest


def install_controller() -> Path:
    """Copy the controller (MIDI device) script into Settings/Hardware/FLMCP/."""
    source = _source_controller()
    if not source.is_file():
        raise FileNotFoundError(f"controller script not found in repo: {source}")
    settings = next((s for s in fl_settings_dirs() if s.is_dir()), None)
    if settings is None:
        raise FileNotFoundError("Could not find FL Studio's Settings folder.")
    dest_dir = settings / "Hardware" / CONTROLLER_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / CONTROLLER_FILENAME
    shutil.copy2(source, dest)
    return dest


def configure_midi() -> str:
    """Write the FLMCP controller assignment into FL's reg.xml (FL closed).

    Idempotent: if the FLMCP input/output keys already exist, their ports are
    normalized to CONTROL_PORT and the controller is enabled; otherwise the keys
    are inserted into the MIDI input/output sections. Returns a status string.
    Never raises on a missing/oddly-shaped reg.xml - returns a WARN string so the
    installer can fall back to a one-time manual assignment.
    """
    if not REG_XML.is_file():
        return f"WARN reg.xml not found at {REG_XML}; assign the controller once in FL's MIDI settings"
    data = REG_XML.read_text(encoding="utf-8")

    input_key = (
        f'<Key Name="{REQ_PORT_NAME}">\n'
        f'                  <Value Name="Type" Type="2">-3</Value>\n'
        f'                  <Value Name="ScriptFolder" Type="2">{SCRIPT_NAME}</Value>\n'
        f'                  <Value Name="IDString" Type="2">-</Value>\n'
        f'                  <Value Name="Port" Type="2">{CONTROL_PORT}</Value>\n'
        f'                  <Value Name="Enabled" Type="2">1</Value>\n'
        f'                </Key>'
    )
    output_key = (
        f'<Key Name="{RES_PORT_NAME}">\n'
        f'                  <Value Name="Port" Type="2">{CONTROL_PORT}</Value>\n'
        f'                  <Value Name="IDString" Type="2">-</Value>\n'
        f'                </Key>'
    )

    changed = False
    if REQ_PORT_NAME in data:
        # Normalize the existing input port + enabled flag within its key block.
        data, n = re.subn(
            r'(<Key Name="' + re.escape(REQ_PORT_NAME) + r'">.*?<Value Name="Port" Type="2">)-?\d+(</Value>)',
            r'\g<1>' + str(CONTROL_PORT) + r'\g<2>',
            data, count=1, flags=re.DOTALL,
        )
        changed = changed or bool(n)
    else:
        data, n = re.subn(r'(<Key Name="MIDI input">\s*)',
                          r'\g<1>' + input_key + "\n                ", data, count=1)
        if not n:
            return "WARN could not locate <Key Name=\"MIDI input\"> in reg.xml; assign controller once in FL"
        changed = True

    if RES_PORT_NAME not in data:
        data2, n = re.subn(r'(<Key Name="MIDI output">\s*)',
                           r'\g<1>' + output_key + "\n                ", data, count=1)
        if n:
            data = data2
            changed = True

    if changed:
        REG_XML.write_text(data, encoding="utf-8")
        return f"OK controller assigned to {REQ_PORT_NAME}/{RES_PORT_NAME} port {CONTROL_PORT} (reg.xml)"
    return "OK controller assignment already present"


def install_all() -> dict[str, str]:
    """Full FL-side install: note script + controller script + MIDI assignment."""
    results: dict[str, str] = {}
    try:
        results["note_script"] = f"OK {install()}"
    except FileNotFoundError as exc:
        results["note_script"] = f"FAIL {exc}"
    try:
        results["controller_script"] = f"OK {install_controller()}"
    except FileNotFoundError as exc:
        results["controller_script"] = f"FAIL {exc}"
    results["midi_assignment"] = configure_midi()
    return results


def main() -> None:
    results = install_all()
    for step, status in results.items():
        print(f"  [{status.split(' ', 1)[0]}] {step}: {status.split(' ', 1)[1] if ' ' in status else ''}")
    if any(v.startswith("FAIL") for v in results.values()):
        raise SystemExit(1)
    print("\nDone. Start your Claude session (opens the MIDI ports), then open FL Studio.")
    print("Ports must exist before FL launches; if FL is already open, restart it once.")


if __name__ == "__main__":
    main()
