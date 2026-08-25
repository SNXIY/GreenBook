param(
  [switch]$HealthOnly,
  [ValidateSet("PHONE", "EMAIL")]
  [string]$IdentifierType = "",
  [string]$Identifier = "",
  [string]$Password = "",
  [ValidateRange(30, 900)]
  [int]$TimeoutSeconds = 240
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"

. (Join-Path $PSScriptRoot "Import-GreenBookEnv.ps1")
if (Test-Path -LiteralPath $EnvFile) {
  Import-GreenBookRootEnv -EnvFile $EnvFile
}

$JavaBaseUrl = "http://127.0.0.1:8080"
$AgentBaseUrl = "http://127.0.0.1:8094"
$FrontendBaseUrl = "http://127.0.0.1:5173"

function Invoke-JsonRequest {
  param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("GET", "POST", "PUT", "PATCH", "DELETE")]
    [string]$Method,
    [Parameter(Mandatory = $true)]
    [string]$Uri,
    [hashtable]$Headers = @{},
    [AllowNull()]
    [object]$Body = $null
  )

  $parameters = @{
    Method = $Method
    Uri = $Uri
    Headers = $Headers
    TimeoutSec = 30
  }
  if ($null -ne $Body) {
    $parameters.ContentType = "application/json; charset=utf-8"
    # Windows PowerShell may encode a string request body through the active
    # console/code-page encoding.  That silently turns non-ASCII input into
    # question marks before FastAPI can parse it.  Send the JSON bytes with an
    # explicit UTF-8 boundary instead.
    $json = $Body | ConvertTo-Json -Depth 12 -Compress
    $parameters.Body = [System.Text.Encoding]::UTF8.GetBytes($json)
  }
  return Invoke-RestMethod @parameters
}

function Assert-Health {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [Parameter(Mandatory = $true)]
    [string]$Uri
  )

  try {
    $null = Invoke-RestMethod -Method Get -Uri $Uri -TimeoutSec 10
    Write-Host "[UP] $Name"
  }
  catch {
    throw "$Name is unavailable at $Uri. $($_.Exception.Message)"
  }
}

function Wait-AgentRun {
  param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,
    [Parameter(Mandatory = $true)]
    [hashtable]$Headers
  )

  $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
  $terminal = @("COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED", "WAITING_APPROVAL")
  do {
    $run = Invoke-JsonRequest -Method GET `
      -Uri "$AgentBaseUrl/api/v1/agent/runs/$RunId" `
      -Headers $Headers
    if ($terminal -contains [string]$run.status) {
      return $run
    }
    Start-Sleep -Milliseconds 750
  } while ([DateTimeOffset]::UtcNow -lt $deadline)

  throw "Agent run $RunId did not finish within $TimeoutSeconds seconds."
}

Write-Host "Checking live GreenBook services..."
Assert-Health -Name "Java backend" -Uri "$JavaBaseUrl/actuator/health"
Assert-Health -Name "GreenBook Agent API" -Uri "$AgentBaseUrl/health"
Assert-Health -Name "Frontend" -Uri $FrontendBaseUrl

if ($HealthOnly) {
  Write-Host "GreenBook live health checks passed."
  exit 0
}

if ([string]::IsNullOrWhiteSpace($IdentifierType)) {
  $IdentifierType = Get-GreenBookEnvValue `
    -Name "GREENBOOK_E2E_IDENTIFIER_TYPE" `
    -DefaultValue "EMAIL"
}
if ([string]::IsNullOrWhiteSpace($Identifier)) {
  $Identifier = Get-GreenBookEnvValue `
    -Name "GREENBOOK_E2E_IDENTIFIER" `
    -DefaultValue ""
}
if ([string]::IsNullOrWhiteSpace($Password)) {
  $Password = Get-GreenBookEnvValue `
    -Name "GREENBOOK_E2E_PASSWORD" `
    -DefaultValue ""
}
if ([string]::IsNullOrWhiteSpace($Identifier) -or [string]::IsNullOrWhiteSpace($Password)) {
  throw "Set GREENBOOK_E2E_IDENTIFIER and GREENBOOK_E2E_PASSWORD in .env, or pass -Identifier and -Password. Use a dedicated USER test account."
}

Write-Host "Authenticating through Java..."
$auth = Invoke-JsonRequest -Method POST `
  -Uri "$JavaBaseUrl/api/v1/auth/login" `
  -Body @{
    identifierType = $IdentifierType
    identifier = $Identifier
    password = $Password
    code = $null
  }
$accessToken = [string]$auth.token.accessToken
if ([string]::IsNullOrWhiteSpace($accessToken)) {
  throw "Java login did not return an access token."
}
$authHeaders = @{ Authorization = "Bearer $accessToken" }

$me = Invoke-JsonRequest -Method GET `
  -Uri "$JavaBaseUrl/api/v1/auth/me" `
  -Headers $authHeaders
if ([string]$me.role -ne "USER") {
  throw "E2E account must have USER role; actual role is $($me.role)."
}

$conversation = Invoke-JsonRequest -Method POST `
  -Uri "$AgentBaseUrl/api/v1/agent/conversations" `
  -Headers $authHeaders `
  -Body @{
    title = "E2E Direct"
    surface = "HOME"
  }
$conversationId = [string]$conversation.conversation_id
if ([string]::IsNullOrWhiteSpace($conversationId)) {
  throw "Agent did not create a conversation."
}

$prompt = "Answer briefly: is the GREEN-BOOK community service online? Do not call any tools."

$accepted = Invoke-JsonRequest -Method POST `
  -Uri "$AgentBaseUrl/api/v1/agent/conversations/$conversationId/messages" `
  -Headers @{
    Authorization = "Bearer $accessToken"
    "Idempotency-Key" = [guid]::NewGuid().ToString("N")
  } `
  -Body @{
    content = $prompt
    client_timezone = "Asia/Shanghai"
  }
$runId = [string]$accepted.run_id
if ([string]::IsNullOrWhiteSpace($runId)) {
  throw "Agent did not accept the E2E run."
}

try {
  Write-Host "Waiting for Agent run $runId..."
  $run = Wait-AgentRun -RunId $runId -Headers $authHeaders
  if ([string]$run.status -ne "COMPLETED") {
    throw "Agent run ended with status $($run.status): $($run.error)"
  }
  if ([string]::IsNullOrWhiteSpace([string]$run.final_response)) {
    throw "Agent completed without a final response."
  }
  if ([string]$run.execution_path -ne "DIRECT") {
    throw "Direct scenario unexpectedly used execution path $($run.execution_path)."
  }

  Write-Host "GreenBook Direct E2E scenario passed."
  Write-Host "Run: $runId | path: $($run.execution_path) | model calls: $($run.budget.model_calls)"
}
finally {
  Write-Host "E2E run finished. No draft was created by this scenario."
}
