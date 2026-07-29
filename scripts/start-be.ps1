param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\Import-GreenBookEnv.ps1"
Import-GreenBookRootEnv -EnvFile "$Root\.env"

& "$PSScriptRoot\ensure-jwt-keys.ps1"
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

$jwtSecret = Get-GreenBookEnvValue -Name "JWT_SECRET" -DefaultValue ""
$moderationSecret = Get-GreenBookEnvValue -Name "MODERATION_AGENT_AUTH_SECRET" -DefaultValue ""
$creatorHandoffSecret = Get-GreenBookEnvValue -Name "CREATOR_HANDOFF_SHARED_SECRET" -DefaultValue ""
$creatorAgentSecret = Get-GreenBookEnvValue -Name "CREATOR_AGENT_SHARED_SECRET" -DefaultValue ""
$assistantSecret = Get-GreenBookEnvValue -Name "ASSISTANT_SERVICE_SHARED_SECRET" -DefaultValue ""
Assert-GreenBookSecret -Name "JWT_SECRET" -Value $jwtSecret -ForbiddenValues @("change-me-to-a-long-random-string")
Assert-GreenBookSecret -Name "MODERATION_AGENT_AUTH_SECRET" -Value $moderationSecret -ForbiddenValues @("change-me-moderation-secret")
Assert-GreenBookSecret -Name "CREATOR_HANDOFF_SHARED_SECRET" -Value $creatorHandoffSecret -ForbiddenValues @("change-me-creator-handoff")
Assert-GreenBookSecret -Name "CREATOR_AGENT_SHARED_SECRET" -Value $creatorAgentSecret -ForbiddenValues @("change-me-creator-proxy")
Assert-GreenBookSecret -Name "ASSISTANT_SERVICE_SHARED_SECRET" -Value $assistantSecret -ForbiddenValues @("change-me-assistant-service")

$env:SPRING_PROFILES_ACTIVE = ""
$env:MYSQL_HOST = "127.0.0.1"
$env:MYSQL_PORT = Get-GreenBookEnvValue -Name "ZHIGUANG_MYSQL_HOST_PORT" -DefaultValue "33306"
$env:MYSQL_DB = Get-GreenBookEnvValue -Name "ZHIGUANG_MYSQL_DB" -DefaultValue "zhiguang"
$env:MYSQL_USER = "root"
$env:MYSQL_PASSWORD = Get-GreenBookEnvValue -Name "ZHIGUANG_MYSQL_PASSWORD" -DefaultValue "123456"
$env:REDIS_HOST = "127.0.0.1"
$env:REDIS_PORT = Get-GreenBookEnvValue -Name "GREENBOOK_REDIS_HOST_PORT" -DefaultValue "26379"
$env:REDIS_PASSWORD = Get-GreenBookEnvValue -Name "GREENBOOK_REDIS_PASSWORD" -DefaultValue "mindflow"
$env:REDIS_DATABASE = "1"
$env:KAFKA_HOST = "127.0.0.1"
$env:KAFKA_PORT = Get-GreenBookEnvValue -Name "ZHIGUANG_KAFKA_HOST_PORT" -DefaultValue "39092"
$env:CANAL_ENABLED = Get-GreenBookEnvValue -Name "CANAL_ENABLED" -DefaultValue "false"
$env:JWT_ISSUER = Get-GreenBookEnvValue -Name "JWT_ISSUER" -DefaultValue "http://127.0.0.1:8080"
$env:LOCAL_STORAGE_PUBLIC_BASE_URL = Get-GreenBookEnvValue -Name "JAVA_PUBLIC_BASE_URL" -DefaultValue "http://127.0.0.1:8080"
$env:MODERATION_AGENT_BASE_URL = Get-GreenBookEnvValue -Name "MODERATION_AGENT_BASE_URL" -DefaultValue "http://127.0.0.1:8088"
$env:JWT_SECRET = $jwtSecret
$env:MODERATION_AGENT_AUTH_SECRET = $moderationSecret
$env:CREATOR_HANDOFF_SHARED_SECRET = $creatorHandoffSecret
$env:CREATOR_AGENT_SHARED_SECRET = $creatorAgentSecret
$env:ASSISTANT_SERVICE_SHARED_SECRET = $assistantSecret

if (-not (Get-Command mvn -ErrorAction SilentlyContinue)) {
  throw "Maven was not found. Install Maven 3.9+ and ensure 'mvn' is available in PATH."
}

Set-Location "$Root\zhiguang-be"
Write-Host "Starting Java backend at http://127.0.0.1:8080"
Write-Host "Press Ctrl+C to stop it. Logs remain in this terminal."
& mvn spring-boot:run
exit $LASTEXITCODE
