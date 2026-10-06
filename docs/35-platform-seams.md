# 35 - Platform seams inventory and Windows port design

## Decision

Windows runner images need a persistent virtual audio output device, selected
in FL Studio's audio settings. RDP's redirected device appears with the judge
session and vanishes when it ends. The Bridge heartbeat must show
`render_rate > 0` before a run starts; zero means FL's audio engine is not
running, commonly because no output device is available.

Production runs FL Studio on Windows through RDP streaming, while macOS remains the development rig.


The engine stays one Python codebase with an OS-neutral core and one runtime-selected `Platform` implementation at its outer boundary.

The native Bridge stays one CMake source tree and emits a macOS dylib or Windows DLL from platform-specific source leaves.

## Windows build

The `native-windows` GitHub Actions job builds the DLL on `windows-latest`.
It fetches the proprietary SDK into ignored `native/sdk` with the same
`native/scripts/fetch_sdk.sh` used locally, builds with the Visual Studio 2022
x64 generator, runs the spool/mock-host tests and the Windows FPC-layout ABI
check, then uploads `FL MCP Bridge_x64.dll` as an artifact. The current mirror
is public, so the job needs no repository secret. Never commit `native/sdk`.

From a PowerShell prompt on Windows, run:

```powershell
bash native/scripts/fetch_sdk.sh
cmake -S native -B native/build-windows -G "Visual Studio 17 2022" -A x64
cmake --build native/build-windows --config Release --target bridge bridge_core_test bridge_abi_test bridge_events_test bridge_gui_test
ctest --test-dir native/build-windows -C Release --output-on-failure
Get-Item 'native/build-windows/Release/FL MCP Bridge_x64.dll'
```

`bridge_abi_check` uses the FPC-shaped mock on Windows. The Mach-O vtable
reader remains a macOS check because it reads Mach-O symbol layout.

Windows builds `bridge_discover_windows.cpp`, a documented discovery stub.
`wp_getters`, `wp_timing`, `wp_target`, and `wp_sweep` are unavailable and
make no dispatcher calls. The stub provides an empty getter table and the
portable target-key helper so the shared mock-host test links; the mmap-backed
capture and the measured Free Pascal/Mach-O discovery path remain macOS-only.

No feature code may select an OS, construct a platform socket, invoke an FL executable, or enumerate processes after this boundary exists.

The five Python seams are MIDI wire creation, broker endpoint, render command, FL discovery, and process sweep. Native GUI-thread marshalling is the corresponding bridge seam.

The spool protocol, UDP protocol, FLP codec, transport routing, controller protocol, store, schemas, and session behavior remain OS-neutral.

`sys.platform` is read once by the platform factory, not scattered through callers.

The inventory below is the ground truth for this refactor rather than a claim that the Darwin assumption appears in one module.

## Inventory method and scope

This inventory searched the complete tracked tree for Darwin and macOS labels, CoreMIDI and `rtmidi`, virtual ports, Unix sockets and `AF_UNIX`, application bundles and `/Applications`, render flags and `OsxFL`, `pgrep` and `pkill`, dylib and DLL installation, CMake platform conditionals, Apple frameworks, POSIX headers, and ScreenCaptureKit aliases.

`ps eww` has no tracked occurrence.

ScreenCaptureKit, `SCK`, `CGWindow`, and `screencapture` have no tracked production or native-source occurrence.

The only capture-related design statement is `native/bridge/WINDOWS.md:66-67`, which correctly defers Windows Graphics Capture or DXGI until the write path is proven.

References to `unix_time` and `unix_ms` are protocol timestamps, not operating-system dependencies, and are excluded from the table.

Lockfile wheel names are packaging metadata rather than platform behavior, and are excluded from the table.

| File and line | Current touchpoint | Seam and required disposition |
| --- | --- | --- |
| `mcp-server/src/fl_mcp/core/carriers/wire.py:68-99` | Imports `rtmidi` and calls `open_virtual_port` for the fixed request and response ports. | MIDI wire factory. |
| `mcp-server/src/fl_mcp/session/facade.py:68-97` | `create_session` directly chooses `MidiControl` or `BrokerControl` and builds the rest of the session. | Selection point that must obtain its control carrier from `Platform`. |
| `mcp-server/src/fl_mcp/core/carriers/control_broker.py:10-18,42-80,115-123` | Defines, binds, probes, unlinks, and serves an `AF_UNIX` broker socket. | Replace with the shared loopback-TCP endpoint. |
| `mcp-server/src/fl_mcp/core/carriers/broker_client.py:9-11,24,35-37,103-130` | Imports the Unix default socket path and connects through `AF_UNIX` after broker auto-start. | Replace with the shared loopback-TCP endpoint. |
| `mcp-server/tests/test_control_broker.py:1-2,45-61` | Tests the real Unix-socket broker and works around the macOS pathname limit. | Replace with endpoint-parametrized loopback-TCP coverage. |
| `mcp-server/tests/test_carrier_wire.py:2,34-55,71-99,139-152` | Fakes `rtmidi`, virtual-port opening, missing backend, and a CoreMIDI error. | MIDI wire factory test. |
| `mcp-server/src/fl_mcp/session/state.py:95-97` | Reports that an unavailable virtual MIDI wire needs `python-rtmidi`. | MIDI wire factory diagnostic. |
| `mcp-server/src/fl_mcp/fl/paths.py:32-50` | Resolves `FL_APP` or `/Applications/FL Studio*.app`, with an existing Windows `Program Files/.../FL64.exe` branch. | FL discovery. |
| `mcp-server/src/fl_mcp/fl/paths.py:70-80` | Adds a Windows OneDrive Image-Line settings candidate. | FL discovery. |
| `mcp-server/src/fl_mcp/fl/paths.py:92-103` | Selects app-data roots for macOS, Windows, and other POSIX systems. | FL discovery. |
| `mcp-server/src/fl_mcp/fl/process.py:1-155` | Reads a macOS `Info.plist`, falls back to `OsxFL`, uses `pgrep` plus signals, and has Windows `tasklist`, `taskkill`, and `Popen` branches. | Process sweep and FL discovery. |
| `mcp-server/src/fl_mcp/session/lifecycle.py:9,28-35` | Calls global running and kill helpers during controlled reload. | Process sweep consumer that must remain carrier-blind. |
| `mcp-server/src/fl_mcp/session/link.py:14,118` | Uses the running-process predicate for control recovery. | Process sweep consumer. |
| `mcp-server/src/fl_mcp/session/notes.py:36,198` | Uses the running-process predicate before a file-lane reload. | Process sweep consumer. |
| `mcp-server/src/fl_mcp/session/state.py:11,131` | Uses the running-process predicate to classify session state. | Process sweep consumer. |
| `mcp-server/src/fl_mcp/dev_session.py:24,107,155` | Launches a marked FL process and kills that confirmed PID. | Process sweep and FL discovery consumer. |
| `mcp-server/src/fl_mcp/session/controls.py:20-21,75-83` | Constructs `<app>/Contents/MacOS/<executable> -R <project>` for a blocking renderer. | Render command. |
| `mcp-server/tests/test_session_controls.py:48-66` | Asserts the macOS app bundle executable and `-R` render form. | Render command test. |
| `mcp-server/tests/test_fl_windows.py:1-62` | Covers Windows path discovery, exact image names, `tasklist`, `taskkill`, and nonblocking launch. | Existing FL discovery and process-sweep tests. |
| `mcp-server/tests/e2e/test_matrix_render.py:4-5,43-64,120-122` | Requires live FL and directly discovers the macOS bundle executable before `-R` rendering. | Render command test and live macOS-only evidence. |
| `mcp-server/tests/e2e/fl_host.py:27-78` | Uses AppleScript window queries and `open -a` to inspect and launch FL. | macOS-only live-host harness outside the five production seams. |
| `mcp-server/tests/e2e/matrix.py:51-53,202-205` | Reads FL data from `Contents/Resources/FL/Data`. | FL discovery test fixture. |
| `mcp-server/tests/test_plugin_state_corpus.py:16-32` and `mcp-server/tests/test_flp_corpus.py:38-52` | Resolve installed FL data through the macOS bundle layout. | FL discovery corpus fixture. |
| `mcp-server/tests/test_scrape_rtti.py:3,25-28,288-290` | Hardcodes the installed Mach-O dylib under `/Applications`. | macOS ABI-scrape verification that must be explicitly macOS-only. |
| `mcp-server/src/fl_mcp/discover.py:184-187` and `mcp-server/src/fl_mcp/scrape_rtti.py:418-420` | Default reverse-engineering input is `Contents/Libs/FLEngine_x64.dylib`. | macOS ABI discovery, deliberately unsupported on Windows. |
| `mcp-server/src/fl_mcp/profile.py:486-503` | Extracts installed FL version and build from macOS `Info.plist`. | FL discovery. |
| `mcp-server/src/fl_mcp/make_agent_project.py:22-47` | Locates templates at `Contents/Resources/FL/Data/Templates`. | FL discovery. |
| `mcp-server/src/fl_mcp/core/provenance.py:72` | Records `OsxFL -R` as the evidence source for full-mix render. | Render provenance that must become platform-specific evidence. |
| `mcp-server/src/fl_mcp/fl_scripts/device_FLMCP.py:278` | Carries a hardcoded macOS MCP tool path for an in-FL diagnostic. | FL discovery and controller diagnostics. |
| `harness/flctl.py:39-77` | Detects `/Applications` and uses broad `pgrep -f` process handling. | Historical macOS harness process sweep and discovery. |
| `harness/trigger.py:47-82` | Uses Apple events and `pgrep -f` to trigger FL. | macOS-only harness outside the engine. |
| `harness/enable_autonomy.py:29-49` | Opens Apple privacy preference URLs and requests Apple-event permission. | macOS-only harness outside the engine. |
| `harness/bootstrap.py:33-43` and `harness/setup_fl.py:40-117` | Assume bundle resource paths, dylib names, and macOS library-validation behavior. | Bridge installation and development harness. |
| `tools/fl_harness.py:20,31,111,188` | Requires macOS accessibility, names `OsxFL`, uses `pgrep`, and writes `/tmp` evidence. | macOS-only UI harness. |
| `experiments/_guard.py:14` | Identifies FL as `OsxFL`. | Historical macOS experiment helper. |
| `experiments/e4_render_cli.py:1-71` | Proves macOS `-R`, finds `Contents/MacOS`, and uses broad `pkill`. | Render evidence and superseded process handling. |
| `experiments/README.md:17` | Records the `OsxFL -R` macOS render result. | Render evidence. |
| `install.sh:2,67-69` | Rejects non-Darwin installation and says Windows virtual ports are not wired. | Installer, MIDI wire factory, and open-core onboarding. |
| `native/CMakeLists.txt:30-46` | Sets universal macOS architectures and selects macOS or Windows bridge leaves. | Native bridge build target. |
| `native/CMakeLists.txt:92-108` | Builds one bridge target and assigns `.dylib` or `.dll`. | Native bridge build target. |
| `native/CMakeLists.txt:146-160` | Links the GUI test to CoreFoundation only on Apple. | Native GUI-thread seam. |
| `native/CMakeLists.txt:187-198` | Runs Mach-O vtable inspection only on Apple. | Native macOS ABI verification. |
| `native/bridge/src/bridge_gui.cpp:6-45` | Imports libdispatch and pthread, then uses the Cocoa main queue on Apple. | Native GUI-thread marshalling seam that needs a Windows implementation or an explicit unsupported operation. |
| `native/bridge/tests/bridge_gui_test.cpp:18-64` | Imports CoreFoundation and pthread to drain the macOS main queue. | Native GUI-thread test. |
| `native/bridge/src/bridge_discover.cpp:3-4` | Imports `mmap` and `unistd` for macOS reverse-engineering passes. | Native macOS ABI discovery. |
| `native/bridge/src/bridge_discover_windows.cpp:1-15` | Fails discovery operations closed on Windows rather than guessing a dispatcher ABI. | Native Windows safety seam. |
| `native/bridge/src/bridge_udp_windows.cpp:1-31` | Fails UDP startup closed so the Windows bridge uses the spool. | Native Windows safety seam. |
| `native/bridge/src/bridge_udp.cpp:7` and `native/bridge/src/bridge*.cpp:43-46` | Use POSIX `unistd` and `sys/stat` APIs in macOS bridge code. | Native filesystem and socket portability work. |
| `native/bridge/tests/bridge_core_test.cpp:11-12`, `bridge_events_test.cpp:7-8`, and `bridge_udp_test.cpp:11-12` | Import POSIX filesystem headers and use `/tmp` test fixtures. | Native test portability work. |
| `native/abi/fl_pascal.cpp:5` and `native/abi/fl_host_abi.h:1-69` | Implement and document the measured macOS Free Pascal and Mach-O host ABI. | Native macOS ABI discovery, not portable by assumption. |
| `native/scripts/install_bridge.sh:8-81` | Finds `/Applications` bundles, installs a dylib, and clears quarantine with `xattr`. | macOS bridge installation. |
| `native/scripts/check_vtable.sh:2-76` | Uses `nm` and `lipo` to inspect a Mach-O dylib. | macOS bridge ABI verification. |
| `native/bridge/WINDOWS.md:1-67` | Documents a DLL build, private SDK, spool-first validation, and deferred capture. | Existing Windows bridge rollout record. |
| `.github/workflows/ci.yml:1-21` | Runs only the Python fake-FL suite on Ubuntu and has no native build matrix. | CI gap. |
| `README.md:10,47-55`, `SETUP.md:5`, and `docs/00-overview.md:92` | Describe macOS support and current Windows limits. | Product and onboarding documentation. |
| `docs/03-protocols.md:15-27,99-101` | States CoreMIDI virtual-port creation and a Windows loopback-driver or MIDI Services alternative. | MIDI wire factory and spool portability. |
| `docs/04-native-plugin.md:33-40,130`, `docs/12-fl2026-native-surface.md`, and `docs/25-fl-capability-catalog.md:39,135,202,281` | Record macOS plugin paths, signing, ABI facts, CoreMIDI assumption, and render evidence. | Native ABI, MIDI, bridge install, and render evidence. |
| `docs/14-spool-first-verdict.md`, `docs/21-v2-rewrite-spec.md`, `docs/24-coverage-map.md`, `docs/26-master-plan.md`, and `docs/34-per-session-control.md` | Specify macOS spool, bundle, live-host, and Unix-broker behavior. | Historical architecture records that need link-level follow-up, not compatibility code. |
| `docs/research/save-render-api-2026-09-01.md:15,95-109` | Distinguishes the macOS `-R` evidence from the reported Windows `/R` and `/Ewav` form. | Render command evidence. |
| `docs/research/r4-streamline-and-audit.md:269-275` and `MERGE_NOTES.md:96,114,133,147` | Preserve the refuted broad-`pkill` and duplicate-process-probe history plus the rtmidi broker decision. | Process-sweep and MIDI design history. |
| `docs/research/insights-2026-08-31.md:33-35`, `docs/research/r3-tier2-production.md:67`, `RUNBOOK_E2E.md`, and `PR_LOG_HANDOFF.md` | Refer to macOS render evidence, streaming, and dylib-based ABI discovery. | Evidence and operations records. |
| `native/probe/PROBE_FINDINGS.md`, `native/probe/PROBE_VERDICT_2026-08-30.md`, `native/bridge/BRIDGE_FINDINGS.md`, `RUNBOOK_BRIDGE_UDP.md`, `RUNBOOK_BRIDGE_V2.md`, `RUNBOOK_BRIDGE_V3.md`, and `RUNBOOK_DISCOVERY.md` | Contain macOS-only binary, Cocoa, `lipo`, `otool`, `/tmp`, and kill-runbook evidence. | Historical native ABI evidence, intentionally retained and labeled macOS-specific. |

## Boundary design

Add `fl_mcp.platform` as the only runtime module that imports `sys.platform` and owns concrete operating-system adapters. Step 1 creates this package only. Nothing calls it yet, so it changes no session behavior.

Keep `fl_mcp.fl.paths` and `fl_mcp.fl.process` as compatibility-free implementation details until their callers move behind the boundary. Use a `Protocol` so callers can use a small typed fake without patching module-global platform state.

```python
class Platform(Protocol):
    def control_factory(self) -> Callable[[], ControlCarrier]: ...
    def broker_endpoint(self) -> BrokerEndpoint: ...
    def render_command(self, project: Path) -> Sequence[str]: ...
    def fl_install(self) -> FLInstall: ...
    def sweep_fl(self) -> ProcessSweep: ...
```

`BrokerEndpoint` is one immutable TCP-loopback value object for every platform: `host="127.0.0.1"`, ephemeral `port`, 128-bit `token`, and `port_file` for discovery. It owns bind, connect, stale-endpoint cleanup, and its identifier, so neither broker side imports `AF_UNIX`. This is the existing proven pattern in `core/carriers/udp.py` and `bridge_udp.json`: one code path, no 104-character socket-path limit, and no Node pipe semantics to preserve because the broker is Python-only. Delete the AF_UNIX endpoint rather than retaining it beside a named pipe. Named pipes are not a target.

`FLInstall` returns the authoritative executable, settings directories, templates directory, data directory, native plugin directory, engine image when supported, and app-data root. `ProcessSweep` lists exact FL process identities, kills either all listed processes or one confirmed PID, reports whether FL is running, and launches a project. `Platform.render_command(project)` returns complete immutable argv and never runs it.

`MacPlatform` delegates to the existing CoreMIDI factory, app-bundle resolver, plist executable lookup, exact `pgrep -x`, exact-PID signals, direct executable spawning, and `-R` renderer. `WindowsPlatform` opens provisioned loopMIDI names, uses the same authenticated loopback-TCP broker endpoint, resolves `FL64.exe`, scopes process teardown to its launched project marker, and emits the Windows CLI render argv. Unsupported platforms raise a typed, actionable error on use.

Later, `create_session` receives an optional `platform: Platform | None = None`, resolves `platform_for(sys.platform)` once when absent, and injects the endpoint and control factory. `FLSession` stores no OS name. `FLMCP_SPOOL_ROOT` remains an OS-neutral routing decision.

### MIDI wire decision

The CoreMIDI implementation remains valid only on macOS because it creates virtual ports in-process.

`python-rtmidi` ships Windows support but does not provide a Windows virtual-port facility equivalent to CoreMIDI, so `MidiOut.open_virtual_port` and `MidiIn.open_virtual_port` cannot be the Windows implementation.


The Windows factory reads names from an explicit `FLMCP_MIDI_REQUEST_PORT` and `FLMCP_MIDI_RESPONSE_PORT` configuration after provisioning, opens existing ports by name, and fails with a typed setup diagnostic that names the missing dependency and ports.

The installer provisions both ports. The doctor verifies both ports and one request-response round trip across a noninteractive reboot before it claims MIDI control is usable.

This keeps MIDI semantics, message framing, the one-machine broker, and PID addressing unchanged while honestly isolating the Windows driver dependency.

### Broker endpoint decision

The broker keeps its one-owner bind invariant and newline-JSON protocol over authenticated loopback TCP on `127.0.0.1`. It binds an ephemeral port, writes a port file containing that port and a 128-bit token, and authenticates every connection with that token. This canonical endpoint is identical on macOS and Windows.

The former AF_UNIX and named-pipe designs are deleted. A single transport prevents platform drift and avoids Unix socket pathname limits. The request-per-connection protocol remains unchanged.

### Render command decision

The render seam returns platform-specific argv for a saved project copy and produces an artifact that the existing audio analysis reads back.

macOS retains the proven `<bundle>/Contents/MacOS/<CFBundleExecutable> -R <project>` form.

[Image-Line's export manual](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/fformats_save_export.htm) documents the Windows form `FL.exe /R /E<ext> /F<folder> [project.flp]`, with `/O<folder>` also available. The two forum-reported failure modes are that FL can [remain open after CLI render](https://forum.image-line.com/viewtopic.php?t=287758) and that [missing assets can raise a blocking popup](https://forum.image-line.com/viewtopic.php?t=177807). Windows stays `VERIFY-ON-WINDOWS`: the render step needs a timeout, output-completion check, and controlled kill. It cannot be enabled until a real saved copy renders and audio analysis reads the discovered artifact.

### FL discovery and process-sweep decision

`FLInstall` owns all FL-relative paths so template, corpus, profile, and bridge-install callers cannot recreate bundle layout assumptions.

The existing Windows `FL64.exe` and `tasklist` implementation is useful evidence but must become `WindowsPlatform` behavior rather than more `sys.platform` branches in `fl.paths` and `fl.process`.

The existing exact-image-name plus owned-PID invariant stays mandatory because broad `pgrep`, `pkill`, and command-line matching were explicitly refuted. On Windows, the session project path in `Win32_Process.CommandLine` is the ownership marker that replaces the unreadable child environment marker. It is never the sole filter: first match the exact FL image name, then confirm an owned PID and its project path, then kill only that PID with `taskkill /PID <pid> /F /T`.

### Native bridge and CMake decision

Keep one `bridge` target named `FL MCP Bridge_x64` with no prefix, producing `.dylib` on Apple and `.dll` on Windows.

Keep the portable spool executor, command ledger, note conversion, JSON protocol, and SDK headers common to both artifacts.

Keep macOS Free Pascal dispatcher discovery, Mach-O scraper evidence, and live UDP disabled on Windows until independently proven there, as `bridge_discover_windows.cpp` and `bridge_udp_windows.cpp` already do. Windows is spool-first.

Replace direct POSIX includes in portable bridge and test sources with a small filesystem and temporary-directory adapter, so the core and its unit tests compile under MSVC.

Give `bridge_gui` a Windows implementation only after its FL callback threading contract is measured, or compile a clearly unsupported synchronous path that cannot claim GUI-thread marshalling works.

Make every test target use the selected `BRIDGE_PLATFORM_SOURCES`, avoid Clang-only sanitizer flags under MSVC, and retain Apple-only CoreFoundation and Mach-O ABI tests behind `if(APPLE)`.

Add a GitHub Actions native-build matrix with `macos-14` and `windows-2022`, CMake configure, Release bridge build, portable native tests, and DLL export inspection on Windows.

The matrix does not claim live-FL validation because the proprietary SDK and installed FL are not public CI inputs.



## Native effort table

| Surface | Work | Evidence gate |
| --- | --- | --- |
| `native/bridge/src/bridge_gui.cpp` | Measure FL's Windows callback-thread contract, then implement marshalling or fail closed synchronously. | Real FL GUI-thread proof on Windows. |
| Portable bridge core and tests | Replace POSIX filesystem, temporary-directory, and sanitizer assumptions. | MSVC build and portable CTest. |

## Test policy

Pure protocol, FLP, store, transport, session, and fake-control tests run on every CI operating system.

Tests for a concrete platform implementation instantiate that implementation or pass it to `create_session`, rather than monkeypatching `sys.platform` in unrelated modules.

Use `pytest.mark.platform("darwin")` and `pytest.mark.platform("win32")` for tests whose assertion is an OS contract, with a collection hook that skips them when the selected platform differs.

Use `pytest.mark.live_fl` together with `FL_E2E=1` for tests that need a real installed FL, and require both the platform mark and the environment opt-in.

Mac-only Mach-O scraping, AppleScript host inspection, CoreMIDI virtual-port behavior, and macOS `-R` rendering are marked `platform("darwin")`.


The existing fake `rtmidi` tests remain cross-platform because they verify wire framing and failure translation rather than CoreMIDI itself.

The existing Windows path and process tests become direct `WindowsPlatform` unit tests and run on every host through subprocess fakes.

## Mechanical PR sequence

These steps start only after the currently parked engine PRs merge, because this document deliberately does not touch their files or create a competing seam.



1. Add typed platform value objects, the `Platform` protocol, `platform_for`, and fake-platform tests without changing session behavior.

   Gate: the existing macOS Python suite excluding live E2E stays green.

2. Move app-data, FL install discovery, templates, profile metadata, and engine-image lookup behind `FLInstall`, then delete direct bundle-path construction from callers.

   Gate: the existing macOS suite and corpus skips stay green.

   VERIFY-ON-WINDOWS: `FL_APP` override, default `FL64.exe` resolution, settings discovery, and template location on a real installed FL.

3. Move exact process enumeration, controlled launch, and exact PID termination behind `ProcessSweep`, then delete direct `pgrep`, signal, `tasklist`, and `taskkill` callers outside the implementations.

   Gate: existing macOS process and reload tests stay green.

   VERIFY-ON-WINDOWS: process enumeration never matches a non-FL process, launch carries the session environment, and one-owned-PID teardown works.

4. Move the saved-copy renderer behind `render_command`, convert the matrix render test to consume it, and mark the current executable assertion macOS-only.

   Gate: the existing macOS live render regression still renders and its WAV analysis reads back.

   VERIFY-ON-WINDOWS: determine the accepted `/R` and export argv, prove a saved copy yields the expected WAV, and run analysis on that WAV.

5. Introduce the shared loopback-TCP `BrokerEndpoint`, replace the existing AF_UNIX broker, and make `create_session` inject the endpoint and control factory.

   Gate: loopback broker ownership, stale cleanup, token rejection, multi-session PID routing, and all existing session-link tests stay green on macOS.

   VERIFY-ON-WINDOWS: loopback-only bind, port-file discovery, token rejection, first-owner behavior, stale-owner recovery, and concurrent broker-client routing.

6. Split `MidiControl` into OS-neutral framing plus factory-owned opening, retain CoreMIDI creation on macOS, and add the configured Windows existing-port factory and doctor contract.

   Gate: all fake wire tests, broker tests, and direct macOS control behavior stay green.

   VERIFY-ON-WINDOWS: provision loopMIDI endpoints, open both ports, prove hello and PID-addressed round trips across a noninteractive reboot, and confirm failure diagnostics when unprovisioned.

7. Make the CMake bridge core and portable tests compile with both Clang and MSVC, select platform source leaves for every target, and remove unguarded POSIX and sanitizer assumptions.

   Gate: macOS CMake build, CTest, and Mach-O ABI check stay green.

   VERIFY-ON-WINDOWS: configure with VS 2022 x64, build Release DLL, run portable CTest targets, and inspect exports.

8. Add the macOS and Windows native CI build matrix, retain Ubuntu for the fast Python fake suite, and publish build artifacts only when repository policy permits.

   Gate: pull-request CI passes on all three operating systems without claiming live-FL coverage.


9. Update installation, setup, protocol, capability, and operational documents only after each corresponding boundary is live, then remove outdated macOS-first claims and historical harnesses that duplicate the canonical implementation.

   Gate: documentation links and the complete default test suite stay green.

   VERIFY-ON-WINDOWS: a clean Windows operator follows the documented provisioning, install, doctor, bridge, render, and RDP validation path without developer-only assumptions.

## Non-goals and acceptance rule

This plan does not port the measured macOS Free Pascal dispatcher ABI to Windows, enable Windows UDP, implement Windows capture, or imply that a successful DLL compilation proves live FL compatibility.

The first Windows production milestone is the spool-backed Bridge with verified DLL load, heartbeat, ping, targeted note write, FL readback, saved-copy render, and analyzed audio artifact.

A feature is portable only when its core has no platform import and its platform-specific boundary has a concrete test or recorded `VERIFY-ON-WINDOWS` evidence.

## Windows box install (2026-09-24)

This replaces the earlier repository-checkout runbook (`provision.ps1 -RepoRoot` plus `accept.ps1`).
It is written before a live box run.
Everything marked VERIFY-ON-WINDOWS below is an assumption until a box proves it.

### What a box needs

A box needs exactly three artifacts, each pinned by sha256:

- the `fl-studio-agent` wheel from `scripts/build-wheel.sh`, stamped `<version>+g<shortsha>`;
- its hash-locked runtime requirements, written beside the wheel by the same script from `uv.lock`;
- the Bridge DLL, the `FL-MCP-Bridge-Windows-x64` artifact of the `native-windows` workflow.

The wheel carries every runtime file: the controller script and its wire codec and the piano-roll script (`fl_mcp/fl_scripts/`), the session template, and the profiles.
The runtime closure has no git or direct references; `flpkit==0.8.1` comes from PyPI.
The repo-root `harness` package is not part of the runtime.

The engine venv uses CPython 3.12, not the box's 3.13.
`python-rtmidi` 1.5.8, the latest release, ships no cp313 Windows wheel, and a box has no compiler to build one.
`scripts/build-wheel.sh` fails if the Windows closure stops resolving to binary wheels for 3.12.

The MIDI carrier is loopMIDI.
RtMidi's WinMM backend lists every port as `<name> <index>`, so the engine matches `FLMCP Req 0` as `FLMCP Req`, and refuses a missing or duplicated port.
loopMIDI ports belong to the user session and exist only while loopMIDI runs there.
loopMIDI has no command-line port creation.

### Install interface

Artifact arguments take a presigned URL or a local path.

```powershell
provision.ps1 -WheelUrl <url> -WheelName fl_studio_agent-0.1.0+g36ceb61-py3-none-any.whl -WheelSha256 <sha> `
  -RequirementsUrl <url> -RequirementsSha256 <sha> -BridgeDllUrl <url> -BridgeDllSha256 <sha> `
  -FLUser <console user> [-FLLicense trial|licensed] [-LoopMidiPortsRegUrl <url> -LoopMidiPortsRegSha256 <sha>]
```

The SYSTEM part fetches and verifies the artifacts, installs Python 3.12.10 to `C:\Program Files\Python312` if missing (pinned installer, no PATH change), and installs the locked closure and then the wheel into `C:\FLMCP\venv`.
It checks that the installed version equals the wheel name's stamp, sets the machine variable `FLMCP_FL_LICENSE`, and copies the DLL into `<FL>\Plugins\Fruity\Effects\FL MCP Bridge\`, stopping FL only when the DLL changes.
It installs loopMIDI 1.0.16.27 from the zip that winget pins, when missing.

It then runs `scripts/windows/session-install.ps1` as a one-shot interactive scheduled task in the FL user's console session.
That part runs `fl-mcp-install --json` (controller, piano-roll script, and the MIDI assignment, which FL for Windows keeps in `HKCU\Software\Image-Line\FL Studio <N>\Devices`, not in a reg.xml), and resets `Documents\Image-Line\FL Studio\Projects\FLMCP Agent.flp` from the packaged template.
It imports the optional captured loopMIDI registry export into the user's hive, restarts loopMIDI in the session, and opens both ports with the engine's own port rule.
Finally it starts FL on the agent project and polls `fl-mcp-doctor` until every plane is connected or the boot timeout passes.

The result is `C:\FLMCP\install-result.json`, and stdout ends with `FLMCP_INSTALL_OK` or `FLMCP_INSTALL_FAILED`:

```json
{
  "schema": "flmcp-box-install/1",
  "ok": true,
  "finished_at": "2026-09-24T00:00:00.0000000Z",
  "fl_user": "<console user>",
  "fl_license": "trial",
  "engine": {"wheel": "<wheel name>", "version": "0.1.0+g36ceb61", "venv_python": "C:\\FLMCP\\venv\\Scripts\\python.exe"},
  "steps": [{"name": "wheel_sha256", "part": "system", "status": "ok", "detail": "sha256 ..."}],
  "doctor": {"status": "ok", "control_plane": {}, "note_plane": {}, "project_file": {}, "run_refusals": 0}
}
```

A step status is `ok`, `failed`, or `unverified` (written but not yet confirmed, such as the MIDI assignment before the controller hello).
`ok` is true only when no step failed and the doctor reports `ok`.
The doctor object is the `fl_doctor` report shape from `contracts/fl-mcp.json`.

### Single-session defaults

A box runs one FL, started by the console session without `FLMCP_*` variables.
Without `FLMCP_SPOOL_ROOT` the engine and the Bridge both use `Documents\Image-Line\FL Studio\Settings\Piano roll scripts\FLMCP` (then its parent), so they agree as long as both run as the FL user.
The Windows Bridge has UDP disabled, so it publishes no `bridge_udp.json` and every Bridge command rides the file spool.
Without `FLMCP_AGENT_PROJECT` the engine's project is `Documents\Image-Line\FL Studio\Projects\FLMCP Agent.flp` when that file exists.
The engine uses the per-machine MIDI wire directly, not the session broker.

FL must have the Bridge loaded, and the packaged template carries it as the last Master insert effect.
FL's default startup project has no Bridge, so a box must open the agent project.
The console session's FL launch (logon script and relaunch task) should therefore reset the agent project from the template and pass its path to `FL64.exe`, and start loopMIDI first.

### Trial mode

A trial FL cannot reopen a project it saved.
`FLMCP_FL_LICENSE=trial` (typed as `FLLicense` in `fl_mcp.contract`) refuses every path that makes FL open a saved project, at one choke point and before any file changes, with the `fl_trial_cannot_reopen` error.

| Where the engine needs FL to open a .flp | Code | Trial |
| --- | --- | --- |
| FL launch on the agent project (box install, `fl-mcp-dev-session`) | `fl.process.launch_fl` | Works (live 2026-10-05): the file is a fresh copy of the licensed-authored template, never saved by the trial, and the trial opens it. |
| File-lane reload after a project-file edit: note writes when the Bridge is not live, tempo and channel-level fallbacks without the control wire, automation, adding effects | `FLSession._reload_fl`, `lifecycle.reload_fl` | Blocked: it kills FL and reopens a file the trial may have saved. Refused under `trial`. |
| `export_offline` (a second FL rendering the saved project, `-R` or `/R`) | `controls.export_offline` | Blocked. Refused under `trial`. |
| `render_wav` (save, then play once while the Bridge captures) | `controls.render_wav` | Opens nothing. Under `trial` FL is never asked to save: live 2026-10-05, the first trial save opened FL's modal Buy dialog and every later controller call (including the render's `transport.setSongPos`) timed out. The capture runs without a save, and the render result reports `project_saved: false`, taking the length from the last saved file. A licensed FL still fails the render on a failed save. |
| Bridge note receipts (save, then parse the saved file) | `BridgeLane._live_readback` | Opens nothing. Under `trial` there is no save (`save_project` refuses with `fl_trial_cannot_save`), so the write applies and the receipt says unconfirmed. |

Live lanes need no file: control-wire tempo, channel levels, plugin parameters, transport, and Bridge note writes work the same in trial.
Reads parse the last saved file and are only as current as the last successful save.

### Live box results (2026-10-05)


- Confirmed: loopMIDI's Burn installer honors `/quiet /norestart` from SYSTEM and installs `C:\Program Files (x86)\Tobias Erichsen\loopMIDI\loopMIDI.exe`.
- Confirmed: a `reg export` of `HKCU\Software\Tobias Erichsen\loopMIDI` (ports are `REG_DWORD` values under `Ports`), imported into the user's hive, recreates `FLMCP Req` and `FLMCP Res` when loopMIDI restarts, and the WinMM names match the engine's rule.
- Refuted: FL for Windows has no `Settings\reg.xml`; it keeps the MIDI assignment in the real registry under `HKCU\Software\Image-Line\FL Studio <N>\Devices`, with the same fields as macOS as `REG_SZ` values (read back from a hand assignment). `fl-mcp-install` now writes there.
  loopMIDI ports are bidirectional, so FL also lists `FLMCP Req` as an output; at startup FL links the scripted input to that same-named output while it has no port, and the controller's replies loop back into FL. The install parks that output on another port.
- Found: loopMIDI ports outlive FL's controller init, so its OnInit hello goes out before the engine opens them; the engine asks with `ctl.hello` on provisioned ports.
- Confirmed: FL, started by the scheduled task with the agent project path, opens it in trial mode with no welcome dialog (title `FLMCP Agent.flp - FL Studio 2026`), finds the DLL by its folder, and loads the Master Bridge from the Mac-authored template. A plain launch still shows the welcome dialog.
- Refuted, then fixed: FL for Windows indexes the plugin and host VMTs from the first declared virtual, not past a 200-byte Free Pascal header (`fl_host_abi.h` `kHostVmtHeaderSlots`). With the fix the heartbeat reads `abi=ok`, `tick` advances and `render_rate` is positive on the VB-Cable device.
- Confirmed: a trial save (FPT_Save) opens FL's modal Buy dialog, and every later controller call times out until a human closes it. Nothing saves under `trial` now (`fl_trial_cannot_save`); note receipts stay unconfirmed and `render_wav` records without a save.
- Confirmed: `fl-mcp-doctor` reports `ok`, and a model run through the harness wrote four notes through the Bridge and `render_wav` captured them (7.9 s, RMS -19.9 dBFS, C-E-G-C on beats 1-4). Tempo readback was not exercised.
- Trial exit (Alt+F4) opens a browser to Image-Line's trial-exit page.
- The Windows capability-profile gate is vacuous today: FL version detection reads a macOS `Info.plist`, so no build is detected and no profile is enforced.
