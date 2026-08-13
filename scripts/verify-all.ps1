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
  $previousErrorActionPreference = $ErrorActionPreference
  try {
    # Windows PowerShell promotes native stderr (for example JVM warnings)
    # to ErrorRecord objects.  The command exit code remains authoritative.
    $ErrorActionPreference = "Continue"
    & $Command
    if ($LASTEXITCODE -ne 0) {
      throw "$Name failed with exit code $LASTEXITCODE."
    }
  }
  finally {
    $ErrorActionPreference = $previousErrorActionPreference
    Pop-Location
  }
}

Write-Host "=== Docker Compose configuration ==="
docker compose --project-directory $Root config --quiet
if ($LASTEXITCODE -ne 0) {
  throw "Docker Compose validation failed."
}

Invoke-ProjectCheck -Name "Agent Runtime Python tests" -Path $Root -Command {
  uv run ruff check packages/agent_core apps/agent_api apps/agent_worker services/greenbook_mcp packages/contracts packages/security packages/java_client packages/creator_client
  if ($LASTEXITCODE -ne 0) {
    throw "Agent Runtime Ruff check failed."
  }
  uv run ruff check scripts/run_p0_e2e.py --select F
  if ($LASTEXITCODE -ne 0) {
    throw "P0 harness Ruff check failed."
  }
  uv run pytest -q
}

Invoke-ProjectCheck -Name "Java backend tests" -Path "$Root\apps\backend" -Command {
  mvn -q test
}

Invoke-ProjectCheck -Name "Frontend typecheck and build" -Path "$Root\zhiguang-fe" -Command {
  npm run lint
  if ($LASTEXITCODE -ne 0) {
    throw "Frontend typecheck failed."
  }
  npm run build
}

Invoke-ProjectCheck -Name "Creator Service tests" -Path "$Root\creator-agent" -Command {
  uv run ruff check app --select F,I
  if ($LASTEXITCODE -ne 0) {
    throw "Creator Ruff check failed."
  }
  uv run pytest -q
}

Write-Host ""
Write-Host "All canonical GreenBook verification checks passed."
