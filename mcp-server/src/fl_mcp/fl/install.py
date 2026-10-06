r"""Install FL Studio MCP into FL's user settings, with no FL UI required.

Three file operations, all done with FL closed:
  1. the piano-roll note script  -> Settings/Piano roll scripts/
  2. the controller script        -> Settings/Hardware/FLMCP/
  3. the MIDI controller assignment (input+output ports) -> FL's registry:
     reg.xml on macOS, HKCU\Software\Image-Line\FL Studio <N> on Windows

FL only loads these from fixed locations under the user's Image-Line data
folder, so they are copied there rather than referenced in place. The
assignment is what removes the last manual step (assigning the controller in
FL's MIDI settings); it was validated by hand on 2026-08-26 on macOS (docs/12/14)
and read back from a hand assignment on a live Windows box on 2026-10-05.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

from fl_mcp.controller_version import controller_script_bytes

from .paths import fl_settings_dirs, piano_roll_scripts_dir
from .process import fl_process_running

if sys.platform == "win32":
    import winreg

SCRIPT_FILENAME = "MCP Write Notes.pyscript"
CONTROLLER_FILENAME = "device_FLMCP.py"
CONTROLLER_SUBDIR = "FLMCP"  # under Settings/Hardware/


def reg_xml_path() -> Path:
    """FL's MIDI-assignment registry on macOS (FL's emulated registry file). A
    function, not an import-time constant: binding Path.home() at import
    captured ambient config before a test (or an unusual HOME) could redirect it.
    FL for Windows has no reg.xml; it uses the real registry (configure_midi_registry).
    """
    return Path.home() / "Library" / "Preferences" / "Image-Line" / "reg.xml"

# The controller assignment FL's registry expects. ScriptFolder must match the
# controller script's `# name=` header (FL keys the device by that name).
REQ_PORT_NAME = "FLMCP Req"
RES_PORT_NAME = "FLMCP Res"
SCRIPT_NAME = "FLMCP Bridge Script"
CONTROL_PORT = 1  # any matching int; input and output must agree


def script_dir() -> Path | None:
    return piano_roll_scripts_dir()


def script_installed_path() -> Path | None:
    target = script_dir()
    if target is None:
        return None
    path = target / SCRIPT_FILENAME
    return path if path.is_file() else None


def install() -> Path:
    """Copy the packaged piano-roll script into Settings/Piano roll scripts/."""
    target_dir = script_dir()
    if target_dir is None:
        searched = "\n  ".join(str(p) for p in fl_settings_dirs()) or "(none)"
        raise FileNotFoundError(
            "Could not find FL Studio's 'Piano roll scripts' folder.\nSearched:\n  " + searched
        )

    dest = target_dir / SCRIPT_FILENAME
    dest.write_bytes(files("fl_mcp.fl_scripts").joinpath(SCRIPT_FILENAME).read_bytes())
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


IMAGE_LINE_KEY = r"Software\Image-Line"
_FL_SETTINGS_KEY = re.compile(r"FL Studio (\d+)")


class FLRegistry(Protocol):
    """The slice of HKEY_CURRENT_USER the Windows assignment touches."""

    def subkeys(self, path: str) -> list[str]: ...
    def values(self, path: str) -> dict[str, str]: ...
    def set_values(self, path: str, values: Mapping[str, str]) -> None: ...


class CurrentUserRegistry:
    """FLRegistry over the real HKEY_CURRENT_USER (Windows only)."""

    def subkeys(self, path: str) -> list[str]:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            return []
        names: list[str] = []
        with key:
            while True:
                try:
                    names.append(winreg.EnumKey(key, len(names)))
                except OSError:  # ERROR_NO_MORE_ITEMS ends the enumeration
                    return names

    def values(self, path: str) -> dict[str, str]:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            return {}
        found: dict[str, str] = {}
        with key:
            while True:
                try:
                    name, data, _kind = winreg.EnumValue(key, len(found))
                except OSError:  # ERROR_NO_MORE_ITEMS ends the enumeration
                    return found
                found[name] = str(data)

    def set_values(self, path: str, values: Mapping[str, str]) -> None:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            for name, value in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


# loopMIDI ports are bidirectional, so FL also lists "FLMCP Req" as an output.
# At startup FL links a scripted input to the same-named output when that
# output has no port, and the script then replies into its own input instead
# of "FLMCP Res" (live box 2026-10-05: no ctl.hello answer until this output
# was parked on another port).
PARKED_PORT = CONTROL_PORT + 1

# What FL 2026 for Windows itself writes (REG_SZ, read back from a hand
# assignment): the reg.xml fields, under <FL Studio N>\Devices.
WINDOWS_ASSIGNMENT: dict[str, dict[str, str]] = {
    rf"Devices\MIDI input\{REQ_PORT_NAME}": {
        "Type": "-3", "ScriptFolder": SCRIPT_NAME, "IDString": "-", "Port": str(CONTROL_PORT), "Enabled": "1",
    },
    rf"Devices\MIDI output\{RES_PORT_NAME}": {"Port": str(CONTROL_PORT), "IDString": "-"},
    rf"Devices\MIDI output\{REQ_PORT_NAME}": {"Port": str(PARKED_PORT), "IDString": "-"},
}


def configure_midi_registry(registry: FLRegistry) -> str:
    """Write the FLMCP controller assignment into the newest ``FL Studio <N>``
    settings key and read it back. FL reads it at startup and rewrites it on a
    clean exit, so the caller closes FL first."""
    versions = [(int(m.group(1)), name) for name in registry.subkeys(IMAGE_LINE_KEY)
                if (m := _FL_SETTINGS_KEY.fullmatch(name))]
    if not versions:
        return (f"WARN no HKCU\\{IMAGE_LINE_KEY}\\FL Studio <N> key; start FL once as this user, "
                "then rerun the install")
    root = f"{IMAGE_LINE_KEY}\\{max(versions)[1]}"
    for subkey, values in WINDOWS_ASSIGNMENT.items():
        path = f"{root}\\{subkey}"
        registry.set_values(path, values)
        stored = registry.values(path)
        if any(stored.get(name) != value for name, value in values.items()):
            return f"FAIL HKCU\\{path} reads back {stored}, wrote {values}"
    return f"OK controller assigned to {REQ_PORT_NAME}/{RES_PORT_NAME} port {CONTROL_PORT} (HKCU\\{root})"


def configure_midi() -> str:
    """Write the FLMCP controller assignment into FL's registry (FL closed):
    reg.xml on macOS, configure_midi_registry on Windows.

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
    if sys.platform == "win32":
        return configure_midi_registry(CurrentUserRegistry())
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


class InstallStep(BaseModel):
    """One install step's outcome; ``WARN`` means the step needs a human or FL restart."""

    status: Literal["OK", "WARN", "FAIL"]
    detail: str

    @classmethod
    def from_status(cls, text: str) -> InstallStep:
        """Read a ``"<STATUS> <detail>"`` line from configure_midi()."""
        status, _, detail = text.partition(" ")
        return cls.model_validate({"status": status, "detail": detail})


def install_all() -> dict[str, InstallStep]:
    """Full FL-side install: note script + controller script + MIDI assignment."""
    results: dict[str, InstallStep] = {}
    try:
        results["note_script"] = InstallStep(status="OK", detail=str(install()))
    except FileNotFoundError as exc:
        results["note_script"] = InstallStep(status="FAIL", detail=str(exc))
    try:
        results["controller_script"] = InstallStep(status="OK", detail=str(install_controller()))
    except FileNotFoundError as exc:
        results["controller_script"] = InstallStep(status="FAIL", detail=str(exc))
    results["midi_assignment"] = InstallStep.from_status(configure_midi())
    return results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Install the FL-side FL MCP files for this user.")
    parser.add_argument("--json", action="store_true", help="print one JSON object of step results")
    args = parser.parse_args(argv)
    results = install_all()
    failed = any(step.status == "FAIL" for step in results.values())
    if args.json:
        print(json.dumps({name: step.model_dump() for name, step in results.items()}))
    else:
        for name, step in results.items():
            print(f"  [{step.status}] {name}: {step.detail}")
        if not failed:
            print("\nDone. Open FL Studio first, then start your Claude session so the MIDI ports hot-plug.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
