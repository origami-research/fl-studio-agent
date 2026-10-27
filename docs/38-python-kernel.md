# The run kernel: full Python, confined by the OS

**Status: current** (2026-10-18, replaces the restricted `run` namespace from PR #196; macOS confined by Seatbelt since 2026-10-26).

## What changed

`run` executes model code in a separate, long-lived Python process: a kernel, like a Jupyter notebook's.
The code is full Python: every import, introspection, files, `time.sleep`, numpy, and package installs with `%pip install x` or `!pip install x`.
The boundary is the operating system, not a Python allowlist.
On Windows, the production FL boxes, the kernel is an AppContainer process that can reach only its own workspace.
On macOS it is a Seatbelt sandbox with the same reach (since 2026-10-26; until then the macOS kernel ran unconfined, with the user's own access).
The namespace guard, its import and builtin lists, and the `restricted_namespace` error are deleted.

PR #196 said its filter was "not a Python security sandbox; the contestant box remains the real boundary".
This design makes that boundary explicit and narrow: the model's code runs with less access than the engine, not the same access minus a filter.

The workspace tools (`shell` and the file tools, [39](39-workspace-tools.md)) run behind the same boundary, one process per call through the same launcher.

## How it works

| Part | Where | Job |
| --- | --- | --- |
| `KernelHost` | MCP server, `kernel/host.py` | Boots the kernel, sends cells, serves its `fl` calls, enforces the time budget. |
| kernel | child process, `kernel/child.py` | One persistent namespace; runs cells with `kernel/cell.py`. |
| wire | `kernel/wire.py` | Newline-delimited JSON over the kernel's stdin/stdout pipes. |
| launcher | `Platform.kernel_launcher()` | Starts the process; this is the security boundary (`kernel/windows.py` on Windows, `kernel/seatbelt.py` on macOS). |

The server still owns the FL session.
Inside the kernel, `fl` is the real SDK (`sdk.FL`) over a stand-in session whose every method is a wire call to the server.
The allowed calls are exactly the session methods the SDK uses (`wire.SESSION_METHODS`, pinned to `sdk.py` by a test), with arguments validated against `FLSession`'s own signature.
The server never unpickles or evaluates anything the kernel sends.

The kernel moves the wire off fds 0 and 1 at startup, so `input()` reads an empty pipe (never the null device, which an AppContainer is denied) and a stray `os.write(1, ...)` lands in the server log instead of the protocol.

The server never lets the kernel stall it.
Each line it reads from the kernel is bounded (64 MiB), each write to the kernel has a 10 s deadline, and kernel stderr reaches the server log for at most 200 lines per kernel.
A kernel that breaks any of these is killed and the cell ends `kernel_lost`.
Inside the kernel, any thread may call `fl`: each reply is routed to the call waiting on its id, and an interrupt counts only while a cell is running.
Each text field of a result is cut at 1,000,000 characters with a note, so a huge print does not cost the namespace.

### Outcomes

`RunResult` is `{outcome, stdout, result | traceback, actions}`; `stdout` includes stderr.
`actions` lists the changes FL confirmed for the cell's `fl` calls, recorded by the server as it serves them and kept whatever the outcome ([36](36-engine-contract.md#actions)).

| outcome | Meaning | Kernel state |
| --- | --- | --- |
| `ok` | The cell finished; `result` is the trailing expression's repr. | kept |
| `error` | The cell raised, `SystemExit` included; read `traceback`. | kept |
| `interrupted` | The cell used more than 300 s of kernel time and stopped on interrupt. | kept |
| `kernel_lost` | The process exited (`os._exit`, a crash) or ignored the interrupt for 5 s and was killed. | gone; the next run boots a fresh kernel |

Time the server spends serving the cell's `fl` calls is not charged to the 300 s, so a long render never counts as a runaway cell.
An interrupt cannot break a blocking C call such as `time.sleep` or a subprocess wait, so those end in a kill and `kernel_lost`.
A cell that loses the kernel loses its captured output too.

A kernel that died between cells (for example a background thread's `os._exit`) is replaced before the next cell, whose `stdout` starts with a note saying the previous kernel's state is gone.
A `with fl.file_edit():` block a cell leaves open is closed by the server when the cell ends; if closing fails, the cell's result says so.

A kernel that cannot start makes `run` fail with the `kernel_unavailable` tool error; no model code runs.

### Workspace

`FLMCP_KERNEL_ROOT` names the session's kernel root; unset, the server makes a temp directory.
The server creates:

| Path | Kernel access | Contents |
| --- | --- | --- |
| `<root>/work` | read and write; cwd and `HOME` | model files, `.packages` (pip target), `.tmp`, caches |
| `<root>/renders` | read only | WAVs written by the server for `fl.render(...)` and the `render` tool |
| `<root>/boundary-canary` | none | the probe file described below |

`fl.render(name)` uses only the file name and writes `<root>/renders/<name>`; the `render` tool writes `<root>/renders/render-<id>.wav`.
Every render lands directly in `renders`, never in a folder made inside it, because on Windows the kernel can read a render only through the grant it inherits from `renders` (see "Private directories" below).
The server, not the kernel, writes there, and the kernel cannot write there, so it cannot plant a link that turns a render into a server write somewhere else.
`fl.read_wav(path)` returns `(samples: numpy.ndarray, sample_rate: int)` and runs inside the kernel, as does `fl.analyze(path)`.

`%pip` and `!pip` use the engine's interpreter with `PIP_TARGET=<root>/work/.packages`, which the kernel appends to `sys.path`.
Every other Python started in the workspace (`!python`, `python` in the `shell` tool) appends it too, through the `sitecustomize` on its `PYTHONPATH`.
The engine's own packages stay first on the path, so an install cannot replace the SDK's numpy or pydantic.

The kernel's environment is built, not inherited: `PATH`, the Windows system variables, proxy settings, and workspace locations.
Credentials and `FLMCP_*` session wiring in the server's environment never reach it.

## Windows boundary: AppContainer

The kernel runs in the AppContainer `FLMCP.Kernel` with one capability, `internetClient`.

- Files: an AppContainer token is denied every object that does not grant its SID or `ALL APPLICATION PACKAGES`.
  The server grants the SID `work` (modify) and `renders` (read).
  The kernel also reads the engine's own interpreter: its venv (`sys.prefix`) and the base Python the venv points at (`sys.base_prefix`).
  A box grants those at provisioning (see "Box contract"); on a user's own machine they sit in folders the user owns (uvx's environment in `%LOCALAPPDATA%\uv\cache`, uv's managed Pythons in `%APPDATA%\uv\python`), so the launcher grants the SID read and execute on whichever of the two its ACL does not already allow, once per server, before the first kernel starts.
  Until 2026-10-26 it did not, and `run` failed on every public Windows install with `kernel_unavailable` ("the kernel exited with code 106", "No pyvenv.cfg file"): the venv's redirector could not read its own `pyvenv.cfg`.
  Everything else, including the hosting harness's own directories, other users' profiles, and the session spool, is denied by default.
- Network: `internetClient` allows outbound internet, so pip works.
  Without `privateNetworkClientServer` the VPC is unreachable, and AppContainers get no loopback, so the FL control broker and Bridge ports are unreachable too.
  A firewall rule on the package SID blocks the instance metadata addresses explicitly.
- Private directories: since Python 3.12.4, `os.mkdir(path, 0o700)` on Windows grants only SYSTEM, Administrators, and the owner, so the container could not use a directory it had just made (`tempfile.mkdtemp`, pip's build directories).
  In the kernel, and through a `sitecustomize` on `PYTHONPATH` in every Python it starts, such a directory inherits the workspace ACL instead (`kernel/private_dirs.py`).
  The server is not patched, so it never makes a folder inside `renders`: until 2026-10-19 the `render` tool wrote into a `tempfile.mkdtemp` folder there, which the kernel could not open, so `fl.analyze` on the tool's path failed on boxes.
- Process: the kernel starts suspended, joins a job object, then resumes.
  The job kills the whole tree on kill or when the server exits, runs it below normal priority so FL's audio wins the CPU, and caps it at 4 GiB and 32 processes.
- Sessions: every kernel on a box shares the one SID, so the server revokes the SID's grants on a workspace when its session closes.
  A workspace is granted once, on first use, because `icacls` rewrites the ACL of every file below a folder it grants; once revoked it is never granted again.
  A box runs one session at a time; two concurrent sessions on one box could read each other's workspaces while both are live.
- Wire: the inherited anonymous stdio pipes, so no named-pipe ACL and no loopback exemption are needed.

### Why not a dedicated local account

| | AppContainer | Dedicated low-privilege account |
| --- | --- | --- |
| File access | deny by default; only explicit grants | allow by default for anything `Users` can read; every sensitive directory, present and future, needs a deny |
| Credentials | none | a password the server must hold to call `CreateProcessWithLogonW` |
| Loopback | blocked | open: the kernel could reach the broker and Bridge ports |
| Firewall targeting | `-Package <SID>` | `-LocalUser <SDDL>` |
| Engine code | `CreateProcessW` with security capabilities, via ctypes | `CreateProcessWithLogonW`, via ctypes |

AppContainer fails closed: a directory the harness adds later is already denied.
A dedicated account fails open: the same directory inherits `Users` read access from `C:\` unless someone remembers to deny it.

### Verified, not hoped

On both platforms the kernel runs a boundary probe before its first cell (`kernel/probe.py`).
It tries to read `<root>/boundary-canary`, to create a file beside it (outside `work`), to list the user's home folder, to open a TCP connection to the metadata address (`169.254.169.254:80` and `[fd00:ec2::254]:80`), and to connect to a loopback port the server holds open for the probe.
If any of them succeeds, the server kills the kernel and `run` fails with `kernel_unavailable`; model code never runs unconfined.
A launcher that cannot start the sandbox at all (for example `sandbox-exec` missing) also ends in `kernel_unavailable`.
The write, home, IPv6 metadata and loopback checks were added with the macOS sandbox on 2026-10-26; Windows denied them before (see "Acceptance on a box"), but the probe did not check them.
The probe checks these core properties on every user's machine at every start; the rest of the contract below (other processes, other addresses of this machine, system services) is pinned by the sandbox tests, which CI runs on a macOS runner on every push (see "Acceptance on a Mac"), not on users' machines.

## macOS boundary: Seatbelt

The kernel, and every workspace-tool call, starts under `/usr/bin/sandbox-exec` with a profile `kernel/seatbelt.py` generates for that workspace.
The profile denies everything by default, like an AppContainer, and then allows:

| Access | What |
| --- | --- |
| read and write | `<root>/work` |
| read | `<root>/renders` |
| read and run | the engine's interpreter: what it reports as `sys.executable`, `sys.prefix`, `sys.base_prefix` and its `-I` `sys.path` (uvx's environment and uv's managed Python, a venv, or an editable checkout), with every symlink on the way |
| read and run | the system's software: `/System`, `/usr`, `/bin`, `/sbin`, `/Library/Frameworks`, `/opt/homebrew`, the selected developer tools (Xcode or the Command Line Tools), `/etc`, time zones |
| outbound network | the internet, so pip works |
| services | user lookup and certificate checks (pip verifies HTTPS with the system trust store) |

Everything else is denied, including:

- The user's home folder (`~/.ssh`, `~/Library/Keychains`, `~/.config`, `~/.aws`, browser profiles), other kernels' workspaces, the canary, and Homebrew's service data (`/opt/homebrew/var`).
- This machine on the network: Seatbelt's `localhost` covers loopback and the machine's own addresses, so the FL control broker, the Bridge's ports and every other local service are unreachable, and so are Unix sockets other than the system DNS resolver's.
- Other processes: the kernel can see and signal only processes in its own sandbox, so it cannot stop the server or read another process's arguments and environment (the server's environment can hold credentials).
  Reading another process's arguments needed two rules to close: an explicit `process-info` deny and a short `sysctl-read` list; either alone let `kern.procargs2` through.
- Apple Events, CoreMIDI, the pasteboard, the keychain and every other system service, because Mach service lookups are denied.
  So the model's code cannot drive FL or another app around the server.

The kernel's wire to the server is its inherited stdio pipes, so the sandbox needs no rule for it: `fl` calls reach the server exactly as before, and the server, FL and the Bridge are never sandboxed.
Each path reaches the profile as a `sandbox-exec -D` parameter, never as profile text, so a workspace path cannot change the profile.
`sandbox-exec` replaces itself with the kernel, so the kernel is still the leader of its own process group and a kill takes its whole tree, as before.

### Why Seatbelt

| Option | Verdict |
| --- | --- |
| Seatbelt through `sandbox-exec` | Chosen. Built into every macOS, with no install and no privileges. Chromium and Firefox confine their content processes with Seatbelt, and Bazel and Codex CLI run this same command. Apple marks the command deprecated, but it ships and works on macOS 26. |
| App Sandbox entitlements | Applies to signed app bundles at launch; it cannot confine a Python child of a command-line server. |
| A dedicated user account | Needs administrator setup and a password; allow by default for anything world-readable. |
| A container or VM (Docker, Apple Virtualization) | A large install for every user, and the kernel would need the engine's Python inside it. |

The risk with Seatbelt is a macOS release that changes what a process needs.
A change that breaks a probed property (reading or writing outside the workspace, the home folder, loopback) shows as `kernel_unavailable` on every Mac, never as unconfined code; the sandbox tests in `test_kernel_seatbelt.py` catch the rest in CI.

### Opting out, for engine development only

`FLMCP_KERNEL_UNCONFINED=1` makes the macOS kernel and workspace tools a plain subprocess with your own access, and `serverInfo` says `confined=false`.
It exists for working on the engine itself when the sandbox is in the way; never set it where a model you do not control writes the code.
Unset or `0` keeps the sandbox; any other value stops the server at startup.
Windows has no such setting.

### What macOS does not limit

- The local network and the metadata address: Seatbelt can name only `localhost` or every address, so other machines on your network are reachable.
  The probe's metadata check passes on a Mac because nothing answers there; a Mac in a cloud with a metadata service would fail it and refuse to run.
- Memory, process count and disk: macOS has no per-tree cap a user can set, so the 4 GiB and 32-process job limits are Windows only, and model code can fill the disk through its workspace.
- Whether a name exists in a folder on the way to the engine's Python (your home folder, for a uv install): opening a missing file there says "not found" rather than "not permitted", so a name's existence can be tested; listing the folder and reading any file in it are denied.
- Who is on this Mac: user lookup is allowed (Python's `pwd` and `getpass` need it), so the local account names and the computer's name are readable; no file or secret behind them is.
- A command that calls `setsid` can leave the kernel's process group, so a kill may miss it; it stays sandboxed.

**Acceptance on a Mac** (each is a test in `test_kernel_seatbelt.py`; they start real sandboxes, so they run only with `FLMCP_SANDBOX_TESTS=1`, which CI's `macos-sandbox` job sets on a macOS runner, and that job fails if any of them is skipped)

- Writing outside `work` and listing `~` or `~/.ssh` raise `PermissionError`.
- Reading another session's `work/secret.txt` raises `PermissionError`.
- `socket.create_connection(...)` to a live local listener on `127.0.0.1`, `::1`, or the Mac's own network address raises `PermissionError`.
- `fl.track(...).write(...)`, `fl.set_tempo(...)` and `fl.render(...)` work in the same kernel.
- `%pip install six` then `import six` succeeds, and so does an HTTPS fetch (set `FLMCP_NETWORK_TESTS=1`).
- A cell killed past its budget takes the processes it started with it.
- In `shell`, `osascript -e 'tell application "Finder" to get version'`, `pbpaste` and `defaults read -g` print nothing.

The same job then starts the server over stdio as a client would (`.github/scripts/macos_smoke.py`): it must report `confined=true`, write inside its workspace, and refuse a write outside it through `write_file` and `shell`.
Each release runs that check again on a fresh macOS runner against the package `uvx` installs from PyPI (`release.yml`, `macos-smoke`).

## Box contract

What a production box must provide for the kernel.

**Environment of the MCP server process**

| Variable | Required | Value |
| --- | --- | --- |
| `FLMCP_KERNEL_ROOT` | yes on boxes | An absolute, per-job directory the FL user can create and write, for example `C:\FLMCP\kernels\<job_id>`. It must not sit inside a directory holding job inputs, secrets, or harness state. The harness collects `renders` from it after the job and then deletes it. |

There is no launch-mode setting: on Windows the kernel is always an AppContainer and a failed probe always refuses.

**One-time provisioning, as an administrator**

1. Kernel SID: run `C:\FLMCP\venv\Scripts\fl-mcp-kernel-sid.exe`.
   It prints the SID, which is derived from the name `FLMCP.Kernel` and is the same on every box.
2. Runtime read access: `icacls C:\FLMCP\venv /grant "*<SID>:(OI)(CI)RX"`.
   Repeat after the venv is recreated.
   The FL user cannot rewrite this ACL, so the launcher's own runtime grant (above) only checks it; a runtime the container cannot read and the user cannot grant makes `run` fail with `kernel_unavailable` naming the folder.
   `C:\Program Files\Python312` already grants `ALL APPLICATION PACKAGES` read and execute through `Program Files`; confirm with `icacls`, or grant the SID the same way.
3. Metadata block: `New-NetFirewallRule -DisplayName "FLMCP kernel: no instance metadata" -Direction Outbound -Action Block -RemoteAddress 169.254.169.254,fd00:ec2::254 -Package <SID> -Profile Any`.
4. Windows Firewall: the `mpssvc` service running and the active profile enabled.
   AppContainer network isolation and the rule above both depend on it.
5. Never grant `ALL APPLICATION PACKAGES` or the kernel SID on the hosting harness's directories, user profiles, the spool root, or any parent of `FLMCP_KERNEL_ROOT`.

The FL user, which runs the MCP server, creates the AppContainer profile and grants the workspace when it is first used; that needs no administrator rights.
The workspace tools add one variable and one server mode; see [39's box contract](39-workspace-tools.md#box-contract).

**Acceptance on a box** (run cells through `run` after provisioning)

- `open(r"<a file in the hosting harness's directory>")` raises `PermissionError`.
- `import os; os.listdir(r"C:\Users\Administrator")` raises `PermissionError`.
- `import urllib.request; urllib.request.urlopen("http://169.254.169.254/latest/meta-data/", timeout=3)` raises.
- `import socket; socket.create_connection(("127.0.0.1", 445), timeout=3)` raises (no loopback).
- `%pip install six` then `import six` succeeds.
- `p = fl.render("x.wav"); fl.read_wav(p)[1]` returns the sample rate.

## Known noise

Inside the AppContainer, Python prints `Failed to find real location of ...python.exe` at startup.
It comes from the venv's `python.exe` redirector resolving its own path, and it is harmless: the kernel boots and passes its probe.
It lands in the server log for the kernel, and in the output of `%pip` and `!python` cells.

## Open risks

- The macOS sandbox rests on `sandbox-exec`, which Apple marks deprecated; see "Why Seatbelt" for how a change would show.
- The probe's home and loopback checks are new on Windows (2026-10-26) and not yet run on a Windows box; the box lockdown check already proved both denied.

- First real-Windows run (dev box, 2026-10-18) found that the null device and 0o700 directories are denied inside the container; both are fixed above.
- `PIP_TARGET` installs a package's dependencies even when the engine venv already has them; imports still resolve to the engine's copies first.
- One AppContainer SID per box (the firewall rule needs a fixed SID): concurrent sessions on one box would share it, and a server that crashes leaves its grants until the harness deletes the kernel root.
- The `analyze` tool's `wav` argument still reads any path the server can read (server-side, returns measurements only).
