# Workspace tools: a coding agent's tools, confined by the OS

**Status: current** (2026-10-22, engine phase of the workspace tools design; the box acceptance below passed on a Windows box 2026-10-23; macOS confined by Seatbelt since 2026-10-26).

## What changed

The engine can serve six general-purpose tools beside the FL tools: `shell`, `read_file`, `write_file`, `edit_file`, `glob` and `grep`.
They are the tools a coding agent such as Claude Code, Codex or opencode has, confined to the session's workspace folder.
The boundary is the one the `run` kernel already has ([38](38-python-kernel.md)): on Windows every call is an AppContainer process and on macOS a Seatbelt sandbox, and either can reach only its workspace.
They are off unless asked for, so the fl-mcp-v1 tool surface does not change when a box pins a newer wheel.

## Turning them on

| Setting | Effect |
| --- | --- |
| nothing (default) | The FL tools only: `run, get_scene, check, render, analyze`. |
| `FLMCP_WORKSPACE_TOOLS=1` | The FL tools, then the six workspace tools. Any value other than `0` or `1` stops the server at startup. |
| `fl-mcp --workspace-only` | The six workspace tools alone, with no FL session: the server never starts or connects to FL. For computer-use runs, where the model reaches FL through the desktop. |

The MCP `serverInfo.description` ends with `; confined=true` or `; confined=false`.
It says whether this host's launcher confines model code (`run` and the workspace tools) to the workspace: true on Windows and macOS.
It is false only on a Mac where a developer opted out with `FLMCP_KERNEL_UNCONFINED=1` ([38](38-python-kernel.md#opting-out-for-engine-development-only)), and on an unsupported OS, which runs no model code.
On a confined host nothing runs unconfined: if the boundary check fails, every call is refused.

`contracts/fl-mcp.json` publishes the workspace tools under `workspace_tools`, the gate under `env`, and the flag and description format under `server` ([36](36-engine-contract.md)).

## The tools

Paths are relative to the workspace (`work`) or absolute.
Results show workspace-relative paths with forward slashes, like `notes/plan.md`.

| Tool | Input | Result | Action sentence |
| --- | --- | --- | --- |
| `shell` | `command`, `timeout_s` (1 to 600, default 120) | `exit_code`, `timed_out`, `stdout`, `stderr`, `truncated`, `full_output`, `duration_s`, `note` | `Ran "pip install mido" (exit 0, 3.1 s)` |
| `read_file` | `path`, `offset` (from 1), `limit` (1 to 2000) | `path`, `kind`, `bytes`, `content`, `start_line`, `end_line`, `total_lines`, `truncated`, `hint`, `note` | none |
| `write_file` | `path`, `content` | `path`, `bytes`, `created` | `Wrote notes/plan.md (1.2 KB)` |
| `edit_file` | `path`, `old_string`, `new_string`, `replace_all` | `path`, `replacements` | `Edited notes/plan.md (1 change)` |
| `glob` | `pattern`, `path` | `paths` (newest first), `truncated`, `note` | none |
| `grep` | `pattern` (Python regular expression), `path`, `glob`, `case_insensitive`, `output_mode` (`files`, `lines`, `count`) | `mode`, `matches`, `truncated`, `note` | none |

Every result also carries `actions`, the plain sentences a UI shows as what the assistant did ([36](36-engine-contract.md#actions)); `fl_mcp/actions.py` holds the wording.
A refusal is a `ToolErrorResult` in an MCP `isError` result, with a `fix` that says what to do instead: `edit_file` names a missing or repeated `old_string`, a bad regular expression says so, a denied path says the workspace is the place to write.
A command that exits non-zero is a normal `shell` result, not an error.

`shell` runs `cmd.exe /d /s /c "<command>"` on Windows and `/bin/sh -c` elsewhere, in the workspace, with an empty input.
`python` and `pip` on its `PATH` are the engine's interpreter.
`pip install` lands in `work/.packages`, which every Python started in the workspace imports after the engine's own packages, so an install never replaces the SDK's numpy or pydantic.
cmd.exe runs only the first line of a command, so on Windows a command with line breaks is refused with a fix: chain steps with `&&`, or run a script file.

`read_file` numbers lines like `cat -n`.
UTF-16 text (what PowerShell's `>` writes) reads as text.
A binary file returns its size, a `kind` (`midi`, `audio`, `image`, `fl_song`, `archive`, `binary`) and a hint instead of content.

`edit_file` matches exactly, with one allowance: `read_file` shows Windows line endings as plain newlines, so when the exact `old_string` is not found and the file has `\r\n` endings, the same text with `\r\n` is tried.
New lines take the file's endings when the matched text had `\r\n` or every break in the file is `\r\n`; a mixed file's plain-newline parts still match and stay plain.
An `old_string` whose matches overlap (`aa` in `aaa`) counts as not unique.

`glob` and `grep` skip the workspace's own `.packages`, `.cache`, `.tmp` and `.appdata` folders unless `path` points inside them, and never follow a linked folder.

## Limits

The harness keeps at most 20,000 characters of a tool result as the MCP layer prints it (indented JSON).
Every operation cuts its own output against 18,000 of those characters, counting JSON escapes, and says how to get the rest.

| Limit | Value |
| --- | --- |
| `shell` command | 16 KB |
| `shell` output in the result | 16,000 characters for stdout and stderr together, each keeping its first 40% and last 60%; fewer when escapes make the JSON longer |
| `shell` full output | `work/.logs/shell-<n>.txt` when the output was cut (`full_output` names it), up to 64 MB |
| `read_file` | 2000 lines, 16,000 characters of content, lines cut at 2000 characters; `note` gives the next `offset` |
| `write_file` content | 1 MB |
| `edit_file` strings | 256 KB each |
| `glob` | 500 paths |
| `grep` | 200 results; files over 20 MB and binary files skipped; matched lines cut at 300 characters |
| path, pattern, glob arguments | 4096 characters |
| a search's walk | 200,000 folder entries |
| a file operation | 60 s, then it is stopped and refused |
| a call's process tree on Windows | 2 GiB and 16 processes, below normal priority, in its own job (the kernel's job is separate: 4 GiB, 32) |

## How a call runs

`fl_mcp/workspace` has three parts.

| Part | Job |
| --- | --- |
| `runner.py` (server) | Starts one process per call through the kernel's launcher, sends the request, waits, then stops the call's whole process tree. |
| `op.py` (confined) | `python -I -m fl_mcp.workspace.op`: reads one JSON request on stdin, writes one JSON answer on stdout. |
| `ops.py` (confined) | The operations themselves, stdlib only. |

Each call gets the kernel's launcher, workspace and built environment: the same AppContainer SID, no credentials, no `FLMCP_*` wiring.
A fresh process per call means a hung command cannot cost the model its `run` variables, no state leaks between calls, and a timeout is simply the end of the job.
Starting Python takes well under a second, small next to a model turn.

The server never opens a path inside `work` itself.
Everything that touches the workspace, including the `shell` log, runs inside the confined process, so a link the model plants there cannot redirect a server read or write.
What the op answers is untrusted data: at most 4 MiB, validated against the result models.
`-I` keeps the workspace and `.packages` off the op's own `sys.path`, so a file the model wrote is never imported by it.

`shell` enforces the command's timeout inside the op and returns the output so far with `timed_out: true`.
The server's own deadline is the command's timeout plus 15 s; past it the op is killed and the result says the output was lost.
When a call ends, its process tree is stopped: the job on Windows, the process group on macOS and Linux.
A command that leaves a background process gets a note saying it was stopped.

Before the first call on a confined host, the runner launches the op once as the kernel's boundary probe ([38](38-python-kernel.md#verified-not-hoped)): it must not read `<root>/boundary-canary`, create a file beside it, list the user's home folder, or connect to the metadata address or a loopback port the server holds open.
If any succeeds, every workspace call fails with `kernel_unavailable` (not retryable) and nothing more is launched.
A probe that cannot finish is retried on the next call.

The workspace is granted to the AppContainer once and revoked when the session closes, by `WorkspaceRunner.close()` as well as `KernelHost.close()`.
The launcher remembers what it granted, because `icacls` rewrites the ACL of every file below a folder it grants and the workspace tools launch once per call.
A release is final: a launch that races shutdown is refused rather than granting the finished session again.
The server's shutdown closes the workspace tools, the kernel and the FL session as separate steps, so one failing (a revoke `icacls` refuses) does not skip the others.

On macOS each call runs in the kernel's Seatbelt sandbox, generated for the session's workspace ([38](38-python-kernel.md#macos-boundary-seatbelt)); there is nothing to grant or revoke.
A command that calls `setsid` can leave its process group there, so stopping the call may miss it; it stays sandboxed.
The engine's platforms are Windows and macOS, so `--workspace-only` refuses to start on Linux.

## Box contract

Everything in [38's box contract](38-python-kernel.md#box-contract) applies; the workspace tools need nothing more from the box.

| Variable or argument | Where | Value |
| --- | --- | --- |
| `FLMCP_KERNEL_ROOT` | both modes | The per-job root, as for the kernel. Computer-use runs need it too: it holds the workspace the tools work in. |
| `FLMCP_WORKSPACE_TOOLS` | MCP-mode runs that offer the tools | `1` |
| `--workspace-only` | computer-use runs | passed to `fl-mcp`; no FL session is started |

Acceptance on a box, in addition to [38's](38-python-kernel.md#box-contract), through the workspace tools of a `--workspace-only` server:

- A Python script run by `shell` finds its own token is an AppContainer (`TokenIsAppContainer`) whose package SID (`TokenAppContainerSid`) is what `fl-mcp-kernel-sid` prints. (`whoami /groups` lists neither the package SID nor an `APPLICATION PACKAGE AUTHORITY` group there, so it proves nothing.)
- `write_file x.txt` then `read_file x.txt` round-trips.
- `shell "python -m pip install -q six"` then `shell "python -c \"import six\""` succeeds.
- `read_file` of a secret in the hosting harness's directory (one the FL user can read) is refused (permission denied).
- `shell "type <a file in the hosting harness's directory>"` and `shell "echo x > <a folder the FL user can write outside the workspace>\probe"` are denied.
- `shell "mklink /J j <the hosting harness's directory>"` is refused ("Access is denied"): Windows does not let a sandboxed process create a junction to a folder it cannot write.
- A junction to the hosting harness's directory planted in the workspace by the FL user (as a computer-use model can) is no way out either: `read_file planted\<a file in it>` and `shell "type planted\<a file in it>"` are denied. The boundary is the OS, not a path check.
- `read_file` of another session's `work\secret.txt` is refused.
- `write_file ..\renders\x.wav` and `read_file ..\boundary-canary` are refused.
- `shell "start /b ping -t 127.0.0.1"` returns, and afterwards `tasklist` shows no `ping.exe`.
- `serverInfo.description` ends with `; confined=true`.

## Verified on a box, and what is not yet

The acceptance list above passed on a Windows FL box on 2026-10-23, through the hosting harness's lockdown check (`verify-lockdown.ps1`, its workspace tools section, run as the FL user through the harness's own engine client), along with IMDS, loopback and private-network denials and the grant gone after close.
A live MCP-mode run with the tools on that box wrote its notes through `write_file` and was collected with them.
PowerShell inside the container is still unproven, so the `shell` description does not offer it.
