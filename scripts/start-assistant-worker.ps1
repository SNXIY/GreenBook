param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\Import-GreenBookEnv.ps1"
Import-GreenBookRootEnv -EnvFile "$Root\.env"

$python = "$Root\.venv-v2\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
  throw "Assistant virtual environment is missing: $python"
}

$workerToken = Get-GreenBookEnvValue -Name "ASSISTANT_WORKER_ACCESS_TOKEN" -DefaultValue ""
if ([string]::IsNullOrWhiteSpace($workerToken)) {
  throw "ASSISTANT_WORKER_ACCESS_TOKEN is required for the durable Execution Queue worker. Configure a service JWT with the greenbook-assistant-runtime audience in .env."
}

# Keep a standalone worker launched from PowerShell on the same durable
# profile as the API.  RuntimePersistenceFactory still remains the source of
# truth; these defaults only bridge the root .env naming used by local start
# scripts.
$databaseUrl = Get-GreenBookEnvValue -Name "ASSISTANT_DATABASE_URL" -DefaultValue ""
$storage = Get-GreenBookEnvValue -Name "ASSISTANT_RUNTIME_STORAGE" -DefaultValue ""
if ([string]::IsNullOrWhiteSpace($storage) -and -not [string]::IsNullOrWhiteSpace($databaseUrl)) {
  $env:ASSISTANT_RUNTIME_STORAGE = "postgres"
  $storage = "postgres"
}
$runtimeDatabaseUrl = Get-GreenBookEnvValue -Name "ASSISTANT_RUNTIME_DATABASE_URL" -DefaultValue ""
if ($runtimeDatabaseUrl -match '^\$\{[^}]+\}$' -and -not [string]::IsNullOrWhiteSpace($databaseUrl)) {
  $env:ASSISTANT_RUNTIME_DATABASE_URL = $databaseUrl
}
$queueConsumer = Get-GreenBookEnvValue -Name "ASSISTANT_EXECUTION_QUEUE_CONSUMER" -DefaultValue ""
if ([string]::IsNullOrWhiteSpace($queueConsumer) -and $storage -eq "postgres") {
  $env:ASSISTANT_EXECUTION_QUEUE_CONSUMER = "true"
}
$env:ASSISTANT_WORKER_HEALTH_FILE = Get-GreenBookEnvValue -Name "ASSISTANT_WORKER_HEALTH_FILE" -DefaultValue ".runtime\assistant-worker-health.json"
$env:PYTHONPATH = @(
  $Root,
  "$Root\packages\assistant_core",
  "$Root\packages\contracts",
  "$Root\packages\java_client",
  "$Root\packages\creator_client",
  "$Root\packages\security",
  "$Root\services\greenbook_mcp",
  "$Root\apps\assistant_api",
  "$Root\apps\assistant_worker"
) -join [IO.Path]::PathSeparator
$env:NO_PROXY = @($env:NO_PROXY, "127.0.0.1", "localhost") -ne "" -join ","
$env:no_proxy = $env:NO_PROXY

Set-Location $Root
Write-Host "Starting Assistant Worker"
Write-Host "Runtime storage: $(if ($storage) { $storage } else { 'factory-default' })"
Write-Host "Execution queue consumer: $(Get-GreenBookEnvValue -Name 'ASSISTANT_EXECUTION_QUEUE_CONSUMER' -DefaultValue 'factory-default')"
Write-Host "Press Ctrl+C to stop it."
& $python -m greenbook_assistant_worker.main
exit $LASTEXITCODE
