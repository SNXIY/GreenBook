$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

& "$PSScriptRoot\ensure-jwt-keys.ps1"
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv was not found. Install uv first: https://docs.astral.sh/uv/"
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  throw "npm was not found. Install Node.js 20+ first."
}

Write-Host "Installing locked Agent Runtime workspace..."
Push-Location $Root
try {
  uv sync --frozen
  if ($LASTEXITCODE -ne 0) {
    throw "uv sync failed for the Agent Runtime workspace."
  }
}
finally {
  Pop-Location
}

foreach ($project in @("creator-agent")) {
  Write-Host "Installing locked Python environment: $project"
  Push-Location "$Root\$project"
  try {
    uv sync --frozen
    if ($LASTEXITCODE -ne 0) {
      throw "uv sync failed for $project."
    }
  }
  finally {
    Pop-Location
  }
}

Write-Host "Installing locked frontend dependencies..."
Push-Location "$Root\zhiguang-fe"
try {
  npm ci
  if ($LASTEXITCODE -ne 0) {
    throw "npm ci failed for zhiguang-fe."
  }
}
finally {
  Pop-Location
}

Write-Host "Development dependencies are ready."
