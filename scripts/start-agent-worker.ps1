param(
  [string]$WorkerAccessToken = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\Import-GreenBookEnv.ps1"
Import-GreenBookRootEnv -EnvFile "$Root\.env"

$configuredInProcess = (Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_IN_PROCESS_WORKER" -DefaultValue "false").Trim().ToLowerInvariant() -in @("1", "true", "yes", "on")
if ($configuredInProcess) {
  throw "Invalid execution topology: GREENBOOK_AGENT_IN_PROCESS_WORKER=true selects the API in-process consumer; do not start a standalone Worker."
}
$env:GREENBOOK_AGENT_IN_PROCESS_WORKER = "false"

$python = "$Root\.venv-v2\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
  throw "GreenBook Agent virtual environment is missing: $python"
}

$workerToken = if (-not [string]::IsNullOrWhiteSpace($WorkerAccessToken)) {
  $WorkerAccessToken.Trim()
} else {
  Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_WORKER_ACCESS_TOKEN" -DefaultValue ""
}
if ([string]::IsNullOrWhiteSpace($workerToken)) {
  throw "GREENBOOK_AGENT_WORKER_ACCESS_TOKEN is required for the durable Execution Queue worker. Configure a service JWT with the greenbook-agent-runtime audience in .env."
}
Assert-GreenBookJwtNotExpired -Name "GREENBOOK_AGENT_WORKER_ACCESS_TOKEN" -Token $workerToken
if (-not [string]::IsNullOrWhiteSpace($WorkerAccessToken)) {
  $env:GREENBOOK_AGENT_WORKER_ACCESS_TOKEN = $workerToken
}

$env:GREENBOOK_JAVA_BASE_URL = Get-GreenBookEnvValue -Name "GREENBOOK_JAVA_BASE_URL" -DefaultValue "http://127.0.0.1:8080"
$env:GREENBOOK_CREATOR_BASE_URL = Get-GreenBookEnvValue -Name "GREENBOOK_CREATOR_BASE_URL" -DefaultValue "http://127.0.0.1:8092"

# Keep a standalone worker launched from PowerShell on the same durable
# profile as the API.  RuntimePersistenceFactory still remains the source of
# truth; these defaults only bridge the root .env naming used by local start
# scripts.
$databaseUrl = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_DATABASE_URL" -DefaultValue ""
$storage = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_RUNTIME_STORAGE" -DefaultValue ""
if ([string]::IsNullOrWhiteSpace($storage) -and -not [string]::IsNullOrWhiteSpace($databaseUrl)) {
  $env:GREENBOOK_AGENT_RUNTIME_STORAGE = "postgres"
  $storage = "postgres"
}
$runtimeDatabaseUrl = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_RUNTIME_DATABASE_URL" -DefaultValue ""
if ($runtimeDatabaseUrl -match '^\$\{[^}]+\}$' -and -not [string]::IsNullOrWhiteSpace($databaseUrl)) {
  $env:GREENBOOK_AGENT_RUNTIME_DATABASE_URL = $databaseUrl
}
$queueConsumer = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER" -DefaultValue ""
if ([string]::IsNullOrWhiteSpace($queueConsumer) -and $storage -eq "postgres") {
  $env:GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER = "true"
}
$env:GREENBOOK_AGENT_WORKER_HEALTH_FILE = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_WORKER_HEALTH_FILE" -DefaultValue ".runtime\agent-worker-health.json"
$env:PYTHONPATH = @(
  $Root,
  "$Root\packages\agent_core",
  "$Root\packages\contracts",
  "$Root\packages\java_client",
  "$Root\packages\security",
  "$Root\services\greenbook_mcp",
  "$Root\apps\agent_api",
  "$Root\apps\agent_worker"
) -join [IO.Path]::PathSeparator
$env:NO_PROXY = @($env:NO_PROXY, "127.0.0.1", "localhost") -ne "" -join ","
$env:no_proxy = $env:NO_PROXY

Set-Location $Root
Write-Host "Agent Worker: Execution consumer started"
Write-Host "Runtime storage: $(if ($storage) { $storage } else { 'factory-default' })"
Write-Host "Execution queue consumer: $(Get-GreenBookEnvValue -Name 'GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER' -DefaultValue 'factory-default')"
Write-Host "Press Ctrl+C to stop it."
& $python -m greenbook_agent_worker.main
exit $LASTEXITCODE
