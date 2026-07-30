param(
  [ValidateRange(1, 3650)]
  [int]$Days = 30,
  [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"

. (Join-Path $PSScriptRoot "Import-GreenBookEnv.ps1")
Import-GreenBookRootEnv -EnvFile $EnvFile

Push-Location (Join-Path $Root "community-assistant-agent")
try {
  $arguments = @(
    "evals\run_runtime_report.py",
    "--days",
    [string]$Days
  )
  if (-not [string]::IsNullOrWhiteSpace($Output)) {
    $resolvedOutput = if ([System.IO.Path]::IsPathRooted($Output)) {
      $Output
    }
    else {
      Join-Path $Root $Output
    }
    $arguments += @("--output", $resolvedOutput)
  }
  & ".\.venv\Scripts\python.exe" @arguments
  if ($LASTEXITCODE -ne 0) {
    throw "Assistant runtime report failed with exit code $LASTEXITCODE."
  }
}
finally {
  Pop-Location
}
