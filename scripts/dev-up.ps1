# GreenBook development infrastructure helper (Windows PowerShell).
# Application services deliberately run on the host in separate terminals.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. "$PSScriptRoot\Import-GreenBookEnv.ps1"

if (-not (Test-Path "$Root\.env")) {
  Copy-Item "$Root\.env.example" "$Root\.env"
  Write-Host "Created .env from .env.example"
}
Import-GreenBookRootEnv -EnvFile "$Root\.env"

function Invoke-GreenBookMigrations {
  $mysqlPassword = Get-GreenBookEnvValue -Name "ZHIGUANG_MYSQL_PASSWORD" -DefaultValue "123456"
  $mysqlDatabase = Get-GreenBookEnvValue -Name "ZHIGUANG_MYSQL_DB" -DefaultValue "zhiguang"
  Write-Host "Applying idempotent Java database migrations..."
  $migrationFiles = @(
    "assistant_comment_migration.sql",
    "assistant_capability_migration.sql"
  )
  foreach ($migrationFile in $migrationFiles) {
    Get-Content -Raw -Encoding utf8 "$Root\zhiguang-be\db\$migrationFile" |
      docker compose exec -T -e "MYSQL_PWD=$mysqlPassword" zhiguang-mysql mysql -uroot $mysqlDatabase
    if ($LASTEXITCODE -ne 0) {
      throw "Failed to apply $migrationFile"
    }
  }
}

$mode = if ($args.Count -gt 0) { $args[0] } else { "up" }

switch ($mode) {
  "up" {
    Write-Host "Starting GreenBook middleware..."
    docker compose up -d --wait
    Invoke-GreenBookMigrations
    docker compose ps
    Write-Host @"

Middleware is ready. Start applications in five separate PowerShell terminals:
  .\scripts\start-be.ps1
  .\scripts\start-creator.ps1
  .\scripts\start-moderation.ps1
  .\scripts\start-assistant.ps1
  .\scripts\start-fe.ps1
"@
  }
  "start" {
    docker compose start
    Invoke-GreenBookMigrations
    docker compose ps
  }
  "stop" {
    docker compose stop
  }
  "down" {
    docker compose down
  }
  "status" {
    docker compose ps
  }
  "logs" {
    docker compose logs -f
  }
  default {
    Write-Host "Usage: .\scripts\dev-up.ps1 [up|start|stop|down|status|logs]"
    exit 1
  }
}
