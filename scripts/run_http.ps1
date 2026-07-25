param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$serverExecutable = Join-Path $projectRoot ".venv\Scripts\fidel-google-ads-mcp.exe"

if (-not (Test-Path -LiteralPath $serverExecutable)) {
    throw "Server is not installed. Run .\scripts\setup.ps1 first."
}

Set-Location -LiteralPath $projectRoot
& $serverExecutable --transport streamable-http --host $HostAddress --port $Port
exit $LASTEXITCODE
