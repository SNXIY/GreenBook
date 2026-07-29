param(
  [switch]$NoReload
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\Import-GreenBookEnv.ps1"
Import-GreenBookRootEnv -EnvFile "$Root\.env"

if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
  throw "DEEPSEEK_API_KEY is required. Community Assistant has no mock mode."
}

$sharedSecret = Get-GreenBookEnvValue -Name "ASSISTANT_SERVICE_SHARED_SECRET" -DefaultValue ""
$moderationSecret = Get-GreenBookEnvValue -Name "MODERATION_AGENT_AUTH_SECRET" -DefaultValue ""
Assert-GreenBookSecret -Name "ASSISTANT_SERVICE_SHARED_SECRET" -Value $sharedSecret -ForbiddenValues @("change-me-assistant-service")
Assert-GreenBookSecret -Name "MODERATION_AGENT_AUTH_SECRET" -Value $moderationSecret -ForbiddenValues @("change-me-moderation-secret")

$postgresUser = Get-GreenBookEnvValue -Name "GREENBOOK_POSTGRES_USER" -DefaultValue "mindflow"
$postgresPassword = Get-GreenBookEnvValue -Name "GREENBOOK_POSTGRES_PASSWORD" -DefaultValue "mindflow"
$postgresDatabase = Get-GreenBookEnvValue -Name "CREATOR_POSTGRES_DB" -DefaultValue "mindflow_creator"
$postgresPort = Get-GreenBookEnvValue -Name "GREENBOOK_POSTGRES_HOST_PORT" -DefaultValue "25432"
$redisPassword = Get-GreenBookEnvValue -Name "GREENBOOK_REDIS_PASSWORD" -DefaultValue "mindflow"
$redisPort = Get-GreenBookEnvValue -Name "GREENBOOK_REDIS_HOST_PORT" -DefaultValue "26379"
$assistantPort = Get-GreenBookEnvValue -Name "ASSISTANT_API_PORT" -DefaultValue "8094"

$encodedUser = [Uri]::EscapeDataString($postgresUser)
$encodedPassword = [Uri]::EscapeDataString($postgresPassword)
$encodedRedisPassword = [Uri]::EscapeDataString($redisPassword)
$env:ASSISTANT_DATABASE_URL = "postgresql+asyncpg://${encodedUser}:${encodedPassword}@127.0.0.1:${postgresPort}/${postgresDatabase}"
$env:ASSISTANT_REDIS_URL = "redis://:${encodedRedisPassword}@127.0.0.1:${redisPort}/0"
$env:ASSISTANT_JAVA_BASE_URL = "http://127.0.0.1:8080"
$env:ASSISTANT_CREATOR_BASE_URL = "http://127.0.0.1:8092"
$env:ASSISTANT_IDENTITY_ISSUER = "http://127.0.0.1:8080"
$env:ASSISTANT_IDENTITY_AUDIENCE = "community-assistant-agent"
$env:ASSISTANT_IDENTITY_JWKS_URL = "http://127.0.0.1:8080/.well-known/jwks.json"
$env:ASSISTANT_ALLOW_INSECURE_HTTP = "true"
$env:ASSISTANT_SERVICE_SHARED_SECRET = $sharedSecret
$env:MODERATION_AGENT_AUTH_SECRET = $moderationSecret
$env:ASSISTANT_API_HOST = "127.0.0.1"
$env:ASSISTANT_API_PORT = $assistantPort
$env:ASSISTANT_DEV_RELOAD = if ($NoReload) { "false" } else { "true" }

$python = "$Root\community-assistant-agent\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
  throw "Assistant virtual environment is missing. Run .\scripts\setup-dev.ps1 from the repository root."
}

Set-Location "$Root\community-assistant-agent"
Write-Host "Applying Community Assistant database migrations..."
& $python -m app.migrations
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}
Write-Host "Starting Community Assistant Agent at http://127.0.0.1:$assistantPort"
Write-Host "Press Ctrl+C to stop it. Logs remain in this terminal."
& $python "run_service.py"
exit $LASTEXITCODE
