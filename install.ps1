$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
& $python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 'uc-mcp needs Python 3.10 or newer')"
if ($LASTEXITCODE -ne 0) { exit 1 }

if (-not (Test-Path ".venv")) {
    & $python -m venv .venv
}

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r requirements.txt --quiet
& $py -c "import curl_cffi, bs4, lxml, mcp; print('dependencies OK')"

if (-not (Test-Path "cookies.json")) {
    Copy-Item "cookies.example.json" "cookies.json"
    Write-Host ""
    Write-Host "Created cookies.json. Fill it in before use:" -ForegroundColor Yellow
    Write-Host "  1. Log into unknowncheats.me in your browser."
    Write-Host "  2. Press F12 -> Network, tick 'Disable cache', refresh, and click the first request (index.php)."
    Write-Host "  3. Under Headers -> Request Headers -> Raw, copy the whole Cookie line into the 'cookie' field,"
    Write-Host "     and the User-Agent line into 'user_agent'."
    Write-Host "  The cookie line MUST include cf_clearance and bbsessionhash. Leave bbpassword out."
}

$srv = Join-Path $PSScriptRoot "server.py"
Write-Host ""
Write-Host "Register the MCP server with Claude Code (all projects):" -ForegroundColor Green
Write-Host ("  claude mcp add --scope user unknowncheats -- `"{0}`" `"{1}`"" -f $py, $srv)
