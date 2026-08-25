param(
  [switch]$NoReload,
  [int]$McpPort = 0
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\Import-GreenBookEnv.ps1"
Import-GreenBookRootEnv -EnvFile "$Root\.env"

$port = if ($McpPort -gt 0) {
  [string]$McpPort
} else {
  Get-GreenBookEnvValue -Name "GREENBOOK_MCP_PORT" -DefaultValue "8095"
}
$env:GREENBOOK_MCP_PORT = $port
$env:GREENBOOK_JAVA_BASE_URL = Get-GreenBookEnvValue -Name "GREENBOOK_JAVA_BASE_URL" -DefaultValue "http://127.0.0.1:8080"
$env:GREENBOOK_MCP_RUNTIME_TOKEN = Get-GreenBookEnvValue -Name "GREENBOOK_MCP_RUNTIME_TOKEN" -DefaultValue ""
$env:GREENBOOK_AGENT_IDENTITY_ISSUER = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_IDENTITY_ISSUER" -DefaultValue "http://127.0.0.1:8080"
$env:GREENBOOK_AGENT_IDENTITY_AUDIENCE = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_IDENTITY_AUDIENCE" -DefaultValue "greenbook-agent-runtime"
$env:GREENBOOK_AGENT_IDENTITY_JWKS_URL = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_IDENTITY_JWKS_URL" -DefaultValue "http://127.0.0.1:8080/.well-known/jwks.json"
$env:PYTHONPATH = @(
  $Root,
  "$Root\packages\agent_core",
  "$Root\packages\contracts",
  "$Root\packages\java_client",
  "$Root\packages\security",
  "$Root\services\greenbook_mcp"
) -join [IO.Path]::PathSeparator
$env:NO_PROXY = @($env:NO_PROXY, "127.0.0.1", "localhost") -ne "" -join ","
$env:no_proxy = $env:NO_PROXY

$python = "$Root\.venv-v2\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
  $python = "$Root\.venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $python)) {
  throw "GreenBook Python virtual environment is missing."
}

Set-Location $Root
$arguments = @(
  "-m", "uvicorn",
  "greenbook_mcp_server.http_app:create_app",
  "--factory",
  "--host", "127.0.0.1",
  "--port", $port
)
if (-not $NoReload) {
  $arguments += "--reload"
}

Write-Host "GreenBook Business MCP: Streamable HTTP endpoint at http://127.0.0.1:$port/mcp"
Write-Host "Press Ctrl+C to stop the Business MCP provider."
& $python @arguments
exit $LASTEXITCODE
