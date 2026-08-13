param(
  [switch]$NoReload,
  [int]$ApiPort = 0,
  [switch]$ApiOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\Import-GreenBookEnv.ps1"
Import-GreenBookRootEnv -EnvFile "$Root\.env"

if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY) -and
    [string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
  throw "DEEPSEEK_API_KEY or OPENAI_API_KEY is required. GreenBook Agent has no mock mode."
}

$agentPort = if ($ApiPort -gt 0) {
  [string]$ApiPort
} else {
  Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_API_PORT" -DefaultValue "8094"
}
$processRole = if ($ApiOnly) {
  "api"
} else {
  (Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_PROCESS_ROLE" -DefaultValue "all").Trim().ToLowerInvariant()
}
if ($processRole -notin @("all", "api")) {
  throw "GREENBOOK_AGENT_PROCESS_ROLE must be 'all' or 'api'."
}
$executionDispatch = (
  Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_EXECUTION_DISPATCH" -DefaultValue "queue"
).Trim().ToLowerInvariant()
if ($executionDispatch -notin @("direct", "queue")) {
  throw "GREENBOOK_AGENT_EXECUTION_DISPATCH must be 'direct' or 'queue'."
}
$manageWorker = $processRole -eq "all" -and $executionDispatch -eq "queue"
$env:GREENBOOK_AGENT_PROCESS_ROLE = $processRole
$env:GREENBOOK_AGENT_IN_PROCESS_WORKER = if ($manageWorker) { "true" } else { "false" }
if ($manageWorker) {
  $env:GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER = "true"
}

$env:GREENBOOK_JAVA_BASE_URL = Get-GreenBookEnvValue -Name "GREENBOOK_JAVA_BASE_URL" -DefaultValue "http://127.0.0.1:8080"
$env:GREENBOOK_CREATOR_BASE_URL = Get-GreenBookEnvValue -Name "GREENBOOK_CREATOR_BASE_URL" -DefaultValue "http://127.0.0.1:8092"
$env:GREENBOOK_AGENT_IDENTITY_ISSUER = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_IDENTITY_ISSUER" -DefaultValue "http://127.0.0.1:8080"
$env:GREENBOOK_AGENT_IDENTITY_AUDIENCE = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_IDENTITY_AUDIENCE" -DefaultValue "greenbook-agent-runtime"
$env:GREENBOOK_AGENT_IDENTITY_JWKS_URL = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_IDENTITY_JWKS_URL" -DefaultValue "http://127.0.0.1:8080/.well-known/jwks.json"
$env:GREENBOOK_AGENT_API_HOST = "127.0.0.1"
$env:GREENBOOK_AGENT_API_PORT = $agentPort
$env:PYTHONPATH = @(
  $Root,
  "$Root\packages\agent_core",
  "$Root\packages\contracts",
  "$Root\packages\java_client",
  "$Root\packages\creator_client",
  "$Root\packages\security",
  "$Root\services\greenbook_mcp",
  "$Root\apps\agent_api"
) -join [IO.Path]::PathSeparator
$env:NO_PROXY = @($env:NO_PROXY, "127.0.0.1", "localhost") -ne "" -join ","
$env:no_proxy = $env:NO_PROXY

$python = "$Root\.venv-v2\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
  throw "GreenBook Agent virtual environment is missing: $python"
}

Set-Location $Root
$agentArguments = @(
  "-m", "uvicorn",
  "apps.agent_api.greenbook_agent_api.main:create_app",
  "--factory",
  "--host", "127.0.0.1",
  "--port", $agentPort
)
if (-not $NoReload) {
  $agentArguments += "--reload"
}

Write-Host "GreenBook Agent API: Runtime API started at http://127.0.0.1:$agentPort"
Write-Host "Creator base URL: $env:GREENBOOK_CREATOR_BASE_URL"
if ($manageWorker) {
  Write-Host "Agent Worker: in-process queue consumer (no static worker token required)"
} elseif ($executionDispatch -eq "queue") {
  Write-Host "Agent Worker: external (GREENBOOK_AGENT_PROCESS_ROLE=api)"
} else {
  Write-Host "Agent execution: direct development mode (no Worker required)"
}

Write-Host "Press Ctrl+C to stop GreenBook Agent services. Logs remain in this terminal."
& $python @agentArguments
exit $LASTEXITCODE
