$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Invoke-NativeCheck {
  param(
    [Parameter(Mandatory = $true)]
    [scriptblock]$Command,
    [Parameter(Mandatory = $true)]
    [string]$FailureMessage
  )

  $previousErrorActionPreference = $ErrorActionPreference
  try {
    # Native tools commonly write non-fatal warnings to stderr on Windows.
    $ErrorActionPreference = "Continue"
    & $Command
    $exitCode = $LASTEXITCODE
  }
  finally {
    $ErrorActionPreference = $previousErrorActionPreference
  }
  if ($exitCode -ne 0) {
    throw "$FailureMessage (exit code $exitCode)."
  }
}

Write-Host "Checking Docker Compose..."
Invoke-NativeCheck -FailureMessage "Docker Compose validation failed." -Command {
  docker compose --project-directory $Root config --quiet
}

Write-Host "Checking Java compilation..."
Push-Location "$Root\apps\backend"
try {
  Invoke-NativeCheck -FailureMessage "Java compilation failed." -Command {
    mvn -q -DskipTests compile
  }
} finally {
  Pop-Location
}

Write-Host "Checking frontend build..."
Push-Location "$Root\zhiguang-fe"
try {
  Invoke-NativeCheck -FailureMessage "Frontend build failed." -Command {
    npm run build
  }
} finally {
  Pop-Location
}

Write-Host "Checking Creator real-model and identity contracts..."
Push-Location "$Root\creator-agent"
try {
  Invoke-NativeCheck -FailureMessage "Creator contract tests failed." -Command {
    & ".\.venv\Scripts\python.exe" -m pytest `
      tests\test_creator_identity.py `
      tests\test_creator_model_client.py `
      tests\test_creator_runtime_composition.py -q
  }
} finally {
  Pop-Location
}

Write-Host "Checking Agent API and Worker entrypoints..."
Push-Location $Root
try {
  Invoke-NativeCheck -FailureMessage "Agent API/Worker entrypoint import failed." -Command {
    uv run python -c "import greenbook_agent_api.main; import greenbook_agent_worker.main; print('Agent API/Worker imports OK')"
  }
} finally {
  Pop-Location
}

Write-Host "Canonical GreenBook smoke checks passed."
