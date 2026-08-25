param(
  [string]$EnvFile = "",
  [int]$TimeoutSeconds = 2
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\Import-GreenBookEnv.ps1"

if ([string]::IsNullOrWhiteSpace($EnvFile)) {
  $EnvFile = Join-Path $Root ".env"
}
if (-not (Test-Path -LiteralPath $EnvFile)) {
  throw "Environment file not found: $EnvFile"
}
Import-GreenBookRootEnv -EnvFile (Resolve-Path -LiteralPath $EnvFile).Path

function Get-Value {
  param([string[]]$Names, [string]$Default)
  foreach ($name in $Names) {
    $value = Get-GreenBookEnvValue -Name $name -DefaultValue ""
    if ($value) { return $value.Trim() }
  }
  return $Default
}

function Test-HttpReady {
  param([string]$BaseUrl, [string[]]$Paths)
  foreach ($path in $Paths) {
    try {
      $response = Invoke-WebRequest -Uri ($BaseUrl.TrimEnd("/") + $path) -UseBasicParsing -TimeoutSec $TimeoutSeconds
      if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { return $true }
    } catch { }
  }
  return $false
}

function Test-TcpReady {
  param([string]$DatabaseUrl)
  try {
    $uri = [Uri]$DatabaseUrl
    $port = if ($uri.Port -gt 0) { $uri.Port } else { 5432 }
    $client = [Net.Sockets.TcpClient]::new()
    $async = $client.ConnectAsync($uri.Host, $port)
    if (-not $async.Wait($TimeoutSeconds * 1000)) { $client.Dispose(); return $false }
    $ready = $client.Connected
    $client.Dispose()
    return $ready
  } catch { return $false }
}

function Test-WorkerReady {
  $healthPath = Get-Value -Names @("GREENBOOK_AGENT_WORKER_HEALTH_FILE") -Default ".runtime\agent-worker-health.json"
  $resolved = if ([IO.Path]::IsPathRooted($healthPath)) { $healthPath } else { Join-Path $Root $healthPath }
  try {
    $payload = Get-Content -LiteralPath $resolved -Raw | ConvertFrom-Json
    $updated = [DateTimeOffset]::Parse([string]$payload.updated_at)
    $maxAge = [double](Get-Value -Names @("GREENBOOK_AGENT_WORKER_HEALTH_MAX_AGE_SECONDS") -Default "90")
    $fresh = (([DateTimeOffset]::UtcNow - $updated.ToUniversalTime()).TotalSeconds -le $maxAge)
    return ($payload.status -eq "READY" -and $fresh)
  } catch { return $false }
}

$apiBaseUrl = "http://127.0.0.1:" + (Get-Value -Names @("GREENBOOK_AGENT_API_PORT") -Default "8094")
$javaBaseUrl = Get-Value -Names @("GREENBOOK_JAVA_BASE_URL") -Default "http://127.0.0.1:8080"
$databaseUrl = Get-Value -Names @("GREENBOOK_AGENT_DATABASE_URL") -Default ""
$dispatch = (Get-Value -Names @("GREENBOOK_AGENT_EXECUTION_DISPATCH") -Default "direct").ToLowerInvariant()
$queueMode = $dispatch -eq "queue"
$queueEnabled = $queueMode -and ((Get-Value -Names @("GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER") -Default "false").ToLowerInvariant() -in @("1", "true", "yes", "on"))
$apiReady = Test-HttpReady -BaseUrl $apiBaseUrl -Paths @("/health")
$processRole = (Get-Value -Names @("GREENBOOK_AGENT_PROCESS_ROLE") -Default "api").ToLowerInvariant()
$inProcessWorker = $queueMode -and $processRole -eq "all"
$workerReady = if (-not $queueMode) {
  $true
} elseif ($inProcessWorker) {
  $apiReady
} else {
  Test-WorkerReady
}
$javaReady = Test-HttpReady -BaseUrl $javaBaseUrl -Paths @("/actuator/health")
$databaseReady = Test-TcpReady -DatabaseUrl $databaseUrl
$queueReady = $workerReady -and $queueEnabled

Write-Host "GREENBOOK Runtime Status"
Write-Host ("API:      " + $(if ($apiReady) { "READY" } else { "UNAVAILABLE" }))
Write-Host ("Worker:   " + $(if (-not $queueMode) { "NOT REQUIRED" } elseif ($workerReady) { "READY" } else { "UNAVAILABLE" }))
Write-Host ("Queue:    " + $(if (-not $queueMode) { "NOT REQUIRED" } elseif ($queueReady) { "READY" } else { "UNAVAILABLE" }))
Write-Host ("Database: " + $(if ($databaseReady) { "READY" } else { "UNAVAILABLE" }))
Write-Host ("Java:     " + $(if ($javaReady) { "READY" } else { "UNAVAILABLE" }))

$dispatchReady = -not $queueMode -or ($workerReady -and $queueReady)
if ($apiReady -and $dispatchReady -and $databaseReady) { exit 0 }
exit 1
