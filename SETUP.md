# Setup

The [README quick start](README.md#quick-start) installs the released package with uv in three steps: install uv (and loopMIDI on Windows), run `fl-mcp-install` once, and add the server to your MCP client.
This page covers what that does on each OS, the optional Bridge plugin, and working from a source checkout.

**Requirements:** FL Studio 2026 on macOS or Windows, and [uv](https://docs.astral.sh/uv/).
The file-based features (compose, arrange, analyze `.flp` files) need no FL running; the live features need FL open.

## What `fl-mcp-install` does

Run it with FL Studio closed (start FL once first if you never have, so its settings folder exists):

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
5. `bridge`: reports whether the Bridge plugin is installed, or installs one with `--bridge PATH` (below).

`--json` prints the same results as one JSON object.

Open FL Studio first, then start your MCP client, which starts the server.
Check everything with `fl-mcp-doctor` while FL is open.

## Windows

Windows has no virtual MIDI ports, so [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) provides the two ports the controller talks over:

1. Install loopMIDI (`winget install TobiasErichsen.loopMIDI`, or the installer from its site) and start it.
2. Add exactly two ports named `FLMCP Req` and `FLMCP Res`.
3. Keep loopMIDI running whenever FL runs, then run `fl-mcp-install`; its `midi_ports` step confirms both ports open.

Production boxes are provisioned unattended by `scripts/windows/provision.ps1` and `session-install.ps1`, which run the same `fl-mcp-install` and pin the Bridge DLL and loopMIDI by sha256.

## The Bridge plugin

The Bridge is a native FL effect that performs live host operations: targeted live note writes and `render` (it records the mix while FL plays).
Without it, notes still land through the project file, with FL reloading the project per write, and `render` is unavailable.
It is built from source because it compiles against Image-Line's FL Studio plugin SDK, which carries no redistribution grant, so no prebuilt binary is published.

From a checkout of this repository, with CMake and a C++ toolchain:

```bash
native/scripts/fetch_sdk.sh
cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release
cmake --build native/build --target bridge
```

Then install it with FL closed:

```bash
uvx --python 3.12 --from fl-studio-agent fl-mcp-install --bridge "native/build/FL MCP Bridge_x64.dylib"
```

On Windows, build from an x64 Native Tools prompt for Visual Studio 2022 (`fetch_sdk.sh` runs in Git Bash), and run the install from an elevated terminal, because FL's plugin folder is under Program Files:

```powershell
cmake -S native -B native\build-windows -G "Visual Studio 17 2022" -A x64
cmake --build native\build-windows --config Release --target bridge
uvx --python 3.12 --from fl-studio-agent fl-mcp-install --bridge "native\build-windows\Release\FL MCP Bridge_x64.dll"
```

FL loads native plugins only from inside its own install (the app bundle on macOS), which is where the installer puts it; `FL_APP` points it at an FL installed somewhere unusual.
The agent project already carries the Bridge on its Master insert.
For another project: Mixer (F9), an insert slot, add effect, **FL MCP Bridge**, then save.
If it does not appear, use Options > Manage plugins > Find installed plugins.
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
