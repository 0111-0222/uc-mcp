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

if [ ! -f cookies.json ]; then
    cp cookies.example.json cookies.json
    printf '\nCreated cookies.json. Fill it in before use:\n'
    printf '  1. Log into unknowncheats.me in your browser.\n'
    printf '  2. Export the site'"'"'s cookies as a header string (Cookie-Editor -> Export -> Header String).\n'
    printf '  3. Paste it into the "cookie" field, and your browser'"'"'s User-Agent into "user_agent".\n'
    printf '  The export MUST include cf_clearance and bbsessionhash. Leave bbpassword out.\n'
fi
# It holds a live login: readable by you alone.
chmod 600 cookies.json

printf '\nRegister the MCP server with Claude Code (all projects):\n'
printf '  claude mcp add --scope user unknowncheats -- "%s" "%s"\n' "$PY" "$PWD/server.py"
