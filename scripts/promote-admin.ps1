[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^\+?[0-9]{6,20}$')]
  [string]$Phone
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "Import-GreenBookEnv.ps1")

$envFile = Join-Path $repoRoot ".env"
Import-GreenBookRootEnv -EnvFile $envFile

$mysqlPassword = Get-GreenBookEnvValue -Name "ZHIGUANG_MYSQL_PASSWORD" -DefaultValue "123456"
$database = Get-GreenBookEnvValue -Name "ZHIGUANG_MYSQL_DB" -DefaultValue "zhiguang"
$safePhone = $Phone.Trim()

$containerId = docker compose -f (Join-Path $repoRoot "docker-compose.yml") ps -q zhiguang-mysql
if ([string]::IsNullOrWhiteSpace($containerId)) {
  throw "zhiguang-mysql is not running. Start middleware with .\scripts\dev-up.ps1 first."
}

$sql = "UPDATE users SET role='ADMIN', updated_at=NOW() WHERE phone='$safePhone'; SELECT id, phone, nickname, role FROM users WHERE phone='$safePhone';"
docker compose -f (Join-Path $repoRoot "docker-compose.yml") exec -T `
  -e "MYSQL_PWD=$mysqlPassword" `
  zhiguang-mysql mysql -uroot --database="$database" --execute="$sql"

if ($LASTEXITCODE -ne 0) {
  throw "Failed to promote the account."
}
