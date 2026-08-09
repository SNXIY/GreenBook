# Canonical regression suite — do not compare counts across ad-hoc filters.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$python = Join-Path ".venv" "Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}
& $python -m pytest -m "regression and not external" -q @args
exit $LASTEXITCODE
