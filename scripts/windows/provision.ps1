[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$FLRoot = "$env:ProgramFiles\Image-Line\FL Studio",
    [string]$BridgeDll = (Join-Path $RepoRoot 'native\build-windows\Release\FL MCP Bridge_x64.dll'),
    [string]$SettingsDir = "$env:USERPROFILE\Documents\Image-Line\FL Studio\Settings",
    [string]$Build,
    [switch]$SkipPackageInstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Install-PackageIfMissing([string]$Name) {
    if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
        throw 'Chocolatey is required. Install it first, then rerun this script.'
    }
    $installed = choco list --local-only --exact $Name --limit-output
    if ($installed -notmatch "^$([regex]::Escape($Name))\|") {
        choco install $Name --yes --no-progress
        if ($LASTEXITCODE -notin 0, 3010) { throw "Chocolatey failed to install $Name (exit $LASTEXITCODE)." }
    }
}

function Require-Path([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "$Description not found: $Path" }
}

if (-not $IsWindows) { throw 'provision.ps1 runs only on Windows.' }
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run from an elevated PowerShell prompt.' }

$flExe = Join-Path $FLRoot 'FL64.exe'
Require-Path $flExe 'FL Studio executable'
Require-Path $BridgeDll 'Bridge DLL'
Require-Path (Join-Path $RepoRoot 'fl-scripts\controller\device_FLMCP.py') 'Controller script'
Require-Path (Join-Path $SettingsDir 'reg.xml') 'FL MIDI registry (launch FL once before provisioning)'

if (-not $SkipPackageInstall) {
    Install-PackageIfMissing 'loopmidi'
    Install-PackageIfMissing 'vb-cable'
    if (-not (Get-Module -ListAvailable -Name AudioDeviceCmdlets)) {
        Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force | Out-Null
        Install-Module -Name AudioDeviceCmdlets -Scope AllUsers -Force -AllowClobber
    }
}

# loopMIDI has no supported noninteractive port-creation interface. Its named
# ports are durable driver state, so the script refuses to pretend they exist.
$requiredPorts = 'FLMCP Req', 'FLMCP Res'
$loopMidi = Get-Process loopMIDI -ErrorAction SilentlyContinue
if (-not $loopMidi) {
    Start-Process (Get-Command loopMIDI.exe -ErrorAction Stop).Source
    throw "Create these persistent loopMIDI ports once, then rerun: $($requiredPorts -join ', ')."
}

Import-Module AudioDeviceCmdlets
$cable = Get-AudioDevice -List | Where-Object { $_.Name -eq 'CABLE Input (VB-Audio Virtual Cable)' }
if (-not $cable) { throw 'VB-CABLE playback device was not found after installation.' }
Set-AudioDevice -Index $cable.Index

$controllerTarget = Join-Path $SettingsDir 'Hardware\FLMCP\device_FLMCP.py'
New-Item -ItemType Directory -Force -Path (Split-Path $controllerTarget) | Out-Null
Copy-Item (Join-Path $RepoRoot 'fl-scripts\controller\device_FLMCP.py') $controllerTarget -Force
$pluginDir = Join-Path $FLRoot 'Plugins\Fruity\Effects\FL MCP Bridge'
New-Item -ItemType Directory -Force -Path $pluginDir | Out-Null
Copy-Item $BridgeDll (Join-Path $pluginDir 'FL MCP Bridge_x64.dll') -Force
Set-Content (Join-Path $pluginDir 'Plugin.nfo') 'ps_Vendorname=FL MCP' -NoNewline

if (-not $Build) {
    $version = (Get-Item $flExe).VersionInfo.ProductVersion
    if ($version -notmatch '^(\d+\.\d+\.\d+)\.(\d+)') { throw "Cannot parse FL build from $version. Pass -Build explicitly." }
    $Build = "FL $($Matches[1]) build $($Matches[2]) (x86_64)"
}
uv run --directory (Join-Path $RepoRoot 'mcp-server') python (Join-Path $RepoRoot 'scripts\windows\provisioning.py') `
    --registry (Join-Path $SettingsDir 'reg.xml') --installed-for (Join-Path $pluginDir 'bridge_installed_for.txt') --build $Build
if ($LASTEXITCODE -ne 0) { throw 'Could not write the FL MIDI profile gate record.' }

Write-Host "Provisioned FL MCP for $Build. FL Studio ASIO will open VB-CABLE through Windows' default playback device."
