r"""Install FL Studio MCP into FL's user settings, with no FL UI required.

``fl-mcp-install`` is the one FL-side setup command. Every step is idempotent:
it reports ``installed`` when it changed something and ``already current`` when
it found the file already right, so rerunning it after an upgrade is the fix
for a stale controller. Done with FL closed:
  1. the agent project            -> <FL user data>/Projects/FLMCP Agent.flp (only if missing)
  2. the piano-roll note script  -> Settings/Piano roll scripts/
  3. the controller script        -> Settings/Hardware/FLMCP/
  4. the MIDI controller assignment (input+output ports) -> FL's registry:
     reg.xml on macOS, HKCU\Software\Image-Line\FL Studio <N> on Windows
  5. Windows only: the two loopMIDI ports the controller talks over must exist
     (macOS needs nothing: the server opens virtual ports itself)
  6. the native Bridge plugin, which the engine needs for notes and render: the
     prebuilt one from this version's GitHub release, checksum-verified
     (fl/bridge.py); ``--bridge PATH`` installs your own build instead.
     ``--no-bridge`` is for hosts that install their own Bridge build (the
     arena boxes pin theirs by SHA-256 in provision.ps1); it is not a user choice

FL only loads these from fixed locations under the user's Image-Line data
folder (and the Bridge from inside FL's own install), so they are copied there
rather than referenced in place. The assignment is what removes the last
manual step (assigning the controller in FL's MIDI settings); its fields are
the ones FL writes for a hand assignment.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import sys
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Protocol

from fl_mcp.controller_version import controller_script_bytes

from . import bridge
from .paths import bridge_binary_name, fl_settings_dirs, piano_roll_scripts_dir
from .process import fl_process_running
from .steps import InstallStep

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


def _ok(where: str) -> InstallStep:
    return InstallStep(status="OK", detail=f"controller assigned to {REQ_PORT_NAME}/{RES_PORT_NAME} port {CONTROL_PORT} ({where})")


def _warn(detail: str) -> InstallStep:
    return InstallStep(status="WARN", detail=detail)


def script_dir() -> Path | None:
    return piano_roll_scripts_dir()


def script_installed_path() -> Path | None:
    target = script_dir()
    if target is None:
        return None
    path = target / SCRIPT_FILENAME
    return path if path.is_file() else None


def _place(dest: Path, data: bytes) -> InstallStep:
    """Write ``data`` to ``dest`` unless it is already exactly that; say which."""
    if dest.is_file() and dest.read_bytes() == data:
        return InstallStep(status="OK", detail=f"{dest} (already current)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return InstallStep(status="OK", detail=f"{dest} (installed)")


def install_note_script() -> InstallStep:
    """Copy the packaged piano-roll script into Settings/Piano roll scripts/."""
    target_dir = script_dir()
    if target_dir is None:
        searched = "\n  ".join(str(p) for p in fl_settings_dirs()) or "(none)"
        raise FileNotFoundError(
            "Could not find FL Studio's 'Piano roll scripts' folder; start FL Studio once, then rerun.\n"
            "Searched:\n  " + searched
        )
    return _place(target_dir / SCRIPT_FILENAME, files("fl_mcp.fl_scripts").joinpath(SCRIPT_FILENAME).read_bytes())


def install_controller() -> InstallStep:
    """Copy the controller (MIDI device) script into Settings/Hardware/FLMCP/."""
    settings = next((s for s in fl_settings_dirs() if s.is_dir()), None)
    if settings is None:
        raise FileNotFoundError("Could not find FL Studio's Settings folder; start FL Studio once, then rerun.")
    return _place(settings / "Hardware" / CONTROLLER_SUBDIR / CONTROLLER_FILENAME, controller_script_bytes())


def install_agent_project() -> InstallStep:
    """Create the one project the agent writes into, from the packaged template.
    An existing agent project is the user's work in progress and is kept."""
    from fl_mcp import make_agent_project  # imports the session package; only this step needs it

    target = make_agent_project.destination()
    existed = target.is_file()
    make_agent_project.create()
    return InstallStep(status="OK", detail=f"{target} ({'already present' if existed else 'created'})")


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
    def delete_key(self, path: str) -> None: ...


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

    def delete_key(self, path: str) -> None:
        """Delete one leaf key (FL's device keys have no subkeys); an absent key is already gone."""
        with contextlib.suppress(FileNotFoundError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)


# loopMIDI ports are bidirectional, so FL also lists "FLMCP Req" as an output.
# At startup FL links a scripted input to the same-named output when that
# output has no port, and the script then replies into its own input instead
# of "FLMCP Res", so that output is parked on another port.
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


# FL for Windows rewrites Devices on every clean exit with an entry for every
# MIDI port it has seen, and started from that rewrite it links no output to
# the controller script (docs/35). So the assignment owns its ports' keys
# outright and drops outputs FL remembers without a port; an output with a
# port (a real device someone assigned) is kept.
NO_PORT = "-1"


def _stale_device_keys(registry: FLRegistry, devices: str) -> list[str]:
    """Keys under ``devices`` FL must not start with: entries for our ports that
    are not the assignment's own, and outputs remembered without a port."""
    stale: list[str] = []
    for section in ("MIDI input", "MIDI output"):
        for name in registry.subkeys(f"{devices}\\{section}"):
            if rf"Devices\{section}\{name}" in WINDOWS_ASSIGNMENT:
                continue
            path = f"{devices}\\{section}\\{name}"
            ours = name in (REQ_PORT_NAME, RES_PORT_NAME)
            portless = section == "MIDI output" and registry.values(path).get("Port", NO_PORT) == NO_PORT
            if ours or portless:
                stale.append(path)
    return stale


def configure_midi_registry(registry: FLRegistry) -> InstallStep:
    """Make the newest ``FL Studio <N>`` settings key hold exactly the FLMCP
    controller assignment and nothing FL remembered that unlinks it, then read
    it back. FL reads it at startup and rewrites it on a clean exit, so the
    caller closes FL first, and a box re-applies it before every FL launch."""
    versions = [(int(m.group(1)), name) for name in registry.subkeys(IMAGE_LINE_KEY)
                if (m := _FL_SETTINGS_KEY.fullmatch(name))]
    if not versions:
        return _warn(f"no HKCU\\{IMAGE_LINE_KEY}\\FL Studio <N> key; start FL once as this user, then rerun the install")
    root = f"{IMAGE_LINE_KEY}\\{max(versions)[1]}"
    devices = f"{root}\\Devices"
    for path in _stale_device_keys(registry, devices):
        registry.delete_key(path)
    for subkey, values in WINDOWS_ASSIGNMENT.items():
        path = f"{root}\\{subkey}"
        registry.delete_key(path)
        registry.set_values(path, values)
        stored = registry.values(path)
        if stored != values:
            return InstallStep(status="FAIL", detail=f"HKCU\\{path} reads back {stored}, wrote {values}")
    return _ok(f"HKCU\\{root}")


def configure_midi() -> InstallStep:
    """Write the FLMCP controller assignment into FL's registry (FL closed):
    reg.xml on macOS, configure_midi_registry on Windows.

    Idempotent. If the FLMCP input/output keys are absent, they are inserted
    into the MIDI input/output sections. If present, the input and output Port
    values are normalized to CONTROL_PORT (they must agree for FL to route
    request/response) and the input Enabled flag is set. Never raises on a
    missing/oddly-shaped reg.xml and never edits outside the FLMCP keys -
    returns WARN so the installer can fall back to a one-time manual assignment.
    """
    if fl_process_running():
        return _warn("FL Studio is running; close it before changing its MIDI registry")
    if sys.platform == "win32":
        return configure_midi_registry(CurrentUserRegistry())
    reg_xml = reg_xml_path()
    if not reg_xml.is_file():
        return _warn(f"reg.xml not found at {reg_xml}; assign the controller once in FL's MIDI settings")
    original = data = reg_xml.read_text(encoding="utf-8")

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

    data, _ = _remove_numbered_port_keys(data, REQ_PORT_NAME)
    data, _ = _remove_numbered_port_keys(data, RES_PORT_NAME)
    enabled_missing = False

    # --- input key ---
    if f'<Key Name="{REQ_PORT_NAME}">' in data:
        data, ok = _set_int_value(data, REQ_PORT_NAME, "Port", CONTROL_PORT)
        if not ok:
            return _warn(f"{REQ_PORT_NAME} key present but has no Port value; "
                         "unexpected reg.xml shape - assign the controller once in FL")
        data, enabled_ok = _set_int_value(data, REQ_PORT_NAME, "Enabled", 1)
        enabled_missing = not enabled_ok  # an OK below must not overclaim this
    else:
        data, n = re.subn(r'(<Key Name="MIDI input">\s*)',
                          r'\g<1>' + input_key + "\n                ", data, count=1)
        if not n:
            return _warn('could not locate <Key Name="MIDI input"> in reg.xml; assign controller once in FL')

    # --- output key ---
    if f'<Key Name="{RES_PORT_NAME}">' in data:
        data, ok = _set_int_value(data, RES_PORT_NAME, "Port", CONTROL_PORT)
        if not ok:
            return _warn(f"{RES_PORT_NAME} key present but has no Port value; "
                         "unexpected reg.xml shape - assign the controller once in FL")
    else:
        data, n = re.subn(r'(<Key Name="MIDI output">\s*)',
                          r'\g<1>' + output_key + "\n                ", data, count=1)
        if not n:
            return _warn('could not locate <Key Name="MIDI output"> in reg.xml; assign controller once in FL')

    if enabled_missing:
        if data != original:
            _write_reg_xml(reg_xml, data)
        return _warn(f"ports assigned, but {REQ_PORT_NAME} has no Enabled value to set; "
                     "check the controller is enabled in FL's MIDI settings")
    if data == original:
        return _ok("reg.xml; already current")
    _write_reg_xml(reg_xml, data)
    return _ok("reg.xml; backup written")


def _write_reg_xml(reg_xml: Path, data: str) -> None:
    shutil.copy2(reg_xml, reg_xml.with_name(f"{reg_xml.name}.bak"))
    reg_xml.write_text(data, encoding="utf-8")


LOOPMIDI_FIX = (
    "Install loopMIDI (https://www.tobias-erichsen.de/software/loopmidi.html, or "
    "`winget install TobiasErichsen.loopMIDI`), start it, add exactly two ports named "
    f"'{REQ_PORT_NAME}' and '{RES_PORT_NAME}', and keep loopMIDI running whenever FL runs; "
    "then rerun fl-mcp-install."
)


def check_midi_ports() -> InstallStep:
    """Windows: open the two loopMIDI ports the way the server will, then close them.

    WinMM has no virtual ports, so they must exist before FL and the server start;
    loopMIDI owns them and keeps them per user session while it runs."""
    from fl_mcp.core.carriers.wire import ControlUnavailable, windows_midi_control

    try:
        windows_midi_control().close()
    except ControlUnavailable as exc:
        return _warn(f"{exc} {LOOPMIDI_FIX}")
    return InstallStep(status="OK", detail=f"loopMIDI ports '{REQ_PORT_NAME}' and '{RES_PORT_NAME}' open")


def install_all(bridge_library: Path | None = None, *, with_bridge: bool = True) -> dict[str, InstallStep]:
    """Full FL-side install, each step independent: one failing never skips the rest."""
    results: dict[str, InstallStep] = {}
    for name, step in (
        ("agent_project", install_agent_project),
        ("note_script", install_note_script),
        ("controller_script", install_controller),
    ):
        try:
            results[name] = step()
        except OSError as exc:  # a missing FL folder, or one this user cannot write
            results[name] = InstallStep(status="FAIL", detail=str(exc))
    results["midi_assignment"] = configure_midi()
    if sys.platform == "win32":
        results["midi_ports"] = check_midi_ports()
    if bridge_library is not None:
        results["bridge"] = bridge.install_built(bridge_library)
    elif with_bridge:
        results["bridge"] = bridge.install_release()
    return results


NEXT_STEPS = (
    "Next: open FLMCP Agent.flp in FL Studio (File > Open, FL's Projects folder), "
    "then start your MCP client (it launches the server). "
    "Check the connection any time with fl-mcp-doctor."
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Install what FL Studio needs for FL MCP, for this user: the scripts, the MIDI "
        "assignment, the agent project, and the Bridge plugin the engine requires. "
        "Safe to rerun; close FL Studio first."
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object of step results")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--bridge", type=Path, metavar="PATH",
                        help=f"install this Bridge library ({bridge_binary_name()}) instead of the prebuilt release")
    source.add_argument("--no-bridge", action="store_true",
                        help="for hosts that install their own Bridge build (CI, provisioned boxes): skip the "
                        "Bridge step. The engine still needs a Bridge in FL; a normal install never uses this")
    args = parser.parse_args(argv)
    results = install_all(args.bridge, with_bridge=not args.no_bridge)
    failed = any(step.status == "FAIL" for step in results.values())
    waiting = [name for name, step in results.items() if step.status == "WARN"]
    if args.json:
        print(json.dumps({name: step.model_dump() for name, step in results.items()}))
    else:
        for name, step in results.items():
            print(f"  [{step.status}] {name}: {step.detail}")
        if failed:
            footer = "Some steps failed; fix them and rerun fl-mcp-install."
        elif waiting:
            # A missing Bridge means no render: "Next: open FL" read as done (2026-10-26).
            footer = f"Not finished: {', '.join(waiting)} needs you (see WARN above); fix it and rerun fl-mcp-install."
        else:
            footer = NEXT_STEPS
        print("\n" + footer)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
