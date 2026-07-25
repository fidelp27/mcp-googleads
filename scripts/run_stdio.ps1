$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$serverExecutable = Join-Path $projectRoot ".venv\Scripts\fidel-google-ads-mcp.exe"

if (-not (Test-Path -LiteralPath $serverExecutable)) {
    throw "Server is not installed. Run .\scripts\setup.ps1 first."
}

Set-Location -LiteralPath $projectRoot
& $serverExecutable --transport stdio
exit $LASTEXITCODE
