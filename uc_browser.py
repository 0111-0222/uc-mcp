"""Read the forum login straight out of the Firefox the user already browses with.

The hand-exported cookies.json goes stale constantly: Cloudflare's cf_clearance
is reissued on its own schedule and is tied to the User-Agent that earned it, so
a copy is dead as soon as the browser gets a new one. Firefox keeps the current
values in a plain sqlite file, and vBulletin's bbuserid/bbpassword pair sits
next to it, so reading that file on demand gives a session that stays valid for
as long as the browser's own does. Nothing read here is written anywhere: the
values live in this process's memory only.

Firefox only. Chromium browsers encrypt their cookie store with a key bound to
the browser binary, and working around that is not something this server does;
those users keep the cookies.json path.
"""

from __future__ import annotations

import configparser
import glob
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

HOST = "unknowncheats.me"


@dataclass
class Cookies:
    jar: dict[str, str]
    user_agent: str
    impersonate: str  # curl_cffi TLS profile; must match the User-Agent's browser
    source: str       # "firefox" or "file"
    label: str        # shown by uc_session

    @property
    def fingerprint(self) -> str:
        """Identifies this exact set of values without holding them twice."""
        blob = repr((sorted(self.jar.items()), self.user_agent)).encode()
        return hashlib.sha256(blob).hexdigest()

    def without(self, name: str) -> Cookies:
        jar = {k: v for k, v in self.jar.items() if k != name}
        return Cookies(jar, self.user_agent, self.impersonate, self.source, self.label)

    def can_log_in(self) -> bool:
        return "bbsessionhash" in self.jar or {"bbuserid", "bbpassword"} <= self.jar.keys()


def profile_dirs() -> list[Path]:
    override = os.environ.get("UC_MCP_FIREFOX_PROFILE")
    if override:
        return [Path(override)]
    home = Path.home()
    if sys.platform == "win32":
        roots = [Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
                 / "Mozilla" / "Firefox" / "Profiles"]
    elif sys.platform == "darwin":
        roots = [home / "Library" / "Application Support" / "Firefox" / "Profiles"]
    else:
        roots = [home / ".mozilla" / "firefox",
                 home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
                 home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox"]
    return [Path(p).parent for root in roots
            for p in glob.glob(str(root / "*" / "cookies.sqlite"))]


_QUERY = ("SELECT name, value, expiry, lastAccessed, originAttributes FROM moz_cookies "
          "WHERE host = ? OR host LIKE ?")


def _read_rows(db: Path) -> list[tuple]:
    """Rows for the forum's host, read without disturbing a running Firefox."""
    args = (HOST, "%." + HOST)
    try:
        # Read-only, but through the WAL, where a running Firefox keeps its
        # newest writes; a freshly reissued cf_clearance is exactly that.
        con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            return con.execute(_QUERY, args).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        pass
    # Locked or shm-less: read a private copy instead (WAL included).
    with tempfile.TemporaryDirectory(prefix="uc-mcp-ff-") as tmp:
        copy = Path(tmp) / "cookies.sqlite"
        shutil.copy2(db, copy)
        wal = db.with_name(db.name + "-wal")
        if wal.exists():
            shutil.copy2(wal, copy.with_name(copy.name + "-wal"))
        con = sqlite3.connect(copy)
        try:
            return con.execute(_QUERY, args).fetchall()
        finally:
            con.close()


def _pick(rows: list[tuple], now: float) -> tuple[dict[str, str], float]:
    """One value per cookie, from one browsing context, plus when it was last used.

    A profile can hold the forum's cookies several times over: once per
    container tab, and again in partitioned (CHIPS) form, which is how
    Cloudflare now issues cf_clearance. Containers may be different accounts
    and must not be mixed; within one, the copy that expires last is the one
    most recently set.
    """
    contexts: dict[str, list[tuple]] = {}
    for name, value, expiry, used, attrs in rows:
        if not (name.startswith("bb") or name == "cf_clearance"):
            continue  # analytics and consent cookies are none of our business
        expiry = expiry / 1000 if expiry > 1e11 else expiry  # ms since Firefox 139
        if expiry and expiry < now:
            continue
        context = re.sub(r"[&^]?partitionKey=[^&]*", "", attrs or "").strip("^&")
        contexts.setdefault(context, []).append((name, value, expiry, used))

    best: tuple[dict[str, str], float] = ({}, 0.0)
    for picked in contexts.values():
        jar: dict[str, str] = {}
        for name, value, _expiry, _used in sorted(picked, key=lambda r: r[2]):
            jar[name] = value
        login = [r[3] for r in picked if r[0] in ("bbsessionhash", "bbpassword")]
        if login and max(login) > best[1]:
            best = (jar, max(login))
    return best


def _platform_token() -> str:
    if sys.platform == "win32":
        return "Windows NT 10.0; Win64; x64"
    if sys.platform == "darwin":
        return "Macintosh; Intel Mac OS X 10.15"
    try:
        if re.search(r"^ID=ubuntu$", Path("/etc/os-release").read_text(), re.M):
            return "X11; Ubuntu; Linux x86_64"
    except OSError:
        pass
    return "X11; Linux x86_64"


def user_agent(profile: Path) -> str | None:
    """The User-Agent this profile's Firefox sends; cf_clearance is bound to it."""
    forced = os.environ.get("UC_MCP_USER_AGENT")
    if forced:
        return forced
    try:
        prefs = (profile / "prefs.js").read_text(encoding="utf-8", errors="replace")
        m = re.search(r'user_pref\("general\.useragent\.override",\s*"([^"]+)"\)', prefs)
        if m:
            return m.group(1)
    except OSError:
        pass
    ini = configparser.ConfigParser()
    try:
        ini.read(profile / "compatibility.ini", encoding="utf-8")
        major = int(ini["Compatibility"]["LastVersion"].split(".")[0])
    except (configparser.Error, KeyError, ValueError, OSError):
        return None
    return (f"Mozilla/5.0 ({_platform_token()}; rv:{major}.0) "
            f"Gecko/20100101 Firefox/{major}.0")


def firefox() -> Cookies | None:
    """The most recently used logged-in forum session in any Firefox profile."""
    now = time.time()
    best: tuple[float, Cookies] | None = None
    for profile in profile_dirs():
        try:
            jar, used = _pick(_read_rows(profile / "cookies.sqlite"), now)
        except (sqlite3.Error, OSError):
            continue
        ua = user_agent(profile)
        found = Cookies(jar, ua or "", "firefox", "firefox", f"Firefox profile {profile.name}")
        if ua and found.can_log_in() and (best is None or used > best[0]):
            best = (used, found)
    return best[1] if best else None
