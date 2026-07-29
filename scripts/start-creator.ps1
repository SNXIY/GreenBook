param(
  [switch]$NoReload
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\Import-GreenBookEnv.ps1"
Import-GreenBookRootEnv -EnvFile "$Root\.env"

$modelProvider = (Get-GreenBookEnvValue -Name "AI_PROVIDER" -DefaultValue "").Trim().ToLowerInvariant()
if ($modelProvider -notin @("deepseek", "openai", "ollama")) {
  throw "AI_PROVIDER must be a real provider: deepseek, openai, or ollama."
}
if ($modelProvider -eq "deepseek" -and [string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
  throw "DEEPSEEK_API_KEY is required when AI_PROVIDER=deepseek."
}
if ($modelProvider -eq "openai" -and [string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
  throw "OPENAI_API_KEY is required when AI_PROVIDER=openai."
}

$communitySecret = Get-GreenBookEnvValue -Name "CREATOR_COMMUNITY_SHARED_SECRET" -DefaultValue ""
$handoffSecret = Get-GreenBookEnvValue -Name "CREATOR_HANDOFF_SHARED_SECRET" -DefaultValue ""
Assert-GreenBookSecret -Name "CREATOR_COMMUNITY_SHARED_SECRET" -Value $communitySecret -ForbiddenValues @("change-me-creator-proxy")
Assert-GreenBookSecret -Name "CREATOR_HANDOFF_SHARED_SECRET" -Value $handoffSecret -ForbiddenValues @("change-me-creator-handoff")

$postgresUser = Get-GreenBookEnvValue -Name "GREENBOOK_POSTGRES_USER" -DefaultValue "mindflow"
$postgresPassword = Get-GreenBookEnvValue -Name "GREENBOOK_POSTGRES_PASSWORD" -DefaultValue "mindflow"
$postgresDatabase = Get-GreenBookEnvValue -Name "CREATOR_POSTGRES_DB" -DefaultValue "mindflow_creator"
$postgresPort = Get-GreenBookEnvValue -Name "GREENBOOK_POSTGRES_HOST_PORT" -DefaultValue "25432"
$redisPassword = Get-GreenBookEnvValue -Name "GREENBOOK_REDIS_PASSWORD" -DefaultValue "mindflow"
$redisPort = Get-GreenBookEnvValue -Name "GREENBOOK_REDIS_HOST_PORT" -DefaultValue "26379"
$qdrantPort = Get-GreenBookEnvValue -Name "QDRANT_HTTP_HOST_PORT" -DefaultValue "26333"
$creatorPort = Get-GreenBookEnvValue -Name "CREATOR_API_PORT" -DefaultValue "8092"

$encodedUser = [Uri]::EscapeDataString($postgresUser)
$encodedPostgresPassword = [Uri]::EscapeDataString($postgresPassword)
$encodedRedisPassword = [Uri]::EscapeDataString($redisPassword)
$env:CREATOR_DATABASE_URL = "postgresql+psycopg://${encodedUser}:${encodedPostgresPassword}@127.0.0.1:${postgresPort}/${postgresDatabase}"
$env:CREATOR_CHECKPOINT_BACKEND = "postgres"
$env:CREATOR_CHECKPOINT_POSTGRES_URL = "postgresql://${encodedUser}:${encodedPostgresPassword}@127.0.0.1:${postgresPort}/${postgresDatabase}"
$env:CREATOR_CHECKPOINT_AUTO_SETUP = "true"
$env:CREATOR_API_EXECUTION_MODE = "local"
$env:CREATOR_API_CREATE_SCHEMA = "false"
$env:REDIS_URL = "redis://:${encodedRedisPassword}@127.0.0.1:${redisPort}/0"
$env:CREATOR_MEMORY_QDRANT_URL = "http://127.0.0.1:${qdrantPort}"
$env:CREATOR_RETRIEVAL_QDRANT_URL = "http://127.0.0.1:${qdrantPort}"
$env:CREATOR_IDENTITY_ISSUER = "http://127.0.0.1:8080"
$env:CREATOR_IDENTITY_AUDIENCE = Get-GreenBookEnvValue -Name "CREATOR_IDENTITY_AUDIENCE" -DefaultValue "creator-agent"
$env:CREATOR_IDENTITY_JWKS_URL = "http://127.0.0.1:8080/.well-known/jwks.json"
$env:CREATOR_IDENTITY_ALLOW_INSECURE_HTTP = "true"
$env:CREATOR_COMMUNITY_PROVIDER = "java"
$env:CREATOR_COMMUNITY_JAVA_BASE_URL = "http://127.0.0.1:8080"
$env:CREATOR_COMMUNITY_JAVA_SHARED_SECRET = $communitySecret
$env:CREATOR_PUBLICATION_JAVA_BASE_URL = "http://127.0.0.1:8080"
$env:CREATOR_PUBLICATION_SHARED_SECRET = $handoffSecret
$env:CREATOR_API_HOST = "127.0.0.1"
$env:CREATOR_API_PORT = $creatorPort
$env:CREATOR_DEV_RELOAD = if ($NoReload) { "false" } else { "true" }

$python = "$Root\creator-agent\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
  throw "Creator virtual environment is missing. Run .\scripts\setup-dev.ps1 from the repository root."
}

Set-Location "$Root\creator-agent"
Write-Host "Applying Creator Agent database migrations..."
& $python -m app.creator.deployment.migrate
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}
Write-Host "Starting Creator Agent at http://127.0.0.1:$creatorPort/creator.html"
Write-Host "Press Ctrl+C to stop it. Logs remain in this terminal."
& $python "run_service.py"
exit $LASTEXITCODE
