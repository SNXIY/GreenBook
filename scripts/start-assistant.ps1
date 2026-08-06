param(
  [switch]$NoReload,
  [int]$ApiPort = 0
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\Import-GreenBookEnv.ps1"
Import-GreenBookRootEnv -EnvFile "$Root\.env"

if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY) -and
    [string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
  throw "DEEPSEEK_API_KEY or OPENAI_API_KEY is required. Assistant has no mock mode."
}

$assistantPort = if ($ApiPort -gt 0) {
  [string]$ApiPort
} else {
  Get-GreenBookEnvValue -Name "ASSISTANT_API_PORT" -DefaultValue "8094"
}

$env:ASSISTANT_JAVA_BASE_URL = Get-GreenBookEnvValue -Name "ASSISTANT_JAVA_BASE_URL" -DefaultValue "http://127.0.0.1:8080"
$env:ASSISTANT_CREATOR_BASE_URL = Get-GreenBookEnvValue -Name "ASSISTANT_CREATOR_BASE_URL" -DefaultValue "http://127.0.0.1:8092"
$env:ASSISTANT_IDENTITY_ISSUER = Get-GreenBookEnvValue -Name "ASSISTANT_IDENTITY_ISSUER" -DefaultValue "http://127.0.0.1:8080"
$env:ASSISTANT_IDENTITY_AUDIENCE = Get-GreenBookEnvValue -Name "ASSISTANT_IDENTITY_AUDIENCE" -DefaultValue "community-assistant-agent"
$env:ASSISTANT_IDENTITY_JWKS_URL = Get-GreenBookEnvValue -Name "ASSISTANT_IDENTITY_JWKS_URL" -DefaultValue "http://127.0.0.1:8080/.well-known/jwks.json"
$env:ASSISTANT_API_HOST = "127.0.0.1"
$env:ASSISTANT_API_PORT = $assistantPort
$env:PYTHONPATH = @(
  $Root,
  "$Root\packages\assistant_core",
  "$Root\packages\contracts",
  "$Root\packages\java_client",
  "$Root\packages\creator_client",
  "$Root\packages\security",
  "$Root\services\greenbook_mcp",
  "$Root\apps\assistant_api"
) -join [IO.Path]::PathSeparator
$env:NO_PROXY = @($env:NO_PROXY, "127.0.0.1", "localhost") -ne "" -join ","
$env:no_proxy = $env:NO_PROXY

$python = "$Root\.venv-v2\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
  throw "Assistant virtual environment is missing: $python"
}

Set-Location $Root
$assistantArguments = @(
  "-m", "uvicorn",
  "apps.assistant_api.greenbook_assistant_api.main:create_app",
  "--factory",
  "--host", "127.0.0.1",
  "--port", $assistantPort
)
if (-not $NoReload) {
  $assistantArguments += "--reload"
}

Write-Host "Starting Assistant API at http://127.0.0.1:$assistantPort"
Write-Host "Creator base URL: $env:ASSISTANT_CREATOR_BASE_URL"
Write-Host "Press Ctrl+C to stop it. Logs remain in this terminal."
& $python @assistantArguments
exit $LASTEXITCODE
