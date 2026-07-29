param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Invoke-ProjectCheck {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [scriptblock]$Command
  )

  Write-Host ""
  Write-Host "=== $Name ==="
  Push-Location $Path
  try {
    & $Command
    if ($LASTEXITCODE -ne 0) {
      throw "$Name failed with exit code $LASTEXITCODE."
    }
  }
  finally {
    Pop-Location
  }
}

Write-Host "=== Docker Compose configuration ==="
docker compose --project-directory $Root config --quiet
if ($LASTEXITCODE -ne 0) {
  throw "Docker Compose validation failed."
}

Invoke-ProjectCheck -Name "Java backend tests" -Path "$Root\zhiguang-be" -Command {
  mvn -q test
}

Invoke-ProjectCheck -Name "Frontend typecheck and build" -Path "$Root\zhiguang-fe" -Command {
  npm run lint
  if ($LASTEXITCODE -ne 0) {
    throw "Frontend typecheck failed."
  }
  npm run build
}

Invoke-ProjectCheck -Name "Creator Agent tests" -Path "$Root\creator-agent" -Command {
  & ".\.venv\Scripts\python.exe" -m pytest -q
}

Invoke-ProjectCheck -Name "Moderation Agent tests" -Path "$Root\moderation-agent" -Command {
  & ".\.venv\Scripts\python.exe" -m pytest -q
}

Invoke-ProjectCheck -Name "Community Assistant Agent tests" -Path "$Root\community-assistant-agent" -Command {
  & ".\.venv\Scripts\python.exe" -m pytest -q
}

Write-Host ""
Write-Host "All GreenBook verification checks passed."
