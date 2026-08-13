param(
  [string]$EnvFile = ""
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

$errors = [System.Collections.Generic.List[string]]::new()
$warnings = [System.Collections.Generic.List[string]]::new()

function Get-ConfiguredValue {
  param(
    [string[]]$Names,
    [string]$Label,
    [switch]$Required
  )

  foreach ($name in $Names) {
    $value = Get-GreenBookEnvValue -Name $name -DefaultValue ""
    if (-not [string]::IsNullOrWhiteSpace($value)) {
      return $value.Trim()
    }
  }
  if ($Required) {
    $errors.Add("$Label is missing. Configure $($Names -join ' or ') in .env.")
  }
  return ""
}

function Assert-HttpUrl {
  param([string]$Name, [string]$Value)
  if ([string]::IsNullOrWhiteSpace($Value)) { return }
  $parsed = $null
  if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$parsed) -or $parsed.Scheme -notin @("http", "https")) {
    $errors.Add("$Name must be an absolute http(s) URL: $Value")
  }
}

$databaseUrl = Get-ConfiguredValue -Names @("GREENBOOK_AGENT_DATABASE_URL") -Label "GREENBOOK_AGENT_DATABASE_URL" -Required
$dispatch = Get-ConfiguredValue -Names @("GREENBOOK_AGENT_EXECUTION_DISPATCH") -Label "GREENBOOK_AGENT_EXECUTION_DISPATCH" -Required
$queueConsumer = Get-ConfiguredValue -Names @("GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER") -Label "GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER"
$workerToken = Get-ConfiguredValue -Names @("GREENBOOK_AGENT_WORKER_ACCESS_TOKEN") -Label "GREENBOOK_AGENT_WORKER_ACCESS_TOKEN"
$processRole = (Get-ConfiguredValue -Names @("GREENBOOK_AGENT_PROCESS_ROLE") -Label "GREENBOOK_AGENT_PROCESS_ROLE").ToLowerInvariant()
$inProcessWorker = (Get-ConfiguredValue -Names @("GREENBOOK_AGENT_IN_PROCESS_WORKER") -Label "GREENBOOK_AGENT_IN_PROCESS_WORKER").ToLowerInvariant()
$javaBaseUrl = Get-ConfiguredValue -Names @("GREENBOOK_JAVA_BASE_URL") -Label "GREENBOOK_JAVA_BASE_URL" -Required
$creatorBaseUrl = Get-ConfiguredValue -Names @("GREENBOOK_CREATOR_BASE_URL") -Label "GREENBOOK_CREATOR_BASE_URL" -Required

if ($dispatch -and $dispatch.ToLowerInvariant() -notin @("direct", "queue")) {
  $errors.Add("GREENBOOK_AGENT_EXECUTION_DISPATCH must be 'direct' or 'queue'.")
}
$queueMode = $dispatch -and $dispatch.ToLowerInvariant() -eq "queue"
if ($queueMode -and [string]::IsNullOrWhiteSpace($queueConsumer)) {
  $errors.Add("GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER is required in queue mode.")
} elseif ($queueMode -and $queueConsumer.ToLowerInvariant() -notin @("1", "true", "yes", "on")) {
  $errors.Add("GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER must be true in queue mode.")
}
if (-not $processRole) { $processRole = "all" }
$localConsumer = $queueMode -and (
  $processRole -eq "all" -or $inProcessWorker -in @("1", "true", "yes", "on")
)
if ($queueMode -and -not $localConsumer -and [string]::IsNullOrWhiteSpace($workerToken)) {
  $errors.Add("GREENBOOK_AGENT_WORKER_ACCESS_TOKEN is required in queue mode.")
}
if ($databaseUrl -and $databaseUrl -notmatch '^postgres(?:ql)?(?:\+[A-Za-z0-9_]+)?://') {
  $errors.Add("GREENBOOK_AGENT_DATABASE_URL must use a PostgreSQL URL scheme.")
}
Assert-HttpUrl -Name "GREENBOOK_JAVA_BASE_URL" -Value $javaBaseUrl
Assert-HttpUrl -Name "GREENBOOK_CREATOR_BASE_URL" -Value $creatorBaseUrl

Write-Host "GREENBOOK Runtime Environment"
Write-Host "Database: $(if ($databaseUrl) { 'CONFIGURED' } else { 'MISSING' })"
Write-Host "Dispatch: $dispatch"
Write-Host "Queue consumer: $(if ($queueMode) { $queueConsumer } else { 'NOT REQUIRED' })"
Write-Host "Worker token: $(if (-not $queueMode -or $localConsumer) { 'NOT REQUIRED' } elseif ($workerToken) { 'CONFIGURED' } else { 'MISSING' })"
Write-Host "Java: $javaBaseUrl"
Write-Host "Creator: $creatorBaseUrl"

foreach ($warning in $warnings) { Write-Warning $warning }
if ($errors.Count -gt 0) {
  Write-Error ("Runtime environment check failed:`n - " + ($errors -join "`n - "))
  exit 1
}
Write-Host "Environment check: READY"
exit 0
