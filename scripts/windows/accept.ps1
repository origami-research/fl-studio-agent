[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$SpoolRoot = (Join-Path $env:TEMP ("flmcp-accept-" + [guid]::NewGuid().ToString('N')))
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $IsWindows) { throw 'accept.ps1 runs only on Windows.' }

New-Item -ItemType Directory -Force -Path $SpoolRoot | Out-Null
$env:FLMCP_SPOOL_ROOT = $SpoolRoot
$assertions = @'
report = fl._session.doctor()
assert report.status == "ok", report
assert report.control_plane.connected, report.control_plane
assert report.note_plane.connected, report.note_plane
heartbeat = __import__("fl_mcp.session.detect", fromlist=["read_heartbeat"]).read_heartbeat(
    __import__("pathlib").Path(__import__("os").environ["FLMCP_SPOOL_ROOT"]) / "bridge_heartbeat.txt")
assert heartbeat.abi == "ok", heartbeat
assert heartbeat.render_rate > 0, heartbeat
print("acceptance: hello answered; abi=ok; render_rate=%s" % heartbeat.render_rate)
'@

# fl-mcp-dev-session copies fl-mcp-template, launches it with this spool root,
# waits for bridge_udp.json, waits for controller hello, and tears down only the
# Bridge PID published in that session's port file.
uv run --directory (Join-Path $RepoRoot 'mcp-server') fl-mcp-dev-session --spool $SpoolRoot --exec $assertions
if ($LASTEXITCODE -ne 0) { throw 'Windows acceptance failed. Preserve the spool root for diagnosis.' }
