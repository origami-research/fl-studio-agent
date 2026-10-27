# Setup

The [README quick start](README.md#quick-start) installs the released package with uv in three steps: install uv (and loopMIDI on Windows), run `fl-mcp-install` once, and add the server to your MCP client.
This page covers what that does on each OS, the Bridge plugin the server requires, and working from a source checkout.

**Requirements:** FL Studio 2026 on macOS or Windows, and [uv](https://docs.astral.sh/uv/).
The file-based features (compose, arrange, analyze `.flp` files) need no FL running; the live features need FL open.

## What `fl-mcp-install` does

Run it with FL Studio closed (start FL once first if you never have, so its settings folder exists); on Windows, run it in a terminal opened as administrator, because the Bridge goes into FL's folder under Program Files.
Use **Run as administrator** on your own account: the scripts and MIDI settings go into the profile of the account that runs the command, so a separate administrator account would set up its own FL instead of yours.

```bash
uvx --python 3.12 --from fl-studio-agent fl-mcp-install
```

Each step prints `OK`, `WARN` (needs you), or `FAIL` (the command then exits nonzero), and says `installed` or `already current`, so it is safe to rerun:

1. `agent_project`: creates `FLMCP Agent.flp` in FL's Projects folder from the packaged template, only if it is missing.
   It is the only project the agent modifies; your own work is never touched, and a backup is written before every change.
2. `note_script` and `controller_script`: copies FL's piano-roll script and the FLMCP controller script into FL's Settings folder.
   The server derives its expected controller version from the exact controller source this deploys, so to update, run `uvx --python 3.12 --from fl-studio-agent@latest fl-mcp-install` (`@latest` makes uvx fetch the newest release instead of reusing its cached one) and restart FL and your client; `fl-mcp-doctor` reports a stale controller.
3. `midi_assignment`: assigns the controller to the `FLMCP Req` / `FLMCP Res` ports in FL's MIDI settings, in `reg.xml` on macOS (a `reg.xml.bak` is written before any change) or in `HKCU\Software\Image-Line\FL Studio <N>` on Windows.
   It refuses while FL is running, because FL rewrites these settings when it exits.
4. `midi_ports` (Windows only): opens the two loopMIDI ports the way the server will.
   macOS needs no step here: the server opens virtual MIDI ports itself, and FL picks them up while it runs.
5. `bridge`: downloads the prebuilt Bridge plugin for this OS from the GitHub release matching the package version, checks it against that release's `SHA256SUMS`, and installs it into FL's plugin folder (below).
   `--bridge PATH` installs your own build instead.
   `--no-bridge` skips this step; it is only for hosts that install their own Bridge build (CI and the provisioned boxes below), because the server needs a Bridge in FL.

`--json` prints the same results as one JSON object.

Then open `FLMCP Agent.flp` in FL Studio (File > Open, FL's Projects folder), and only then start your MCP client, which starts the server.
The engine never opens a project itself, and only a project carrying the Bridge takes notes and renders: with FL's default song open, `fl-mcp-doctor` reports that no Bridge is running.
Check everything with `fl-mcp-doctor` while FL is open: its `status` is `ok` only when every part, the Bridge included, is connected, and `not_ready` otherwise, with a `fix` for each part that is not.

## Windows

Windows has no virtual MIDI ports, so [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) provides the two ports the controller talks over:

1. Install loopMIDI (`winget install TobiasErichsen.loopMIDI` on Windows 10 and 11, or the installer from its site; Windows Server has no winget) and start it.
2. Add exactly two ports named `FLMCP Req` and `FLMCP Res`: type each name into "New port-name" and click **+**.
3. Tick **Autostart loopMIDI** in its tray menu: loopMIDI does not start with Windows otherwise, and without it the ports are gone after a restart.
4. Keep loopMIDI running whenever FL runs, then run `fl-mcp-install`; its `midi_ports` step confirms both ports open.

Production boxes are provisioned unattended by `scripts/windows/provision.ps1` and `session-install.ps1`, which run the same `fl-mcp-install` (with `--no-bridge`: the box pins its own Bridge DLL) and pin the DLL and loopMIDI by sha256.

## The Bridge plugin

The Bridge is a native FL effect that performs live host operations: targeted live note writes and `render` (it records the mix while FL plays).
The server requires it: with no Bridge running in FL, note writes and `render` are refused with a fix (open the agent project), and `fl-mcp-doctor` reports `not_ready`.

`fl-mcp-install` installs it for you: each release carries a universal macOS library (Apple Silicon and Intel) and a Windows x64 DLL, built by the release workflow, with `SHA256SUMS` and a `BRIDGE-NOTICE.txt`.
The installer downloads the one for your OS from the release matching the installed package version and installs it only if its SHA-256 matches; `fl-mcp-doctor` reports what is installed (`bridge.source`, `bridge.sha256`).

FL loads native plugins only from inside its own install (the app bundle on macOS, Program Files on Windows), which is where the installer puts it; `FL_APP` points it at an FL installed somewhere unusual.
The agent project already carries the Bridge on its Master insert.
For another project: Mixer (F9), an insert slot, add effect, **FL MCP Bridge**, then save.
If it does not appear, use Options > Manage plugins > Find installed plugins.

The libraries are not code-signed:

- macOS: FL ships with library validation disabled, so it loads the (ad-hoc signed) library.
  A file downloaded by the installer carries no quarantine flag, and the installer clears `com.apple.quarantine` anyway, so Gatekeeper does not prompt.
  If you copy the library in by hand from a browser download, run `xattr -d com.apple.quarantine` on it.
- Windows: the DLL is loaded by FL, not run by you, and a file the installer downloads carries no "downloaded from the internet" mark, so SmartScreen does not prompt.
  Antivirus software may still inspect a new unsigned DLL, and Windows 11 Smart App Control, when it is on, can block unsigned code regardless of where it came from.
- macOS may ask to let your terminal modify apps (System Settings > Privacy & Security > App Management), because the Bridge goes inside FL Studio.app; the installer says so if the write is refused.

The Bridge is built with Image-Line's FL Studio plugin SDK headers; FL Studio is a trademark of Image-Line, and this project is not affiliated with Image-Line (see [native/bridge/NOTICE.txt](native/bridge/NOTICE.txt)).

### Build the Bridge yourself

For contributors, or an FL build the release does not support.
From a checkout of this repository, with CMake and a C++ toolchain:

```bash
native/scripts/fetch_sdk.sh
cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release
cmake --build native/build --target bridge
uvx --python 3.12 --from fl-studio-agent fl-mcp-install --bridge "native/build/FL MCP Bridge_x64.dylib"
```

On Windows, build from an x64 Native Tools prompt for Visual Studio 2022 (`fetch_sdk.sh` runs in Git Bash), and install from an administrator terminal:

```powershell
cmake -S native -B native\build-windows -G "Visual Studio 17 2022" -A x64
cmake --build native\build-windows --config Release --target bridge
uvx --python 3.12 --from fl-studio-agent fl-mcp-install --bridge "native\build-windows\Release\FL MCP Bridge_x64.dll"
```

The Windows build details are in [native/bridge/WINDOWS.md](native/bridge/WINDOWS.md).

## From a source checkout

```bash
cd mcp-server
uv sync
uv run fl-mcp-install
claude mcp add fl-studio -- "$(pwd)/.venv/bin/python" -m fl_mcp
```

The registration needs the absolute path; restart the client session afterwards.
`native/scripts/install_bridge.sh` installs a Bridge built in `native/build` the same way.

## What you can ask for

Compose (notes into any pattern), shape the sound (live synth parameters), read and understand the project (key, arrangement, automation), set tempo, check which channels sound, and render to WAV.
Full list: [docs/24-coverage-map.md](docs/24-coverage-map.md).

## Honest notes

- It drives **your** FL on **your** machine; live operations take over screen focus and restart FL around file writes.
- It runs local code that opens FL: a local setup, not a hosted product.
