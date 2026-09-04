
This is the Windows build procedure for the FL MCP Bridge. It produces the
FL Studio installed. Do not copy the proprietary FL SDK into this repository.

The bridge's portable core (spool protocol, command ledger, note conversion,
and `FHD_AddNotesToPR` call) is built on Windows. The `wp_*` reverse-engineering
passes remain disabled: their dispatcher and Free Pascal layout evidence is
macOS-specific. Windows UDP is also disabled until a live Windows FL run
validates it; the durable file-spool carrier remains active.


On a supported Windows Server 2022 build host:

1. Install FL Studio x64 and launch it once as the build user.
2. Install **Visual Studio 2022 Build Tools**, including the MSVC x64/x86 build
   tools and a Windows 10/11 SDK. The box bootstrap normally installs this.
3. Install CMake (`choco install cmake --installargs 'ADD_CMAKE_TO_PATH=System'`).
4. Download the FL Studio plugin SDK from Image-Line's developer portal and
   extract its headers and `.cpp` files into a private directory, for example
   `C:\\FLSDK`. It must contain `fp_plugclass.h`, `fp_plugclass.cpp`,
   `fp_cplug.cpp`, and `fp_extra.cpp`.
5. Clone this repository. Keep the SDK outside the checkout, or use the
   ignored `native\\sdk` directory.

## Build

Run these commands from an **x64 Native Tools Command Prompt for VS 2022**:

```powershell
cmake -S native -B native\\build-windows -G "Visual Studio 17 2022" -A x64 `
  -DFL_SDK_DIR=C:\\FLSDK
cmake --build native\\build-windows --config Release --target bridge
Get-Item native\\build-windows\\Release\\"FL MCP Bridge_x64.dll"
```

The last command confirms the artifact. The DLL exports only
`CreatePlugInstance` and `SetExternalAppHandle`; verify before installation:

```powershell
dumpbin /exports native\\build-windows\\Release\\"FL MCP Bridge_x64.dll"
```

## Install and validate

Copy the DLL into the FL native **Effects** plugin directory discovered from
the installed FL copy, preserving this layout:

```text
<FL install>\\Plugins\\Fruity\\Effects\\FL MCP Bridge\\
  FL MCP Bridge_x64.dll
  Plugin.nfo
```

`Plugin.nfo` contains `ps_Vendorname=FL MCP`. Restart FL, insert the effect on
any mixer track, and issue a spool `ping` before a note write. A successful
ping creates `bridge_heartbeat.txt` in the selected spool root. Set
`FLMCP_SPOOL_ROOT` in the FL process environment before launch to isolate a
session; otherwise the bridge uses `%USERPROFILE%\\Documents\\Image-Line\\FL
Studio\\Settings\\Piano roll scripts\\FLMCP` and its parent fallback.

acknowledged, and a concrete pattern/channel note write reads back from FL.
Do not enable Windows `wp_*` discovery or UDP based on a successful build.

## Follow-up: window capture

Streaming/window capture is intentionally not part of this port. Implement it
as a Windows-specific capture backend (Windows Graphics Capture or DXGI), with
explicit user consent and a live FL validation, after the bridge write path is
proven.
