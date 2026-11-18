"""Point the server at throwaway cookies and cache before any module loads.

uc_http resolves its file paths at import time, so the environment has to be
set here, ahead of the test modules' imports. Nothing in the suite touches the
network except a loopback server in test_http.py.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

_tmp = Path(tempfile.mkdtemp(prefix="uc-mcp-test-"))
_cookies = _tmp / "cookies.json"
_cookies.write_text(json.dumps({
    "cookie": "bbsessionhash=test-session; cf_clearance=test-clearance; bbuserid=1",
    "user_agent": "uc-mcp-tests",
}), encoding="utf-8")
os.environ["UC_MCP_COOKIES"] = str(_cookies)
# Never the developer's real browser login: the suite reads the file above only.
os.environ["UC_MCP_SOURCE"] = "file"
os.environ.pop("UC_MCP_FIREFOX_PROFILE", None)
os.environ.pop("UC_MCP_USER_AGENT", None)
# Nor the developer's saved login or sign-in profile.
os.environ["UC_MCP_HOME"] = str(_tmp / "home")
os.environ.pop("UC_MCP_BROWSER", None)
os.environ["UC_MCP_CACHE"] = str(_tmp / "uc.sqlite3")

# Every relative date in the fixtures is read against this instant.
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def fixture():
    def load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")
    return load
