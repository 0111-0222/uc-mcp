#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "uc-mcp needs Python 3.10 or newer")'

[ -d .venv ] || "$PYTHON" -m venv .venv

PY="$PWD/.venv/bin/python"
"$PY" -m pip install --upgrade pip --quiet
"$PY" -m pip install -r requirements.txt --quiet
"$PY" -c "import curl_cffi, bs4, lxml, mcp; print('dependencies OK')"

# A cookies.json from an older setup holds a live login: readable by you alone.
[ ! -f cookies.json ] || chmod 600 cookies.json

printf '\n1. Sign in to the forum (opens a browser window; skip if Firefox is already logged in):\n'
printf '  "%s" "%s" login\n' "$PY" "$PWD/server.py"
printf '\n2. Register the MCP server with Claude Code (all projects):\n'
printf '  claude mcp add --scope user unknowncheats -- "%s" "%s"\n' "$PY" "$PWD/server.py"
