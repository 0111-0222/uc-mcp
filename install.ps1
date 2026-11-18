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

$srv = Join-Path $PSScriptRoot "server.py"
Write-Host ""
Write-Host "1. Sign in to the forum (opens a browser window; skip if Firefox is already logged in):" -ForegroundColor Green
Write-Host ("  & `"{0}`" `"{1}`" login" -f $py, $srv)
Write-Host ""
Write-Host "2. Register the MCP server with Claude Code (all projects):" -ForegroundColor Green
Write-Host ("  claude mcp add --scope user unknowncheats -- `"{0}`" `"{1}`"" -f $py, $srv)
