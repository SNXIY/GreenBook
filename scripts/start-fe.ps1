param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\Import-GreenBookEnv.ps1"
Import-GreenBookRootEnv -EnvFile "$Root\.env"

$env:VITE_API_BASE_URL = Get-GreenBookEnvValue -Name "VITE_API_BASE_URL" -DefaultValue "http://127.0.0.1:8080"
$env:VITE_CREATOR_STUDIO_URL = Get-GreenBookEnvValue -Name "VITE_CREATOR_STUDIO_URL" -DefaultValue "http://127.0.0.1:8092/creator.html"
$env:VITE_GREENBOOK_CREATOR_URL = Get-GreenBookEnvValue -Name "VITE_GREENBOOK_CREATOR_URL" -DefaultValue "/creator-api"
$env:VITE_GREENBOOK_AGENT_URL = Get-GreenBookEnvValue -Name "VITE_GREENBOOK_AGENT_URL" -DefaultValue "/agent-api"
$env:GREENBOOK_AGENT_API_PORT = Get-GreenBookEnvValue -Name "GREENBOOK_AGENT_API_PORT" -DefaultValue "8094"
$env:VITE_GREENBOOK_AGENT_PROXY_TARGET = Get-GreenBookEnvValue `
  -Name "VITE_GREENBOOK_AGENT_PROXY_TARGET" `
  -DefaultValue "http://127.0.0.1:$env:GREENBOOK_AGENT_API_PORT"

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  throw "npm was not found. Install Node.js 20+ and ensure 'npm' is available in PATH."
}

Set-Location "$Root\zhiguang-fe"
if (-not (Test-Path -LiteralPath "node_modules\.bin\vite.cmd")) {
  Write-Host "Frontend dependencies are missing; running npm install..."
  & npm install
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}

Write-Host "Starting Vite frontend at http://127.0.0.1:5173"
Write-Host "GreenBook Agent proxy target: $env:VITE_GREENBOOK_AGENT_PROXY_TARGET"
Write-Host "Press Ctrl+C to stop it. Logs remain in this terminal."
& npm run dev -- --host 127.0.0.1 --port 5173
exit $LASTEXITCODE
