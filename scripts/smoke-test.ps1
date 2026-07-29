$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

Write-Host "Checking Docker Compose..."
docker compose --project-directory $Root config --quiet
if ($LASTEXITCODE -ne 0) {
  throw "Docker Compose validation failed."
}

Write-Host "Checking Java compilation..."
Push-Location "$Root\zhiguang-be"
try {
  mvn -q -DskipTests compile
  if ($LASTEXITCODE -ne 0) {
    throw "Java compilation failed."
  }
} finally {
  Pop-Location
}

Write-Host "Checking frontend build..."
Push-Location "$Root\zhiguang-fe"
try {
  npm run build
  if ($LASTEXITCODE -ne 0) {
    throw "Frontend build failed."
  }
} finally {
  Pop-Location
}

Write-Host "Checking Creator real-model and identity contracts..."
Push-Location "$Root\creator-agent"
try {
  & ".\.venv\Scripts\python.exe" -m pytest `
    tests\test_creator_identity.py `
    tests\test_creator_model_client.py `
    tests\test_creator_runtime_composition.py -q
  if ($LASTEXITCODE -ne 0) {
    throw "Creator contract tests failed."
  }
} finally {
  Pop-Location
}

Write-Host "Checking Moderation HTTP contract..."
Push-Location "$Root\moderation-agent"
try {
  & ".\.venv\Scripts\python.exe" -m pytest `
    tests\service\test_moderation_routes.py `
    tests\service\test_auth.py -q
  if ($LASTEXITCODE -ne 0) {
    throw "Moderation HTTP contract tests failed."
  }
} finally {
  Pop-Location
}

Write-Host "Checking Community Assistant harness contracts..."
Push-Location "$Root\community-assistant-agent"
try {
  & ".\.venv\Scripts\python.exe" -m pytest -q
  if ($LASTEXITCODE -ne 0) {
    throw "Community Assistant contract tests failed."
  }
} finally {
  Pop-Location
}

Write-Host "GreenBook smoke checks passed."
