"""Transport for UnknownCheats: TLS-impersonating session, tiered rate limits,
a persistent request budget and an on-disk page cache.

Three things here are load-bearing, each verified against the live site:

1. `curl_cffi` with Chrome impersonation, not `httpx`/`requests`. UC sits behind
   a Cloudflare WAF that fingerprints the TLS/JA3 handshake. A plain Python HTTP
   client is rejected with a hard 403 "you have been blocked" no matter how
   correct the cookies and User-Agent are.
2. Rate limits are tiered, jittered and enforced across the whole process. The
   forum's own search throttle is 15s; page reads are far cheaper, so they get a
   shorter interval instead of paying the search price.
3. The cache is sqlite on disk, not memory. Claude Code starts a fresh server
   process for every session, so an in-memory cache would never hit.

The session cookies are a live login, so every URL is checked before it is
sent: it must parse to an unknowncheats.me host and must not name an endpoint
that changes account state. Forum posts are attacker-controlled text, and a
model that reads one can be talked into fetching a link from it.
"""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from curl_cffi import requests as cr

BASE = "https://www.unknowncheats.me/forum/"
HOST = "unknowncheats.me"
COOKIE_DOMAIN = "." + HOST
ROOT = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.environ.get("UC_MCP_COOKIES") or os.path.join(ROOT, "cookies.json")
CACHE_DB = os.environ.get("UC_MCP_CACHE") or os.path.join(ROOT, ".cache", "uc.sqlite3")

# vBulletin endpoints that change state or expose the account itself. Several
# act on a plain GET (logout, subscribe, mark-read), so a link planted in a post
# must never reach them. Any URL carrying a security token or logout hash is an
# action link by definition.
FORBIDDEN = re.compile(
    r"/(?:login|register|private|sendmessage|newreply|newthread|editpost|postings|"
    r"inlinemod|moderation|modcp|admincp|subscription|usercp|profile|reputation|"
    r"report|threadrate|payments|ajax|infraction|attachment)\.php"
    r"|[?&](?:securitytoken|logouthash)=|[?&]do=logout", re.I)

# Minimum seconds between outbound requests, per class. The forum enforces 15s
# between searches server-side; everything else is self-imposed politeness.
INTERVALS = {"search": 17.0, "read": 4.0, "meta": 4.0}
JITTER = 2.0
BUDGET_HOUR = 150
BUDGET_DAY = 1000

# Cache lifetimes in seconds, chosen from how fast each page actually changes.
TTL = {"thread": 900, "forum": 600, "search": 900, "index": 21600, "raw": 600}


class UCError(Exception):
    """A failure the caller should see as a readable message, not a traceback."""


class BlockedError(UCError):
    """Cloudflare or a login wall answered instead of the page."""


class BudgetError(UCError):
    """Local request budget exhausted."""


@dataclass
class Fetch:
    text: str
    url: str
    cached: bool


class Limiter:
    """Tiered, jittered spacing plus rolling hour/day caps.

    The sleep happens *outside* the lock: under the lock we only claim a slot on
    the timeline and advance it. That keeps ordering fair and stops a burst of
    tool calls from pinning worker threads on a held mutex.
    """

    def __init__(self, store: Store):
        self._lock = threading.Lock()
        self._next = {k: 0.0 for k in INTERVALS}
        self._store = store

    def acquire(self, kind: str) -> float:
        interval = INTERVALS.get(kind, INTERVALS["read"])
        used_h, used_d = self._store.usage()
        # The forum's throttle is server-side and outlives this process, which
        # Claude Code restarts every session. An in-memory timestamp would let
        # the first call of each session fire straight into a rejection, so the
        # last send time is persisted in wall-clock terms as well.
        last_wall = self._store.meta_get(f"last_send:{kind}", 0.0) or 0.0
        wall_wait = max(0.0, (last_wall + interval) - time.time())
        if used_h >= BUDGET_HOUR:
            raise BudgetError(
                f"local budget spent: {used_h}/{BUDGET_HOUR} requests this hour. The cap "
                "exists so this never looks like a scraper to the forum. Retry later.")
        if used_d >= BUDGET_DAY:
            raise BudgetError(
                f"local budget spent: {used_d}/{BUDGET_DAY} requests today. Retry tomorrow.")

        with self._lock:
            now = time.monotonic()
            start = max(now, self._next.get(kind, 0.0))
            self._next[kind] = start + interval + random.uniform(0, JITTER)
            # Every class also pushes the others forward, so parallel tool calls
            # cannot interleave into a burst.
            for k in self._next:
                self._next[k] = max(self._next[k], start + INTERVALS["read"])
            wait = max(0.0, start - now, wall_wait)
        if wait > 0:
            time.sleep(wait)
        self._store.meta_set(f"last_send:{kind}", time.time())
        return wait


class Store:
    """sqlite-backed page cache, request ledger and small key/value scratch."""

    def __init__(self, path: str = CACHE_DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(
            "PRAGMA journal_mode=WAL;"
            "CREATE TABLE IF NOT EXISTS pages ("
            " key TEXT PRIMARY KEY, url TEXT, body TEXT, ts REAL);"
            "CREATE TABLE IF NOT EXISTS hits (ts REAL);"
            "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);"
        )
        self._db.commit()

    def get(self, key: str, ttl: float) -> Fetch | None:
        with self._lock:
            row = self._db.execute(
                "SELECT url, body, ts FROM pages WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        url, body, ts = row
        if time.time() - ts > ttl:
            return None
        return Fetch(text=body, url=url, cached=True)

    def put(self, key: str, url: str, body: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO pages(key,url,body,ts) VALUES(?,?,?,?)",
                (key, url, body, time.time()))
            self._db.commit()

    def record_hit(self) -> None:
        with self._lock:
            now = time.time()
            self._db.execute("INSERT INTO hits(ts) VALUES(?)", (now,))
            self._db.execute("DELETE FROM hits WHERE ts < ?", (now - 86400 * 2,))
            self._db.commit()

    def usage(self) -> tuple[int, int]:
        now = time.time()
        with self._lock:
            hour = self._db.execute(
                "SELECT COUNT(*) FROM hits WHERE ts > ?", (now - 3600,)).fetchone()[0]
            day = self._db.execute(
                "SELECT COUNT(*) FROM hits WHERE ts > ?", (now - 86400,)).fetchone()[0]
        return hour, day

    def meta_get(self, k: str, default=None):
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def meta_set(self, k: str, v) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(v)))
            self._db.commit()

    def cache_stats(self) -> tuple[int, float]:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            newest = self._db.execute("SELECT MAX(ts) FROM pages").fetchone()[0] or 0.0
        return n, newest

    def clear_pages(self) -> int:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            self._db.execute("DELETE FROM pages")
            self._db.commit()
        return n


def on_host(url: str) -> bool:
    """True only for an http(s) URL whose parsed host is unknowncheats.me.

    Parsed, never string-split: "https://evil.com?.unknowncheats.me/" ends with
    the right suffix but connects to evil.com.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme in ("http", "https")
            and not parts.username and not parts.password
            and port in (None, 80, 443)
            # A strict charset: clients disagree on odd hosts like "a.com\.b.me".
            and re.fullmatch(r"[a-z0-9.-]+", host) is not None
            and (host == HOST or host.endswith(COOKIE_DOMAIN)))


def check_url(url: str) -> str:
    """Return `url` as https if it is a forum page this server may read, else raise."""
    if not on_host(url):
        raise UCError(
            f"refused: {url} is not on {HOST}. This server only reads UnknownCheats, "
            "so a link found in forum content cannot send it, or your cookies, "
            "anywhere else.")
    if FORBIDDEN.search(url):
        raise UCError(
            f"refused: {url} is an account or posting endpoint. This server is "
            "read-only and never touches login, PMs, profile, posting, rating, "
            "subscriptions or attachments.")
    return "https:" + url.split(":", 1)[1] if url.lower().startswith("http:") else url


BLOCK_MARKERS = (
    "Attention Required! | Cloudflare",
    "Sorry, you have been blocked",
    "Just a moment...",
    "Checking your browser before accessing",
    "cf-error-details",
)
# vBulletin only renders the logout link on some templates, so the reliable
# signal is the guest security token; the control-panel link corroborates it.
GUEST_TOKEN = 'SECURITYTOKEN = "guest"'
LOGGED_IN_MARKERS = ("usercp.php", "do=logout", "profile.php?do=editprofile")


class Client:
    def __init__(self):
        self.store = Store()
        self.limiter = Limiter(self.store)
        self.cookie_names: list[str] = []
        self._lock = threading.Lock()
        self._session = None
        self._token = None
        self.tz_offset = self.store.meta_get("tz_offset", 1)
        self.load_cookies()

    # -- session ---------------------------------------------------------
    def load_cookies(self) -> None:
        if not os.path.exists(COOKIE_FILE):
            raise UCError(
                f"{COOKIE_FILE} missing. Copy cookies.example.json to cookies.json and "
                "paste your exported UnknownCheats cookie header into it (see README).")
        with open(COOKIE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        header = (data.get("cookie") or "").strip()
        if not header:
            raise UCError("cookies.json has an empty 'cookie' field.")
        jar = {}
        for part in header.split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                jar[name.strip()] = value.strip()
        headers = {"Accept-Language": "en-US,en;q=0.9"}
        if data.get("user_agent"):
            headers["User-Agent"] = data["user_agent"]
        with self._lock:
            session = cr.Session(impersonate="chrome", timeout=45)
            # curl_cffi sends a cookie with no domain to whatever host the
            # request targets. Pinning the domain makes curl's own cookie engine
            # refuse to attach the login to any other host, redirects included.
            for name, value in jar.items():
                session.cookies.set(name, value, domain=COOKIE_DOMAIN)
            session.headers.update(headers)
            self._session = session
            self._token = None
        self.cookie_names = sorted(jar)
        self.store.meta_set("securitytoken", None)

    def has_clearance(self) -> bool:
        return "cf_clearance" in self.cookie_names

    def has_persistent_login(self) -> bool:
        return "bbpassword" in self.cookie_names

    # -- fetching --------------------------------------------------------
    def _check(self, text: str, url: str) -> None:
        if any(m in text[:4000] for m in BLOCK_MARKERS):
            raise BlockedError(
                "Cloudflare blocked the request. The cf_clearance cookie has expired "
                "(they are short-lived, often under an hour) or was issued to a different "
                "IP/User-Agent. Refresh the export in cookies.json from the logged-in "
                "browser, then call uc_session(reload=True).")
        if "login.php" in url and "do=logout" not in url:
            raise BlockedError(
                "Redirected to login: the forum session cookie has expired. Re-export "
                "cookies.json and call uc_session(reload=True).")
        if GUEST_TOKEN in text or not any(m in text for m in LOGGED_IN_MARKERS):
            raise BlockedError(
                "Page loaded but the session is browsing as a guest. Most of this forum, "
                "search included, is login-gated. Re-export cookies.json including "
                "bbsessionhash, then call uc_session(reload=True).")

    def _learn_tz(self, text: str) -> None:
        from uc_dates import detect_tz_offset
        off = detect_tz_offset(text)
        if off is not None and off != self.tz_offset:
            self.tz_offset = off
            self.store.meta_set("tz_offset", off)

    def get(self, path: str, kind: str = "read", ttl_key: str | None = None,
            refresh: bool = False) -> Fetch:
        url = self._absolute(path)
        ttl = TTL.get(ttl_key or kind, TTL["raw"])
        key = "GET " + url
        if not refresh:
            hit = self.store.get(key, ttl)
            if hit:
                return hit
        self.limiter.acquire(kind)
        text, final = self._send("GET", url)
        self.store.put(key, final, text)
        return Fetch(text=text, url=final, cached=False)

    def post(self, path: str, data: dict, kind: str = "search") -> Fetch:
        url = self._absolute(path)
        self.limiter.acquire(kind)
        text, final = self._send("POST", url, data=data)
        return Fetch(text=text, url=final, cached=False)

    def _absolute(self, path: str) -> str:
        path = (path or "").strip()
        url = path if re.match(r"^[a-z][a-z0-9+.-]*:", path, re.I) else BASE + path.lstrip("/")
        return check_url(url)

    def _send(self, method: str, url: str, data: dict | None = None) -> tuple[str, str]:
        self.store.record_hit()
        with self._lock:
            session = self._session
        try:
            if method == "POST":
                resp = session.post(url, data=data, headers={"Referer": BASE})
            else:
                resp = session.get(url, headers={"Referer": BASE})
        except Exception as e:  # network/TLS/timeout all surface as readable text
            raise UCError(f"request failed: {type(e).__name__}: {e}") from e

        self._drop_superseded_cookies(session)
        text, final = resp.text, str(resp.url)
        # The cookies cannot follow a redirect off-site, but the page it lands on
        # is still not forum content and must not reach the model as if it were.
        if not on_host(final):
            raise UCError(f"refused: the forum redirected off-site to {final}.")
        if resp.status_code == 429:
            raise UCError(
                "HTTP 429 from the forum: rate limited server-side. Wait several minutes.")
        if resp.status_code in (403, 503):
            self._check(text, final)
            raise BlockedError(f"HTTP {resp.status_code} from Cloudflare. Refresh cookies.json.")
        if resp.status_code >= 400:
            raise UCError(f"HTTP {resp.status_code} for {final}")
        self._check(text, final)
        self._learn_tz(text)
        return text, final

    @staticmethod
    def _drop_superseded_cookies(session) -> None:
        """Keep one copy of each cookie: the one the forum most recently set.

        The exported cookies are pinned to COOKIE_DOMAIN, but when the forum
        rotates a session it sets the new value host-only on www. Both then go
        out on every request, the forum may honour the stale bbsessionhash, and
        the page renders as a guest until the next reload.
        """
        jar = session.cookies.jar
        fresh = {c.name for c in jar
                 if c.domain != COOKIE_DOMAIN and c.domain.endswith(HOST)}
        stale = [c for c in jar if c.domain == COOKIE_DOMAIN and c.name in fresh]
        for c in stale:
            jar.clear(c.domain, c.path, c.name)

    # -- vBulletin security token ---------------------------------------
    def security_token(self, refresh: bool = False) -> str:
        """vBulletin requires a per-session token on every search POST."""
        if self._token and not refresh:
            return self._token
        cached = self.store.meta_get("securitytoken")
        if cached and not refresh:
            self._token = cached
            return cached
        page = self.get("search.php", kind="meta", ttl_key="index", refresh=refresh)
        m = re.search(r'SECURITYTOKEN\s*=\s*"([^"]+)"', page.text)
        if not m or m.group(1) in ("guest", ""):
            raise BlockedError(
                "No logged-in security token on search.php — the session cookie is not "
                "authenticating. Re-export cookies.json.")
        self._token = m.group(1)
        self.store.meta_set("securitytoken", self._token)
        return self._token


_client: Client | None = None
_client_lock = threading.Lock()


def client() -> Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = Client()
        return _client


def reset_client() -> None:
    global _client
    with _client_lock:
        _client = None
