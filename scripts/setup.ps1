param(
    [switch]$ProductionOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$generatedPath = Join-Path $projectRoot "generated"
$launcherPath = Join-Path $projectRoot "scripts\run_stdio.ps1"
$serverExecutable = Join-Path $venvPath "Scripts\fidel-google-ads-mcp.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    python -m venv $venvPath
}

& $venvPython -m pip install --upgrade pip setuptools wheel
if ($ProductionOnly) {
    & $venvPython -m pip install -e $projectRoot
} else {
    & $venvPython -m pip install -e "$projectRoot[dev]"
}

New-Item -ItemType Directory -Force -Path $generatedPath | Out-Null
$config = @{
    mcpServers = @{
        fidelGoogleAds = @{
            command = "powershell.exe"
            args = @(
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                $launcherPath
            )
        }
    }
}
$config | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $generatedPath "claude_desktop_config.snippet.json") -Encoding utf8
$codexSnippet = @"
[mcp_servers.fidel_google_ads]
enabled = true
command = '$serverExecutable'
args = ["--transport", "stdio"]
cwd = '$projectRoot'
startup_timeout_sec = 30
tool_timeout_sec = 120
"@
$codexSnippet | Set-Content -LiteralPath (Join-Path $generatedPath "codex_config.snippet.toml") -Encoding utf8

Write-Output "Installation complete."
Write-Output "Claude config snippet: $generatedPath\claude_desktop_config.snippet.json"
Write-Output "ChatGPT/Codex config snippet: $generatedPath\codex_config.snippet.toml"
Write-Output "Next: copy .env.example to .env and configure Google Ads credentials."
