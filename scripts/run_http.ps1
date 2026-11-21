param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

Set-Location -LiteralPath $projectRoot
if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $env:PYTHONPATH = $projectRoot
} elseif ($env:PYTHONPATH -notlike "*$projectRoot*") {
    $env:PYTHONPATH = "$projectRoot;$($env:PYTHONPATH)"
}

$candidates = @(
    (Join-Path $projectRoot ".venv\Scripts\google-ads-mcp-server.exe"),
    (Join-Path $projectRoot ".venv\Scripts\google-ads-mcp.exe")
)

foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
        & $candidate --transport streamable-http --host $HostAddress --port $Port
        exit $LASTEXITCODE
    }
}

$cmd = Get-Command google-ads-mcp-server -ErrorAction SilentlyContinue
if (-not $cmd) {
    $cmd = Get-Command google-ads-mcp -ErrorAction SilentlyContinue
}
if ($cmd) {
    & $cmd.Source --transport streamable-http --host $HostAddress --port $Port
    exit $LASTEXITCODE
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source (Join-Path $projectRoot "google_ads_server.py") --transport streamable-http --host $HostAddress --port $Port
    exit $LASTEXITCODE
}

throw "Server is not installed. Run .\scripts\setup.ps1 or pip install -e . first."
