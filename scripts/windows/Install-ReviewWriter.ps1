[CmdletBinding()]
param(
    [switch]$SkipDependencyInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Venv = Join-Path $Root ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

function Say([string]$Message) { Write-Host "[Review Writer] $Message" }
function Warn([string]$Message) { Write-Warning "[Review Writer] $Message" }

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This script is for Windows. On WSL/Linux use python3 -m venv .venv and the README commands."
}

Set-Location $Root
$pythonExe = $null
$pythonArgs = @()
$py = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $py) {
    try {
        & $py.Source -3.11 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $pythonExe = $py.Source; $pythonArgs = @("-3.11") }
    } catch { }
}
if ($null -eq $pythonExe) {
    $candidate = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $candidate) {
        try {
            & $candidate.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { $pythonExe = $candidate.Source }
        } catch { }
    }
}
if ($null -eq $pythonExe) {
    throw "Python 3.11+ was not found. Install the official Windows Python and rerun this script."
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Say "Creating .venv (existing environments are never removed)."
    & $pythonExe @pythonArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Python venv creation failed." }
} else {
    Say "Using existing .venv."
}

if (-not $SkipDependencyInstall) {
    Say "Installing pinned runtime dependencies into .venv."
    & $VenvPython -m pip install -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed; no environment was removed." }
}

$pdftotext = Get-Command pdftotext -ErrorAction SilentlyContinue
if ($null -eq $pdftotext) {
    Warn "pdftotext was not found on PATH. Install a trusted Poppler for Windows build, add its bin directory to PATH, then rerun Test-ReviewWriterEnvironment.ps1."
} else {
    Say "pdftotext found on PATH."
}

Say "Building the QoderWork CN Expert Kit ZIP."
& $VenvPython (Join-Path $Root "scripts\build_qoderwork_plugin_zip.py")
if ($LASTEXITCODE -ne 0) { throw "QoderWork Expert Kit build failed." }

$diagnostic = Join-Path $Root "scripts\windows\Test-ReviewWriterEnvironment.ps1"
& $diagnostic
$diagnosticCode = $LASTEXITCODE
if ($diagnosticCode -ne 0) {
    Warn "Environment status is HOLD. Resolve the listed dependency/configuration items and rerun the diagnostic."
    exit $diagnosticCode
}
Say "Environment status is READY."
