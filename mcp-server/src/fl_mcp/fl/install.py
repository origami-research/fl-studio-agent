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
from pathlib import Path

from fl_mcp.controller_version import controller_script_bytes

from .paths import fl_settings_dirs, piano_roll_scripts_dir
from .process import fl_process_running

SCRIPT_FILENAME = "MCP Write Notes.pyscript"
CONTROLLER_FILENAME = "device_FLMCP.py"
CONTROLLER_SUBDIR = "FLMCP"  # under Settings/Hardware/


def reg_xml_path() -> Path:
    """FL's MIDI-assignment registry. A function, not an import-time constant:
    binding Path.home() at import captured ambient config before a test (or an
    unusual HOME) could redirect it."""
    return Path.home() / "Library" / "Preferences" / "Image-Line" / "reg.xml"

# The controller assignment reg.xml expects. ScriptFolder must match the
# controller script's `# name=` header (FL keys the device by that name).
REQ_PORT_NAME = "FLMCP Req"
RES_PORT_NAME = "FLMCP Res"
SCRIPT_NAME = "FLMCP Bridge Script"
CONTROL_PORT = 1  # any matching int; input and output must agree


def _source_script() -> Path:
    # repo layout: mcp-server/src/fl_mcp/fl/install.py -> <repo>/fl-scripts/piano-roll/
    return Path(__file__).resolve().parents[4] / "fl-scripts" / "piano-roll" / SCRIPT_FILENAME


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
    settings = next((s for s in fl_settings_dirs() if s.is_dir()), None)
    if settings is None:
        raise FileNotFoundError("Could not find FL Studio's Settings folder.")
    dest_dir = settings / "Hardware" / CONTROLLER_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / CONTROLLER_FILENAME
    dest.write_bytes(controller_script_bytes())
    return dest


def _set_int_value(data: str, key_name: str, value_name: str, new_value: int) -> tuple[str, bool]:
    """Set <Value Name="value_name" Type="2">N</Value> inside the given key's
    own block only. The body matcher `(?:(?!</Key>).)*?` refuses to cross a
    </Key>, so a missing value in this key can never make the match overreach
    into a later, unrelated device's key. Returns (data, matched).
    """
    pattern = (
        r'(<Key Name="' + re.escape(key_name) + r'">(?:(?!</Key>).)*?'
        r'<Value Name="' + re.escape(value_name) + r'" Type="2">)-?\d+(</Value>)'
    )
    data, n = re.subn(pattern, r"\g<1>" + str(new_value) + r"\g<2>", data, count=1, flags=re.DOTALL)
    return data, bool(n)


def _remove_numbered_port_keys(data: str, port_name: str) -> tuple[str, int]:
    """Remove FL's duplicate virtual-port registry entries, never a real device."""
    pattern = (
        r'\s*<Key Name="' + re.escape(port_name) + r' #\d+">'
        r'(?:(?!</Key>).)*?</Key>'
    )
    return re.subn(pattern, "", data, flags=re.DOTALL)


def configure_midi() -> str:
    """Write the FLMCP controller assignment into FL's reg.xml (FL closed).

    Idempotent. If the FLMCP input/output keys are absent, they are inserted
    into the MIDI input/output sections. If present, the input and output Port
    values are normalized to CONTROL_PORT (they must agree for FL to route
    request/response) and the input Enabled flag is set. Returns a status
    string; never raises on a missing/oddly-shaped reg.xml and never edits
    outside the FLMCP keys - returns WARN so the installer can fall back to a
    one-time manual assignment.
    """
    if fl_process_running():
        return "WARN FL Studio is running; close it before changing its MIDI registry"
    reg_xml = reg_xml_path()
    if not reg_xml.is_file():
        return f"WARN reg.xml not found at {reg_xml}; assign the controller once in FL's MIDI settings"
    data = reg_xml.read_text(encoding="utf-8")

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

    data, removed_inputs = _remove_numbered_port_keys(data, REQ_PORT_NAME)
    data, removed_outputs = _remove_numbered_port_keys(data, RES_PORT_NAME)
    changed = bool(removed_inputs or removed_outputs)
    enabled_missing = False

    # --- input key ---
    if f'<Key Name="{REQ_PORT_NAME}">' in data:
        data, ok = _set_int_value(data, REQ_PORT_NAME, "Port", CONTROL_PORT)
        if not ok:
            return (f"WARN {REQ_PORT_NAME} key present but has no Port value; "
                    "unexpected reg.xml shape - assign the controller once in FL")
        data, enabled_ok = _set_int_value(data, REQ_PORT_NAME, "Enabled", 1)
        enabled_missing = not enabled_ok  # an OK below must not overclaim this
        changed = True
    else:
        data, n = re.subn(r'(<Key Name="MIDI input">\s*)',
                          r'\g<1>' + input_key + "\n                ", data, count=1)
        if not n:
            return 'WARN could not locate <Key Name="MIDI input"> in reg.xml; assign controller once in FL'
        changed = True

    # --- output key ---
    if f'<Key Name="{RES_PORT_NAME}">' in data:
        data, ok = _set_int_value(data, RES_PORT_NAME, "Port", CONTROL_PORT)
        if not ok:
            return (f"WARN {RES_PORT_NAME} key present but has no Port value; "
                    "unexpected reg.xml shape - assign the controller once in FL")
        changed = True
    else:
        data, n = re.subn(r'(<Key Name="MIDI output">\s*)',
                          r'\g<1>' + output_key + "\n                ", data, count=1)
        if not n:
            return 'WARN could not locate <Key Name="MIDI output"> in reg.xml; assign controller once in FL'
        changed = True

    if changed:
        shutil.copy2(reg_xml, reg_xml.with_name(f"{reg_xml.name}.bak"))
        reg_xml.write_text(data, encoding="utf-8")
        if enabled_missing:
            return (f"WARN ports assigned, but {REQ_PORT_NAME} has no Enabled value to set; "
                    "check the controller is enabled in FL's MIDI settings")
        return f"OK controller assigned to {REQ_PORT_NAME}/{RES_PORT_NAME} port {CONTROL_PORT} (reg.xml; backup written)"
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
    print("\nDone. Open FL Studio first, then start your Claude session so the MIDI ports hot-plug.")


if __name__ == "__main__":
    main()
