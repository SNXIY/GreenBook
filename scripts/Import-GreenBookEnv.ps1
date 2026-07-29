function Import-GreenBookRootEnv {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)]
    [string]$EnvFile
  )

  if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Environment file not found: $EnvFile"
  }

  foreach ($rawLine in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith("#")) {
      continue
    }

    $separator = $line.IndexOf("=")
    if ($separator -le 0) {
      continue
    }

    $name = $line.Substring(0, $separator).Trim()
    if ($name -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") {
      continue
    }

    $value = $line.Substring($separator + 1).Trim()
    if ($value.Length -ge 2) {
      $isDoubleQuoted = $value.StartsWith('"') -and $value.EndsWith('"')
      $isSingleQuoted = $value.StartsWith("'") -and $value.EndsWith("'")
      if ($isDoubleQuoted -or $isSingleQuoted) {
        $value = $value.Substring(1, $value.Length - 2)
      }
    }

    [Environment]::SetEnvironmentVariable($name, $value, "Process")
  }
}

function Get-GreenBookEnvValue {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [Parameter(Mandatory = $true)]
    [AllowEmptyString()]
    [string]$DefaultValue
  )

  $value = [Environment]::GetEnvironmentVariable($Name, "Process")
  if ([string]::IsNullOrWhiteSpace($value)) {
    return $DefaultValue
  }
  return $value
}

function Assert-GreenBookSecret {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [Parameter(Mandatory = $true)]
    [AllowEmptyString()]
    [string]$Value,
    [string[]]$ForbiddenValues = @()
  )

  if ([string]::IsNullOrWhiteSpace($Value)) {
    throw "$Name must be configured in the root .env."
  }
  if ($ForbiddenValues -contains $Value) {
    throw "$Name still uses a known development placeholder. Run .\scripts\rotate-dev-secrets.ps1, then restart the applications."
  }
}
