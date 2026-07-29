param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$KeyDirectory = "$Root\zhiguang-be\src\main\resources\keys"
$PrivateKey = "$KeyDirectory\private.pem"
$PublicKey = "$KeyDirectory\public.pem"

if ((Test-Path -LiteralPath $PrivateKey) -and (Test-Path -LiteralPath $PublicKey)) {
  Write-Host "Local JWT key pair is present."
  exit 0
}
if ((Test-Path -LiteralPath $PrivateKey) -or (Test-Path -LiteralPath $PublicKey)) {
  throw "JWT key pair is incomplete. Remove both local PEM files, then run this script again."
}

$openssl = Get-Command openssl -ErrorAction SilentlyContinue
if (-not $openssl) {
  throw "OpenSSL is required to generate the local JWT key pair. Install OpenSSL and rerun this script."
}

New-Item -ItemType Directory -Path $KeyDirectory -Force | Out-Null
& $openssl.Source genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out $PrivateKey
if ($LASTEXITCODE -ne 0) {
  throw "Generating the JWT private key failed."
}
& $openssl.Source pkey -in $PrivateKey -pubout -out $PublicKey
if ($LASTEXITCODE -ne 0) {
  Remove-Item -LiteralPath $PrivateKey -ErrorAction SilentlyContinue
  throw "Generating the JWT public key failed."
}

Write-Host "Generated a local JWT RSA key pair. It is ignored by Git."
