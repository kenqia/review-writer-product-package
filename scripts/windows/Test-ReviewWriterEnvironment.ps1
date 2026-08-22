[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$checks = [System.Collections.Generic.List[object]]::new()

function Add-Check([string]$Name, [bool]$Ok, [string]$Detail) {
    $checks.Add([pscustomobject]@{ Check = $Name; Status = if ($Ok) { "OK" } else { "HOLD" }; Detail = $Detail })
}
function Add-Notice([string]$Name, [string]$Detail) {
    $checks.Add([pscustomobject]@{ Check = $Name; Status = "NOTICE"; Detail = $Detail })
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    Add-Check "Windows" $false "Run this PowerShell script on Windows; WSL/Linux uses the README shell commands."
} else { Add-Check "Windows" $true "Windows host detected" }

$python = $null
if (Test-Path -LiteralPath $VenvPython) {
    $python = $VenvPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) { $python = $pythonCommand.Source }
}
$pythonOk = $false
if ($python) {
    & $python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" 2>$null
    $pythonOk = ($LASTEXITCODE -eq 0)
}
Add-Check "Python 3.11+" $pythonOk $(if ($pythonOk) { "available (value not echoed)" } else { "install Python 3.11+ and/or run Install-ReviewWriter.ps1" })

$importsOk = $false
if ($pythonOk) {
    & $python -c "import jsonschema, PIL, docx" 2>$null
    $importsOk = ($LASTEXITCODE -eq 0)
}
Add-Check "Python dependencies" $importsOk $(if ($importsOk) { "jsonschema, Pillow, python-docx import successfully" } else { "run Install-ReviewWriter.ps1" })

$pdfTool = Get-Command pdftotext -ErrorAction SilentlyContinue
Add-Check "pdftotext" ($null -ne $pdfTool) $(if ($pdfTool) { "available on PATH" } else { "missing; install trusted Poppler for Windows and add its bin directory to PATH" })

$pluginManifest = Join-Path $Root "qoderwork\plugins\review-writer-cn\.qoder-plugin\plugin.json"
$pluginSkill = Join-Path $Root "qoderwork\plugins\review-writer-cn\skills\review-writer\SKILL.md"
Add-Check "QoderWork Expert Kit layout" ((Test-Path -LiteralPath $pluginManifest) -and (Test-Path -LiteralPath $pluginSkill)) "plugin.json and SKILL.md"
$dashboardServer = Join-Path $Root "view\serve_review_dashboard.py"
$dashboardPage = Join-Path $Root "view\assets\dashboard\review.html"
Add-Check "Dashboard assets" ((Test-Path -LiteralPath $dashboardServer) -and (Test-Path -LiteralPath $dashboardPage)) "local server and review page"

$parserSetting = [Environment]::GetEnvironmentVariable("REVIEW_WRITER_MINERU_PARSER", "Process")
$parserSetting = if ([string]::IsNullOrWhiteSpace($parserSetting)) { [Environment]::GetEnvironmentVariable("REVIEW_WRITER_MINERU_PARSER", "User") } else { $parserSetting }
$parserConfigured = -not [string]::IsNullOrWhiteSpace($parserSetting)
$parserExists = $parserConfigured -and (Test-Path -LiteralPath $parserSetting -PathType Leaf)
if ($parserConfigured) {
    Add-Check "MinerU parser setting" $parserExists $(if ($parserExists) { "configured parser path exists (value hidden)" } else { "configured path is missing (value hidden)" })
} else {
    Add-Notice "MinerU parser setting" "not configured; this run will use an installed parser if discoverable, otherwise truthful pdftotext fallback"
}

$checks | Format-Table -AutoSize | Out-Host
$blocking = @($checks | Where-Object { $_.Status -eq "HOLD" })
if ($blocking.Count -gt 0) { Write-Host "HOLD: fix the listed blocking checks before starting a review."; exit 2 }
Write-Host "READY: local product prerequisites are present. MinerU remains optional and is reported truthfully at parse time."
exit 0
