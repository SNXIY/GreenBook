param(
  [switch]$NoReload,
  [switch]$SkipEnvCheck,
  [int]$StartupTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. "$PSScriptRoot\Import-GreenBookEnv.ps1"
$rootEnvFile = Join-Path $Root ".env"
if (-not (Test-Path -LiteralPath $rootEnvFile)) {
  throw "Environment file not found: $rootEnvFile"
}
Import-GreenBookRootEnv -EnvFile $rootEnvFile

if ($StartupTimeoutSeconds -lt 5) {
  throw "StartupTimeoutSeconds must be at least 5 seconds."
}

if (-not $SkipEnvCheck) {
  & "$PSScriptRoot\check-runtime-env.ps1"
  if ($LASTEXITCODE -ne 0) {
    throw "Runtime environment check failed. No application services were launched."
  }
}

$powershell = (Get-Command powershell.exe -ErrorAction SilentlyContinue).Source
if ([string]::IsNullOrWhiteSpace($powershell)) {
  throw "Windows PowerShell was not found."
}

function Start-GreenBookTerminal {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Script,
    [string[]]$Arguments = @()
  )

  $scriptPath = Join-Path $Root $Script
  if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Startup script not found for ${Name}: $scriptPath"
  }
  $processArguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-NoExit",
    "-File", $scriptPath
  )
  # PowerShell 5.1 can turn an explicitly passed empty string array into a
  # null element.  Start-Process rejects null ArgumentList elements, so only
  # append non-empty optional arguments.
  $optionalArguments = @($Arguments | Where-Object {
      $null -ne $_ -and -not [string]::IsNullOrWhiteSpace([string]$_)
    })
  if ($optionalArguments.Count -gt 0) {
    $processArguments += $optionalArguments
  }
  $process = Start-Process -FilePath $powershell -ArgumentList $processArguments -WorkingDirectory $Root -PassThru
  Write-Host ("{0}: launched (PID {1})" -f $Name, $process.Id)
  return $process
}

function Wait-HttpReady {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Url
  )

  $deadline = [DateTimeOffset]::UtcNow.AddSeconds($StartupTimeoutSeconds)
  while ([DateTimeOffset]::UtcNow -lt $deadline) {
    try {
      $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
      if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
        Write-Host ("{0}: READY" -f $Name)
        return
      }
    } catch { }
    Start-Sleep -Seconds 2
  }
  throw ("{0} did not become ready within {1} seconds: {2}" -f $Name, $StartupTimeoutSeconds, $Url)
}

function Wait-WorkerReady {
  param([Parameter(Mandatory = $true)][string]$HealthFile)

  $resolvedPath = if ([IO.Path]::IsPathRooted($HealthFile)) {
    $HealthFile
  } else {
    Join-Path $Root $HealthFile
  }
  $deadline = [DateTimeOffset]::UtcNow.AddSeconds($StartupTimeoutSeconds)
  while ([DateTimeOffset]::UtcNow -lt $deadline) {
    try {
      if (Test-Path -LiteralPath $resolvedPath) {
        $payload = Get-Content -LiteralPath $resolvedPath -Raw | ConvertFrom-Json
        if ([string]$payload.status -eq "READY") {
          Write-Host "Agent Worker: READY"
          return
        }
      }
    } catch { }
    Start-Sleep -Seconds 2
  }
  throw ("Agent Worker did not become ready within {0} seconds: {1}" -f $StartupTimeoutSeconds, $resolvedPath)
}

$noReloadArguments = if ($NoReload) { @("-NoReload") } else { @() }
$agentPort = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_API_PORT" -DefaultValue "8094"
$mcpPort = Get-GreenBookEnvValue -Name "GREENBOOK_MCP_PORT" -DefaultValue "8095"
$javaBaseUrl = Get-GreenBookEnvValue -Name "GREENBOOK_JAVA_BASE_URL" -DefaultValue "http://127.0.0.1:8080"
$agentBaseUrl = "http://127.0.0.1:" + $agentPort
$mcpBaseUrl = "http://127.0.0.1:" + $mcpPort
$env:GREENBOOK_MCP_PORT = $mcpPort
$env:GREENBOOK_BUSINESS_MCP_BASE_URL = Get-GreenBookEnvValue -Name "GREENBOOK_BUSINESS_MCP_BASE_URL" -DefaultValue ($mcpBaseUrl + "/mcp")
$mcpTransport = (Get-GreenBookEnvValue -Name "GREENBOOK_MCP_TRANSPORT" -DefaultValue "mcp").Trim().ToLowerInvariant()
if ($mcpTransport -notin @("mcp", "local")) {
  throw "GREENBOOK_MCP_TRANSPORT must be 'mcp' or 'local'."
}
$env:GREENBOOK_MCP_TRANSPORT = $mcpTransport
$executionDispatch = (Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_EXECUTION_DISPATCH" -DefaultValue "queue").Trim().ToLowerInvariant()
if ($executionDispatch -notin @("queue", "direct")) {
  throw "GREENBOOK_AGENT_EXECUTION_DISPATCH must be 'queue' or 'direct'."
}
$queueMode = $executionDispatch -eq "queue"
$workerHealthFile = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_WORKER_HEALTH_FILE" -DefaultValue ".runtime\agent-worker-health.json"
$inProcessWorker = (Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_IN_PROCESS_WORKER" -DefaultValue "false").Trim().ToLowerInvariant() -in @("1", "true", "yes", "on")
if (-not $queueMode) {
  $inProcessWorker = $false
}
$agentArguments = if ($queueMode -and $inProcessWorker) {
  $noReloadArguments
} else {
  @("-ApiOnly") + $noReloadArguments
}
$env:GREENBOOK_AGENT_PROCESS_ROLE = if ($queueMode -and $inProcessWorker) { "all" } else { "api" }
$env:GREENBOOK_AGENT_IN_PROCESS_WORKER = if ($inProcessWorker) { "true" } else { "false" }

Write-Host "Starting GreenBook Runtime development services..."
$null = Start-GreenBookTerminal -Name "Java Backend" -Script "scripts\start-be.ps1"
Wait-HttpReady -Name "Java Backend" -Url ($javaBaseUrl.TrimEnd("/") + "/actuator/health")

$null = Start-GreenBookTerminal -Name "GreenBook Business MCP" -Script "scripts\start-mcp.ps1" -Arguments @("-NoReload")
Wait-HttpReady -Name "GreenBook Business MCP" -Url ($mcpBaseUrl + "/health")

$null = Start-GreenBookTerminal -Name "Agent API" -Script "scripts\start-agent.ps1" -Arguments $agentArguments
Wait-HttpReady -Name "Agent API" -Url ($agentBaseUrl + "/health")

if ($queueMode -and -not $inProcessWorker) {
  $null = Start-GreenBookTerminal -Name "Agent Worker" -Script "scripts\start-agent-worker.ps1"
  Wait-WorkerReady -HealthFile $workerHealthFile
}

$null = Start-GreenBookTerminal -Name "Frontend" -Script "scripts\start-fe.ps1"

Write-Host ""
if ($queueMode) {
  if ($inProcessWorker) {
    Write-Host "Services launched. Agent API owns the in-process queue consumer (no standalone Worker)."
  } else {
    Write-Host "Services launched. Agent API and Worker run as separate processes over the durable queue."
  }
} else {
  Write-Host "Services launched. Agent is using direct development dispatch; no Worker is required."
}
Write-Host "Use .\scripts\check-runtime-status.ps1 to inspect API, Worker, Queue, Database and Java."
Write-Host "Close the service windows individually when you want to stop development."
& "$PSScriptRoot\check-runtime-status.ps1"
if ($LASTEXITCODE -ne 0) {
  Write-Warning "Some services are still starting or unavailable; inspect the individual service windows."
}
