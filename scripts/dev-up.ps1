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

$mode = if ($args.Count -gt 0) { $args[0] } else { "up" }

switch ($mode) {
  "up" {
    Write-Host "Starting GreenBook middleware..."
    docker compose up -d --wait
    docker compose ps
    Write-Host @"

Middleware is ready. Start applications in five separate PowerShell terminals:
  .\scripts\start-be.ps1
  .\scripts\start-creator.ps1
  .\scripts\start-agent.ps1
  .\scripts\start-fe.ps1
"@
  }
  "start" {
    docker compose start
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
