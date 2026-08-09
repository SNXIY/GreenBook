# Sync Java OpenAPI YAML to Python project contracts directory.
# The Java file is the single source of truth.
# Run from project root: .\scripts\sync-agent-openapi.ps1

param(
    [string]$Source = "contracts\java-openapi.yaml",
    [string]$Target = "..\green-book\contracts\java-openapi.yaml"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Source)) {
    Write-Error "Source file not found: $Source"
    exit 1
}

$sourceHash = (Get-FileHash -Path $Source -Algorithm SHA256).Hash
Write-Host "Source checksum (SHA256): $sourceHash"

$targetDir = Split-Path $Target -Parent
if (-not (Test-Path $targetDir)) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    Write-Host "Created target directory: $targetDir"
}

Copy-Item -Path $Source -Destination $Target -Force
Write-Host "Synced: $Source -> $Target"

if (Test-Path $Target) {
    $targetHash = (Get-FileHash -Path $Target -Algorithm SHA256).Hash
    if ($sourceHash -eq $targetHash) {
        Write-Host "SUCCESS: Checksums match ($sourceHash)"
    } else {
        Write-Error "FAILED: Checksum mismatch! Source=$sourceHash Target=$targetHash"
        exit 1
    }
} else {
    Write-Error "FAILED: Target file was not created at $Target"
    exit 1
}

Write-Host ""
Write-Host "Agent OpenAPI contract sync complete."
Write-Host "JWKS URL: /.well-known/jwks.json"
