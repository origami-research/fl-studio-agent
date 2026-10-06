# FL MCP box install, SYSTEM part. Windows PowerShell 5.1 compatible.
#
# Runs elevated (SYSTEM over SSM, or an administrator) on a box where FL Studio is
# installed and one local user's interactive console session runs FL. It installs
# the engine from ONE stamped wheel into a dedicated venv, installs the Bridge DLL
# and loopMIDI, then runs session-install.ps1 inside that user's session (an
# interactive scheduled task) to install the per-user FL files, bring up the MIDI
# ports, start FL on the agent project, and run fl-mcp-doctor. Every downloaded
# artifact must be pinned by sha256 and is read back after it lands; a local path
# may be unpinned because the caller already verified it. The typed result is written to
# -ResultPath; stdout ends with FLMCP_INSTALL_OK or FLMCP_INSTALL_FAILED.
#
# Artifact arguments take a presigned https URL or a local path. Build the wheel and
# its hash-locked requirements with scripts/build-wheel.sh; the Bridge DLL is the
# native-windows workflow artifact "FL-MCP-Bridge-Windows-x64".
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$WheelUrl,
    [Parameter(Mandatory)][string]$WheelName,
    [Parameter(Mandatory)][string]$WheelSha256,
    [Parameter(Mandatory)][string]$RequirementsUrl,
    [Parameter(Mandatory)][string]$RequirementsSha256,
    [Parameter(Mandatory)][string]$BridgeDllUrl,
    [Parameter(Mandatory)][string]$BridgeDllSha256,
    # The local user whose auto-logged-on console session runs FL.
    [Parameter(Mandatory)][string]$FLUser,
    [ValidateSet('trial', 'licensed')][string]$FLLicense = 'trial',
    # Optional: `reg export "HKCU\Software\Tobias Erichsen\loopMIDI"` captured after
    # creating the two ports once by hand; imported into the FL user's hive.
    [string]$LoopMidiPortsRegUrl,
    [string]$LoopMidiPortsRegSha256,
    [string]$SessionScriptUrl = (Join-Path $PSScriptRoot 'session-install.ps1'),
    [string]$SessionScriptSha256,
    [string]$Root = 'C:\FLMCP',
    [string]$FLRoot,
    [string]$ResultPath = 'C:\FLMCP\install-result.json',
    # python-rtmidi 1.5.8 has no cp313 Windows wheel, so the engine venv uses 3.12.
    # 3.12.10 is the last 3.12 release with a Windows installer (python.org MD5 matches).
    [string]$PythonExe = 'C:\Program Files\Python312\python.exe',
    [string]$PythonInstallerUrl = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe',
    [string]$PythonInstallerSha256 = '67B5635E80EA51072B87941312D00EC8927C4DB9BA18938F7AD2D27B328B95FB',
    # The winget manifest TobiasErichsen.loopMIDI 1.0.16.27 pins this zip (a WiX Burn bundle).
    [string]$LoopMidiZipUrl = 'https://www.tobias-erichsen.de/wp-content/uploads/2020/01/loopMIDISetup_1_0_16_27.zip',
    [string]$LoopMidiZipSha256 = '975399EB76E2C5A1D553ECD8975AB361261CCAB8FD9412B8060B19877CB4E0D5',
    [int]$SessionTimeoutSeconds = 600
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$script:steps = New-Object System.Collections.ArrayList
$script:engine = [ordered]@{ wheel = $WheelName; version = $null; venv_python = $null }
$script:doctor = $null
$script:failed = $false

function Add-Step([string]$Name, [string]$Status, [string]$Detail) {
    [void]$script:steps.Add([ordered]@{ name = $Name; part = 'system'; status = $Status; detail = $Detail })
    if ($Status -eq 'failed') { $script:failed = $true }
    Write-Output ("[{0}] {1}: {2}" -f $Status, $Name, $Detail)
}

function Stop-Install([string]$Name, [string]$Detail) {
    Add-Step $Name 'failed' $Detail
    throw "FLMCP install stopped at ${Name}: $Detail"
}

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    # Windows PowerShell 5.1 turns native stderr into terminating errors under
    # ErrorActionPreference=Stop; the exit code is the verdict here, not stderr.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ Code = $LASTEXITCODE; Output = ($output -join "`n") }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Get-Artifact([string]$Source, [string]$Destination, [string]$Sha256, [string]$Name) {
    if ($Source -match '^https?://') {
        # A downloaded artifact runs elevated on this box, so it must be pinned.
        if (-not $Sha256) { Stop-Install $Name "remote artifact $Source has no sha256 pin" }
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        (New-Object System.Net.WebClient).DownloadFile($Source, $Destination)
    } else {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
    }
    if ($Sha256) {
        $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash
        if ($actual -ne $Sha256.ToUpperInvariant()) { Stop-Install $Name "sha256 $actual, pinned $Sha256" }
        Add-Step $Name 'ok' "sha256 $actual"
    } else {
        Add-Step $Name 'ok' "fetched $((Get-Item -LiteralPath $Destination).Length) bytes (no pin)"
    }
}

function Stop-FL {
    $running = @(Get-Process -Name FL64 -ErrorAction SilentlyContinue)
    foreach ($process in $running) { Stop-Process -Id $process.Id -Force }
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Process -Name FL64 -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    if (Get-Process -Name FL64 -ErrorAction SilentlyContinue) { Stop-Install 'fl_stopped' 'FL64.exe is still running after 30 s' }
    Add-Step 'fl_stopped' 'ok' "stopped $($running.Count) FL64.exe process(es) so the Bridge DLL is not locked"
}

function Find-LoopMidi {
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        $candidate = Join-Path $base 'Tobias Erichsen\loopMIDI\loopMIDI.exe'
        if ($base -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Test-InteractiveSession([string]$User) {
    foreach ($explorer in @(Get-CimInstance Win32_Process -Filter "Name = 'explorer.exe'")) {
        $owner = Invoke-CimMethod -InputObject $explorer -MethodName GetOwner
        if ($owner.User -eq $User) { return $explorer.SessionId }
    }
    return $null
}

function Write-Result {
    $result = [ordered]@{
        schema = 'flmcp-box-install/1'
        ok = (-not $script:failed) -and ($null -ne $script:doctor) -and ($script:doctor.status -eq 'ok')
        finished_at = (Get-Date).ToUniversalTime().ToString('o')
        fl_user = $FLUser
        fl_license = $FLLicense
        engine = $script:engine
        steps = $script:steps
        doctor = $script:doctor
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ResultPath) | Out-Null
    [IO.File]::WriteAllText($ResultPath, ($result | ConvertTo-Json -Depth 12), (New-Object System.Text.UTF8Encoding $false))
    return $result.ok
}

try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Stop-Install 'elevated' 'run as SYSTEM or from an elevated prompt'
    }
    if (-not (Get-LocalUser -Name $FLUser -ErrorAction SilentlyContinue)) { Stop-Install 'fl_user' "local user '$FLUser' does not exist" }
    if (-not $FLRoot) {
        # Same rule as fl_mcp.fl.paths.fl_app(): the newest installed FL Studio.
        $newest = Get-ChildItem -Path (Join-Path $env:ProgramFiles 'Image-Line') -Directory -Filter 'FL Studio*' -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'FL64.exe') } | Sort-Object Name -Descending | Select-Object -First 1
        if (-not $newest) { Stop-Install 'fl_install' "no FL Studio*\FL64.exe under $env:ProgramFiles\Image-Line" }
        $FLRoot = $newest.FullName
    }
    $flExe = Join-Path $FLRoot 'FL64.exe'
    if (-not (Test-Path -LiteralPath $flExe)) { Stop-Install 'fl_install' "$flExe not found" }
    Add-Step 'fl_install' 'ok' "$flExe $((Get-Item -LiteralPath $flExe).VersionInfo.ProductVersion)"
    if ($WheelName -notmatch '^fl_studio_agent-(?<version>[^-]+)-py3-none-any\.whl$') {
        Stop-Install 'wheel_name' "'$WheelName' is not an fl_studio_agent wheel name"
    }
    $expectedVersion = $Matches['version']

    $downloads = Join-Path $Root 'downloads'
    $sessionDir = Join-Path $Root 'session'
    New-Item -ItemType Directory -Force -Path $Root, $downloads, $sessionDir | Out-Null
    $wheel = Join-Path $downloads $WheelName
    $requirements = Join-Path $downloads "$WheelName.requirements.txt"
    $dll = Join-Path $downloads 'FL MCP Bridge_x64.dll'
    Get-Artifact $WheelUrl $wheel $WheelSha256 'wheel_sha256'
    Get-Artifact $RequirementsUrl $requirements $RequirementsSha256 'requirements_sha256'
    Get-Artifact $BridgeDllUrl $dll $BridgeDllSha256 'bridge_dll_sha256'
    $sessionScript = Join-Path $Root 'session-install.ps1'
    Get-Artifact $SessionScriptUrl $sessionScript $SessionScriptSha256 'session_script'
    $portsReg = $null
    if ($LoopMidiPortsRegUrl) {
        $portsReg = Join-Path $sessionDir 'loopmidi-ports.reg'
        Get-Artifact $LoopMidiPortsRegUrl $portsReg $LoopMidiPortsRegSha256 'loopmidi_ports_reg_sha256'
    }

    if (-not (Test-Path -LiteralPath $PythonExe)) {
        $installer = Join-Path $downloads (Split-Path -Leaf $PythonInstallerUrl)
        Get-Artifact $PythonInstallerUrl $installer $PythonInstallerSha256 'python_installer_sha256'
        # PrependPath=0 and no launcher: the box's other Python keeps PATH and py.exe.
        $targetDir = Split-Path -Parent $PythonExe
        $p = Start-Process $installer -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=0', 'Include_launcher=0', 'Include_test=0', "TargetDir=`"$targetDir`"" -Wait -PassThru
        if ($p.ExitCode -ne 0) { Stop-Install 'python' "installer exit $($p.ExitCode)" }
    }
    # No double quotes inside -c code: Windows PowerShell 5.1 does not escape them for native commands.
    $pyVersion = Invoke-Native $PythonExe @('-c', 'import sys; print(''%d.%d'' % sys.version_info[:2])')
    if ($pyVersion.Code -ne 0 -or $pyVersion.Output.Trim() -ne '3.12') { Stop-Install 'python' "$PythonExe reports '$($pyVersion.Output)'" }
    Add-Step 'python' 'ok' "$PythonExe 3.12"

    $venv = Join-Path $Root 'venv'
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    # A running engine holds its package files open; stop only processes of this venv.
    foreach ($process in @(Get-CimInstance Win32_Process | Where-Object {
                ("$($_.ExecutablePath) $($_.CommandLine)") -like "*$venv\*" })) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $created = Invoke-Native $PythonExe @('-m', 'venv', $venv)
        if ($created.Code -ne 0) { Stop-Install 'venv' $created.Output }
    }
    $pip = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input')
    $locked = Invoke-Native $venvPython ($pip + @('--require-hashes', '--only-binary=:all:', '-r', $requirements))
    if ($locked.Code -ne 0) { Stop-Install 'dependencies' $locked.Output }
    # --force-reinstall: a same-version reinstall must never be skipped as satisfied.
    $installed = Invoke-Native $venvPython ($pip + @('--no-deps', '--no-index', '--force-reinstall', $wheel))
    if ($installed.Code -ne 0) { Stop-Install 'engine_wheel' $installed.Output }
    $check = Invoke-Native $venvPython @('-m', 'pip', 'check')
    if ($check.Code -ne 0) { Stop-Install 'dependencies' $check.Output }
    $version = Invoke-Native $venvPython @('-c', 'from importlib.metadata import version; import fl_mcp.server, fl_mcp.doctor; print(version(''fl-studio-agent''))')
    if ($version.Code -ne 0 -or $version.Output.Trim() -ne $expectedVersion) {
        Stop-Install 'engine_version' "installed '$($version.Output)', expected $expectedVersion"
    }
    $script:engine.version = $expectedVersion
    $script:engine.venv_python = $venvPython
    Add-Step 'engine_version' 'ok' "fl-studio-agent $expectedVersion imports from $venv"

    # Every process the FL user starts after its next logon sees this; the session
    # step also sets it explicitly for fl-mcp-doctor.
    [Environment]::SetEnvironmentVariable('FLMCP_FL_LICENSE', $FLLicense, 'Machine')
    $readLicense = [Environment]::GetEnvironmentVariable('FLMCP_FL_LICENSE', 'Machine')
    if ($readLicense -ne $FLLicense) { Stop-Install 'fl_license_env' "machine FLMCP_FL_LICENSE reads '$readLicense'" }
    Add-Step 'fl_license_env' 'ok' "machine FLMCP_FL_LICENSE=$FLLicense (new logons)"

    $pluginDir = Join-Path $FLRoot 'Plugins\Fruity\Effects\FL MCP Bridge'
    New-Item -ItemType Directory -Force -Path $pluginDir | Out-Null
    $installedDll = Join-Path $pluginDir 'FL MCP Bridge_x64.dll'
    $current = if (Test-Path -LiteralPath $installedDll) { (Get-FileHash -LiteralPath $installedDll -Algorithm SHA256).Hash } else { '' }
    if ($current -ne $BridgeDllSha256.ToUpperInvariant()) {
        # A loaded DLL is locked; FL is stopped only when the Bridge actually changes.
        Stop-FL
        Copy-Item -LiteralPath $dll -Destination $installedDll -Force
    }
    Set-Content -LiteralPath (Join-Path $pluginDir 'Plugin.nfo') -Value 'ps_Vendorname=FL MCP' -NoNewline -Encoding ASCII
    $dllHash = (Get-FileHash -LiteralPath $installedDll -Algorithm SHA256).Hash
    if ($dllHash -ne $BridgeDllSha256.ToUpperInvariant()) { Stop-Install 'bridge_dll' "installed copy hashes $dllHash" }
    Add-Step 'bridge_dll' 'ok' "$installedDll (load is proven only by the doctor's note plane)"

    $loopMidi = Find-LoopMidi
    if (-not $loopMidi) {
        $zip = Join-Path $downloads (Split-Path -Leaf $LoopMidiZipUrl)
        Get-Artifact $LoopMidiZipUrl $zip $LoopMidiZipSha256 'loopmidi_zip_sha256'
        Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $downloads 'loopmidi') -Force
        # VERIFY-ON-WINDOWS: standard WiX Burn switches, as winget uses for this bundle.
        $p = Start-Process (Join-Path $downloads 'loopmidi\loopMIDISetup.exe') -ArgumentList '/quiet', '/norestart' -Wait -PassThru
        if ($p.ExitCode -notin 0, 3010) { Stop-Install 'loopmidi' "installer exit $($p.ExitCode)" }
        $loopMidi = Find-LoopMidi
        if (-not $loopMidi) { Stop-Install 'loopmidi' 'installer finished but loopMIDI.exe was not found' }
    }
    Add-Step 'loopmidi' 'ok' $loopMidi

    $sessionId = Test-InteractiveSession $FLUser
    if ($null -eq $sessionId) { Stop-Install 'fl_user_session' "$FLUser has no interactive session (autologon inactive?); reboot, then rerun" }
    Add-Step 'fl_user_session' 'ok' "$FLUser is logged on in session $sessionId"
    & icacls.exe $sessionDir /grant "${FLUser}:(OI)(CI)M" /T /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-Install 'session_dir_acl' "icacls exit $LASTEXITCODE" }

    $sessionResult = Join-Path $sessionDir 'result.json'
    Remove-Item -LiteralPath $sessionResult -Force -ErrorAction SilentlyContinue
    $taskArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$sessionScript`" -Root `"$Root`" " +
        "-FLExe `"$flExe`" -FLLicense $FLLicense -LoopMidiExe `"$loopMidi`" -ResultPath `"$sessionResult`""
    if ($portsReg) { $taskArgs += " -LoopMidiPortsReg `"$portsReg`"" }
    $taskName = 'FLMCPSessionInstall'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskArgs
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$FLUser" -LogonType Interactive -RunLevel Highest
    $taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Seconds $SessionTimeoutSeconds)
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $taskPrincipal -Settings $taskSettings -Force | Out-Null
    try {
        Start-ScheduledTask -TaskName $taskName
        $deadline = (Get-Date).AddSeconds($SessionTimeoutSeconds + 30)
        do {
            Start-Sleep -Seconds 5
            $state = (Get-ScheduledTask -TaskName $taskName).State
        } while (($state -eq 'Running' -or -not (Test-Path -LiteralPath $sessionResult)) -and (Get-Date) -lt $deadline)
    } finally {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    if (-not (Test-Path -LiteralPath $sessionResult)) {
        Stop-Install 'session_install' "no result from the $FLUser session within $SessionTimeoutSeconds s (task state $state)"
    }
    $session = Get-Content -LiteralPath $sessionResult -Raw | ConvertFrom-Json
    foreach ($step in $session.steps) {
        [void]$script:steps.Add([ordered]@{ name = $step.name; part = 'session'; status = $step.status; detail = $step.detail })
        if ($step.status -eq 'failed') { $script:failed = $true }
    }
    $script:doctor = $session.doctor
    Add-Step 'session_install' 'ok' "session $($session.session_id) finished; doctor status $(if ($script:doctor) { $script:doctor.status } else { 'missing' })"
} catch {
    if (-not $script:failed) { Add-Step 'unexpected' 'failed' $_.Exception.Message }
}

$ok = Write-Result
Get-Content -LiteralPath $ResultPath -Raw
if ($ok) { Write-Output 'FLMCP_INSTALL_OK'; exit 0 }
Write-Output 'FLMCP_INSTALL_FAILED'
exit 1
