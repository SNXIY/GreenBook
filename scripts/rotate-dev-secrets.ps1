param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"

if (-not (Test-Path -LiteralPath $EnvFile)) {
  throw "Environment file not found: $EnvFile"
}

function New-GreenBookSecret {
  $bytes = [byte[]]::new(32)
  $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $generator.GetBytes($bytes)
  }
  finally {
    $generator.Dispose()
  }
  return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

$jwtSecret = New-GreenBookSecret
$agentSecret = New-GreenBookSecret

$replacementValues = @{
  "JWT_SECRET" = $jwtSecret
  "GREENBOOK_AGENT_SERVICE_SHARED_SECRET" = $agentSecret
}

$seen = @{}
$updated = foreach ($line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
  if ($line -match "^([A-Za-z_][A-Za-z0-9_]*)=") {
    $name = $Matches[1]
    if ($replacementValues.ContainsKey($name)) {
      $seen[$name] = $true
      "$name=$($replacementValues[$name])"
      continue
    }
  }
  $line
}

$missing = @($replacementValues.Keys | Where-Object { -not $seen.ContainsKey($_) })
if ($missing.Count -gt 0) {
  throw "Cannot rotate secrets because .env is missing: $($missing -join ', ')"
}

[IO.File]::WriteAllLines($EnvFile, $updated, [Text.UTF8Encoding]::new($false))
Write-Host "Rotated application JWT and service-to-service secrets in .env."
Write-Host "API keys and database passwords were not changed."
Write-Host "Restart all applications. Existing login tokens are now invalid."
