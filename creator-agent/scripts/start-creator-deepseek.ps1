[CmdletBinding()]
param(
    [string]$Model = "deepseek-v4-flash",
    [switch]$Thinking,
    [int]$Port = 8080,
    [switch]$Restart,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python virtual environment not found: $Python"
}

$env:AI_PROVIDER = "deepseek"
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
$env:DEEPSEEK_MODEL = $Model
$env:DEEPSEEK_THINKING_ENABLED = $Thinking.IsPresent.ToString().ToLowerInvariant()

Push-Location $ProjectRoot
try {
    $Preflight = @"
from app.core.config import Settings
from app.creator.runtime.composition import validate_creator_model_settings

settings = Settings()
validate_creator_model_settings(settings)
if (
    settings.creator_identity_mode.strip().lower() == 'basic'
    and not settings.creator_basic_password
):
    raise ValueError('CREATOR_BASIC_PASSWORD is missing from .env')
"@
    & $Python -c $Preflight
    $PreflightExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
if ($PreflightExitCode -ne 0) {
    throw "DeepSeek configuration preflight failed. Check the project .env file."
}
if ($CheckOnly) {
    Write-Host "DeepSeek configuration is valid."
    return
}

$Listener = Get-NetTCPConnection `
    -LocalAddress 127.0.0.1 `
    -LocalPort $Port `
    -State Listen `
    -ErrorAction SilentlyContinue

if ($Listener) {
    $Owner = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $($Listener.OwningProcess)"
    $ExpectedCommand = "uvicorn app.main:app.*--port $Port"
    if (-not $Restart) {
        throw (
            "Port $Port is already in use by PID $($Listener.OwningProcess). " +
            "Run again with -Restart to replace the existing Creator service."
        )
    }
    if ($Owner.CommandLine -notmatch $ExpectedCommand) {
        throw "Refusing to stop an unrelated process: $($Owner.CommandLine)"
    }
    Stop-Process -Id $Listener.OwningProcess -Force
    Start-Sleep -Milliseconds 750
}

Write-Host "Starting MindFlow Creator with $Model on http://127.0.0.1:$Port"
Write-Host "Thinking mode: $($env:DEEPSEEK_THINKING_ENABLED)"

Push-Location $ProjectRoot
try {
    & $Python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
}
finally {
    Pop-Location
}
