param(
  [switch]$NoReload
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\Import-GreenBookEnv.ps1"
Import-GreenBookRootEnv -EnvFile "$Root\.env"

$defaultModel = Get-GreenBookEnvValue -Name "DEFAULT_MODEL" -DefaultValue "deepseek-v4-flash"
if ($defaultModel -notin @("deepseek-v4-flash", "deepseek-v4-pro")) {
  throw "DEFAULT_MODEL must name a configured real model."
}
if ($defaultModel.StartsWith("deepseek-") -and [string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
  throw "DEEPSEEK_API_KEY is required for $defaultModel."
}
$env:DEFAULT_MODEL = $defaultModel

$authSecret = Get-GreenBookEnvValue -Name "MODERATION_AGENT_AUTH_SECRET" -DefaultValue ""
Assert-GreenBookSecret -Name "MODERATION_AGENT_AUTH_SECRET" -Value $authSecret -ForbiddenValues @("change-me-moderation-secret")

$postgresUser = Get-GreenBookEnvValue -Name "GREENBOOK_POSTGRES_USER" -DefaultValue "mindflow"
$postgresPassword = Get-GreenBookEnvValue -Name "GREENBOOK_POSTGRES_PASSWORD" -DefaultValue "mindflow"
$postgresDatabase = Get-GreenBookEnvValue -Name "MODERATION_POSTGRES_DB" -DefaultValue "content_moderation"
$postgresPort = Get-GreenBookEnvValue -Name "GREENBOOK_POSTGRES_HOST_PORT" -DefaultValue "25432"
$redisPassword = Get-GreenBookEnvValue -Name "GREENBOOK_REDIS_PASSWORD" -DefaultValue "mindflow"
$redisPort = Get-GreenBookEnvValue -Name "GREENBOOK_REDIS_HOST_PORT" -DefaultValue "26379"
$qdrantPort = Get-GreenBookEnvValue -Name "QDRANT_HTTP_HOST_PORT" -DefaultValue "26333"
$moderationPort = Get-GreenBookEnvValue -Name "MODERATION_API_PORT" -DefaultValue "8088"

$env:HOST = "127.0.0.1"
$env:PORT = $moderationPort
$env:MODE = if ($NoReload) { "production" } else { "dev" }
$env:DATABASE_TYPE = "postgres"
$env:POSTGRES_HOST = "127.0.0.1"
$env:POSTGRES_PORT = $postgresPort
$env:POSTGRES_USER = $postgresUser
$env:POSTGRES_PASSWORD = $postgresPassword
$env:POSTGRES_DB = $postgresDatabase
$env:MODERATION_AUTO_CREATE_SCHEMA = "true"
$encodedRedisPassword = [Uri]::EscapeDataString($redisPassword)
$env:REDIS_URL = "redis://:${encodedRedisPassword}@127.0.0.1:${redisPort}/2"
$env:QDRANT_URL = "http://127.0.0.1:${qdrantPort}"
$env:AUTH_SECRET = $authSecret
$env:JAVA_COMMUNITY_AUTH_TOKEN = $env:AUTH_SECRET
$env:COMMUNITY_PROVIDER = "java"
$env:JAVA_COMMUNITY_BASE_URL = "http://127.0.0.1:8080"

$python = "$Root\moderation-agent\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
  throw "Moderation virtual environment is missing. Run .\scripts\setup-dev.ps1 from the repository root."
}

Set-Location "$Root\moderation-agent"
Write-Host "Applying Moderation Agent database migrations..."
& $python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}
Write-Host "Starting Moderation Agent at http://127.0.0.1:$moderationPort"
Write-Host "Press Ctrl+C to stop it. Logs remain in this terminal."
& $python "src\run_service.py"
exit $LASTEXITCODE
