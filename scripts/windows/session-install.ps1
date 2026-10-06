# FL MCP box install, FL-user-session part. Windows PowerShell 5.1 compatible.
#
# provision.ps1 runs this as an interactive scheduled task in the console session
# of the user who runs FL, because everything here is per user or per session:
# FL's Settings folder and HKCU MIDI assignment live in that profile, loopMIDI ports exist only
# while loopMIDI runs in that session, and FL must start on that desktop. It writes
# its typed result to -ResultPath, including the final fl-mcp-doctor report.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Root,
    [Parameter(Mandatory)][string]$FLExe,
    [Parameter(Mandatory)][ValidateSet('trial', 'licensed')][string]$FLLicense,
    [Parameter(Mandatory)][string]$LoopMidiExe,
    [Parameter(Mandatory)][string]$ResultPath,
    [string]$LoopMidiPortsReg,
    [int]$BootTimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$steps = New-Object System.Collections.ArrayList
$doctor = $null
$sessionId = (Get-Process -Id $PID).SessionId
$scripts = Join-Path $Root 'venv\Scripts'
$python = Join-Path $scripts 'python.exe'

function Add-Step([string]$Name, [string]$Status, [string]$Detail) {
    [void]$steps.Add([ordered]@{ name = $Name; status = $Status; detail = $Detail })
}

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    # Windows PowerShell 5.1 turns native stderr into terminating errors under
    # ErrorActionPreference=Stop; the exit code is the verdict here, not stderr.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Exe @Arguments 2>$null | ForEach-Object { "$_" }
        return [pscustomobject]@{ Code = $LASTEXITCODE; Output = ($output -join "`n") }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Invoke-Python([string]$Code) {
    # No double quotes inside $Code: Windows PowerShell 5.1 does not escape them for native commands.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $python -c $Code 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ Code = $LASTEXITCODE; Output = ($output -join "`n").Trim() }
    } finally {
        $ErrorActionPreference = $previous
    }
}

try {
    if ($sessionId -eq 0) { throw 'running in session 0; this step must run in the FL user''s interactive session' }
    Add-Step 'session' 'ok' "$env:USERNAME in session $sessionId"
    # The single-session defaults: no session spool, no agent-project override.
    Remove-Item Env:FLMCP_SPOOL_ROOT, Env:FLMCP_AGENT_PROJECT -ErrorAction SilentlyContinue
    $env:FLMCP_FL_LICENSE = $FLLicense

    # FL must be closed while its MIDI assignment is edited (it rewrites it on a clean exit)
    # and before the MIDI ports change.
    Get-Process -Name FL64 -ErrorAction SilentlyContinue | Stop-Process -Force
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Process -Name FL64 -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }

    $install = Invoke-Native (Join-Path $scripts 'fl-mcp-install.exe') @('--json')
    $report = $install.Output | ConvertFrom-Json
    foreach ($name in 'note_script', 'controller_script') {
        $step = $report.$name
        Add-Step $name $(if ($step.status -eq 'OK') { 'ok' } else { 'failed' }) $step.detail
    }
    # A written assignment is not an accepted one: only the controller hello proves it.
    $midi = $report.midi_assignment
    Add-Step 'midi_assignment' $(if ($midi.status -eq 'FAIL') { 'failed' } else { 'unverified' }) "$($midi.status) $($midi.detail)"

    # A trial FL cannot reopen a project it saved, so the agent project is reset to
    # the packaged (licensed-authored) template on every install.
    $project = Invoke-Python 'from fl_mcp.make_agent_project import create; print(create(force=True))'
    if ($project.Code -ne 0 -or -not (Test-Path -LiteralPath $project.Output)) { throw "agent project: $($project.Output)" }
    Add-Step 'agent_project' 'ok' $project.Output

    if ($LoopMidiPortsReg) {
        $imported = Invoke-Native 'reg.exe' @('import', $LoopMidiPortsReg)
        # VERIFY-ON-WINDOWS: loopMIDI reads HKCU\Software\Tobias Erichsen\loopMIDI at start.
        Add-Step 'loopmidi_ports_reg' $(if ($imported.Code -eq 0) { 'unverified' } else { 'failed' }) "reg import exit $($imported.Code)"
    }
    Get-Process -Name loopMIDI -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Process -FilePath $LoopMidiExe
    Start-Sleep -Seconds 5
    # The engine's own port rule (WinMM-suffixed names, exactly one match) decides.
    $ports = Invoke-Python 'from fl_mcp.core.carriers.wire import windows_midi_control; windows_midi_control().close(); print(''FLMCP Req and FLMCP Res open'')'
    if ($ports.Code -eq 0) {
        Add-Step 'midi_ports' 'ok' $ports.Output
    } else {
        Add-Step 'midi_ports' 'failed' ("$($ports.Output) Create both ports once in loopMIDI in this session, then pass " +
            '`reg export "HKCU\Software\Tobias Erichsen\loopMIDI"` as -LoopMidiPortsRegUrl.')
    }

    # FL starts on the agent project so the Bridge (on the template's Master insert)
    # loads and the engine's default project path is the file FL has open.
    Start-Process -FilePath $FLExe -ArgumentList ('"' + $project.Output + '"')
    Add-Step 'fl_started' 'ok' "$FLExe `"$($project.Output)`""

    $doctorExe = Join-Path $scripts 'fl-mcp-doctor.exe'
    $deadline = (Get-Date).AddSeconds($BootTimeoutSeconds)
    do {
        Start-Sleep -Seconds 10
        $run = Invoke-Native $doctorExe @()
        try { $doctor = $run.Output | ConvertFrom-Json } catch { $doctor = $null }
    } while (($null -eq $doctor -or $doctor.status -ne 'ok') -and (Get-Date) -lt $deadline)
    if ($null -eq $doctor) {
        Add-Step 'doctor' 'failed' "fl-mcp-doctor printed no report (exit $($run.Code))"
    } elseif ($doctor.status -eq 'ok') {
        Add-Step 'doctor' 'ok' 'control plane, note plane, and project file connected'
    } else {
        $degraded = foreach ($plane in 'control_plane', 'note_plane', 'project_file') {
            if (-not $doctor.$plane.connected) { "${plane}: $($doctor.$plane.detail)" }
        }
        Add-Step 'doctor' 'failed' ("degraded after $BootTimeoutSeconds s: " + ($degraded -join '; '))
    }
} catch {
    Add-Step 'unexpected' 'failed' $_.Exception.Message
}

$result = [ordered]@{ session_id = $sessionId; steps = $steps; doctor = $doctor }
[IO.File]::WriteAllText($ResultPath, ($result | ConvertTo-Json -Depth 12), (New-Object System.Text.UTF8Encoding $false))
