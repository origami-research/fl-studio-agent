# The run kernel: full Python, confined by the OS

**Status: current** (2026-10-18, replaces the restricted `run` namespace from PR #196).

## What changed

`run` executes model code in a separate, long-lived Python process: a kernel, like a Jupyter notebook's.
The code is full Python: every import, introspection, files, `time.sleep`, numpy, and package installs with `%pip install x` or `!pip install x`.
The boundary is the operating system, not a Python allowlist.
On Windows, the production FL boxes, the kernel is an AppContainer process that can reach only its own workspace.
The namespace guard, its import and builtin lists, and the `restricted_namespace` error are deleted.

PR #196 said its filter was "not a Python security sandbox; the contestant box remains the real boundary".
This design makes that boundary explicit and narrow: the model's code runs with less access than the engine, not the same access minus a filter.

## How it works

| Part | Where | Job |
| --- | --- | --- |
| `KernelHost` | MCP server, `kernel/host.py` | Boots the kernel, sends cells, serves its `fl` calls, enforces the time budget. |
| kernel | child process, `kernel/child.py` | One persistent namespace; runs cells with `kernel/cell.py`. |
| wire | `kernel/wire.py` | Newline-delimited JSON over the kernel's stdin/stdout pipes. |
| launcher | `Platform.kernel_launcher()` | Starts the process; on Windows this is the security boundary (`kernel/windows.py`). |

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

`RunResult` is `{outcome, stdout, result | traceback}`; `stdout` includes stderr.

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

`fl.render(name)` uses only the file name and writes `<root>/renders/<name>`.
The server, not the kernel, writes there, and the kernel cannot write there, so it cannot plant a link that turns a render into a server write somewhere else.
`fl.read_wav(path)` returns `(samples: numpy.ndarray, sample_rate: int)` and runs inside the kernel, as does `fl.analyze(path)`.

`%pip` and `!pip` use the engine's interpreter with `PIP_TARGET=<root>/work/.packages`, which the kernel appends to `sys.path`.
The engine's own packages stay first on the path, so an install cannot replace the SDK's numpy or pydantic.

The kernel's environment is built, not inherited: `PATH`, the Windows system variables, proxy settings, and workspace locations.
Credentials and `FLMCP_*` session wiring in the server's environment never reach it.

## Windows boundary: AppContainer

The kernel runs in the AppContainer `FLMCP.Kernel` with one capability, `internetClient`.

- Files: an AppContainer token is denied every object that does not grant its SID or `ALL APPLICATION PACKAGES`.
  The server grants the SID `work` (modify) and `renders` (read).
  Everything else, including `C:\PrivateProduct`, other users' profiles, and the session spool, is denied by default.
- Network: `internetClient` allows outbound internet, so pip works.
  Without `privateNetworkClientServer` the VPC is unreachable, and AppContainers get no loopback, so the FL control broker and Bridge ports are unreachable too.
  A firewall rule on the package SID blocks the instance metadata addresses explicitly.
- Private directories: since Python 3.12.4, `os.mkdir(path, 0o700)` on Windows grants only SYSTEM, Administrators, and the owner, so the container could not use a directory it had just made (`tempfile.mkdtemp`, pip's build directories).
  In the kernel, and through a `sitecustomize` on `PYTHONPATH` in every Python it starts, such a directory inherits the workspace ACL instead (`kernel/private_dirs.py`).
- Process: the kernel starts suspended, joins a job object, then resumes.
  The job kills the whole tree on kill or when the server exits, runs it below normal priority so FL's audio wins the CPU, and caps it at 4 GiB and 32 processes.
- Sessions: every kernel on a box shares the one SID, so the server revokes the SID's grants on a workspace when its session closes.
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

When the launcher is confined, the kernel runs a boundary probe before its first cell.
It tries to read `<root>/boundary-canary` and to open a TCP connection to `169.254.169.254:80`.
If either succeeds, the server kills the kernel and `run` fails with `kernel_unavailable`; model code never runs unconfined.

### macOS and Linux dev hosts

The kernel is a plain subprocess in its own process group, with the workspace as cwd and home and the same built environment.
Nothing confines it; it runs with the developer's access.
`sandbox-exec` was not adopted: it is deprecated and its profiles break as macOS changes, and dev hosts are not where untrusted models run.

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
   `C:\Program Files\Python312` already grants `ALL APPLICATION PACKAGES` read and execute through `Program Files`; confirm with `icacls`, or grant the SID the same way.
3. Metadata block: `New-NetFirewallRule -DisplayName "FLMCP kernel: no instance metadata" -Direction Outbound -Action Block -RemoteAddress 169.254.169.254,fd00:ec2::254 -Package <SID> -Profile Any`.
4. Windows Firewall: the `mpssvc` service running and the active profile enabled.
   AppContainer network isolation and the rule above both depend on it.
5. Never grant `ALL APPLICATION PACKAGES` or the kernel SID on `C:\PrivateProduct`, user profiles, the spool root, or any parent of `FLMCP_KERNEL_ROOT`.

The FL user, which runs the MCP server, creates the AppContainer profile and grants the workspace at each kernel start; that needs no administrator rights.

**Acceptance on a box** (run cells through `run` after provisioning)

- `open(r"C:\PrivateProduct\<any file>")` raises `PermissionError`.
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

- First real-Windows run (dev box, 2026-10-18) found that the null device and 0o700 directories are denied inside the container; both are fixed above.
- `PIP_TARGET` installs a package's dependencies even when the engine venv already has them; imports still resolve to the engine's copies first.
- One AppContainer SID per box (the firewall rule needs a fixed SID): concurrent sessions on one box would share it, and a server that crashes leaves its grants until the harness deletes the kernel root.
- The `analyze` tool's `wav` argument still reads any path the server can read (server-side, returns measurements only).
