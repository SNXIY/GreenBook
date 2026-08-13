param(
  [ValidateSet("Direct", "CreatorDraft")]
  [string]$Scenario = "Direct",
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
$CreatorBaseUrl = "http://127.0.0.1:8092"
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
    $parameters.Body = $Body | ConvertTo-Json -Depth 12 -Compress
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

function Find-CreatedDraftId {
  param([Parameter(Mandatory = $true)][object]$Run)

  foreach ($step in @($Run.steps)) {
    if ([string]$step.tool_name -ne "creator.create_draft") {
      continue
    }
    if ($null -ne $step.output.result.draft_id) {
      return [string]$step.output.result.draft_id
    }
    if ($null -ne $step.output.draft_id) {
      return [string]$step.output.draft_id
    }
  }
  return ""
}

Write-Host "Checking live GreenBook services..."
Assert-Health -Name "Java backend" -Uri "$JavaBaseUrl/actuator/health"
Assert-Health -Name "Creator Service" -Uri "$CreatorBaseUrl/actuator/health/ready"
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

$creatorStatus = Invoke-JsonRequest -Method GET `
  -Uri "$CreatorBaseUrl/api/v1/creator/status" `
  -Headers $authHeaders
if ([string]$creatorStatus.status -ne "READY") {
  throw "Creator did not accept the Java JWT."
}

$conversation = Invoke-JsonRequest -Method POST `
  -Uri "$AgentBaseUrl/api/v1/agent/conversations" `
  -Headers $authHeaders `
  -Body @{
    title = "E2E $Scenario"
    surface = "HOME"
  }
$conversationId = [string]$conversation.conversation_id
if ([string]::IsNullOrWhiteSpace($conversationId)) {
  throw "Agent did not create a conversation."
}

$prompt = if ($Scenario -eq "CreatorDraft") {
  "Create a short GREEN-BOOK integration-test draft. Its title must contain E2E-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()). Create the draft only and do not publish it."
}
else {
  "Answer briefly: is the GREEN-BOOK community service online? Do not call any tools."
}

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

$createdDraftId = ""
try {
  Write-Host "Waiting for Agent run $runId..."
  $run = Wait-AgentRun -RunId $runId -Headers $authHeaders
  if ([string]$run.status -ne "COMPLETED") {
    throw "Agent run ended with status $($run.status): $($run.error)"
  }
  if ([string]::IsNullOrWhiteSpace([string]$run.final_response)) {
    throw "Agent completed without a final response."
  }

  if ($Scenario -eq "Direct") {
    if ([string]$run.execution_path -ne "DIRECT") {
      throw "Direct scenario unexpectedly used execution path $($run.execution_path)."
    }
  }
  else {
    if (@("CREATOR", "ORCHESTRATED") -notcontains [string]$run.execution_path) {
      throw "Creator scenario unexpectedly used execution path $($run.execution_path)."
    }
    $createdDraftId = Find-CreatedDraftId -Run $run
    if ([string]::IsNullOrWhiteSpace($createdDraftId)) {
      throw "Creator scenario completed without a Java draft id."
    }
    $status = Invoke-JsonRequest -Method GET `
      -Uri "$JavaBaseUrl/api/v1/knowposts/$createdDraftId/publish-status" `
      -Headers $authHeaders
    if ([string]$status.status -ne "draft") {
      throw "Creator handoff produced unexpected Java status $($status.status)."
    }
  }

  Write-Host "GreenBook $Scenario E2E scenario passed."
  Write-Host "Run: $runId | path: $($run.execution_path) | model calls: $($run.budget.model_calls)"
}
finally {
  if (-not [string]::IsNullOrWhiteSpace($createdDraftId)) {
    Write-Host "Removing E2E draft $createdDraftId..."
    try {
      $null = Invoke-JsonRequest -Method DELETE `
        -Uri "$JavaBaseUrl/api/v1/knowposts/$createdDraftId" `
        -Headers $authHeaders
    }
    catch {
      Write-Warning "Could not remove E2E draft $createdDraftId. Delete it manually."
    }
  }
}
