"""The two guarantees that keep a shared install from burning its owner's
account: the login cookies only ever go to unknowncheats.me, and no URL can
reach an endpoint that changes account state."""

import http.server
import threading
import time
import types

import pytest
from curl_cffi.const import CurlOpt

import uc_http
from uc_http import UCError, check_url, on_host


@pytest.mark.parametrize("url", [
    "https://www.unknowncheats.me/forum/rust/",
    "https://unknowncheats.me/forum/index.php",
    "http://www.unknowncheats.me/forum/search.php?searchid=1",
    "https://WWW.UnknownCheats.me/forum/",
    "https://www.unknowncheats.me:443/forum/",
])
def test_on_host_accepts_forum(url):
    assert on_host(url)


@pytest.mark.parametrize("url", [
    # Each of these passes a naive `url.split("/")[2].endswith(...)` check.
    "https://evil.com?.unknowncheats.me/",
    "https://evil.com#.unknowncheats.me",
    "https://evil.com\\.unknowncheats.me/",
    # Userinfo, look-alike hosts, odd ports and schemes.
    "https://www.unknowncheats.me@evil.com/",
    "https://user:pass@www.unknowncheats.me/forum/",
    "https://unknowncheats.me.evil.com/",
    "https://notunknowncheats.me/",
    "https://www.unknowncheats.me:8080/forum/",
    "file:///etc/passwd",
    "ftp://www.unknowncheats.me/",
    "javascript:alert(1)",
    "",
])
def test_on_host_rejects_everything_else(url):
    assert not on_host(url)


@pytest.mark.parametrize("url", [
    "https://www.unknowncheats.me/forum/login.php?do=logout&logouthash=abc",
    "https://www.unknowncheats.me/forum/private.php",
    "https://www.unknowncheats.me/forum/newreply.php?do=newreply&p=1",
    "https://www.unknowncheats.me/forum/profile.php?do=editprofile",
    "https://www.unknowncheats.me/forum/subscription.php?do=addsubscription&t=1",
    "https://www.unknowncheats.me/forum/reputation.php?p=1",
    "https://www.unknowncheats.me/forum/attachment.php?attachmentid=1",
    "https://www.unknowncheats.me/forum/misc.php?do=x&securitytoken=abc",
    "https://www.unknowncheats.me/forum/anything?do=logout",
])
def test_check_url_blocks_account_endpoints(url):
    with pytest.raises(UCError, match="read-only"):
        check_url(url)


def test_check_url_upgrades_to_https():
    assert check_url("http://www.unknowncheats.me/forum/rust/") == \
        "https://www.unknowncheats.me/forum/rust/"


@pytest.fixture
def client():
    uc_http.reset_client()
    c = uc_http.client()
    yield c
    uc_http.reset_client()


def test_absolute_resolves_relative_paths(client):
    assert client._absolute("rust/1-a.html") == uc_http.BASE + "rust/1-a.html"
    assert client._absolute("/rust/") == uc_http.BASE + "rust/"
    # Protocol-relative input stays a path under the forum, never a new host.
    assert client._absolute("//evil.com/x") == uc_http.BASE + "evil.com/x"


@pytest.mark.parametrize("path", [
    "https://evil.com?.unknowncheats.me/",
    "HTTPS://evil.com/",
    "https://www.unknowncheats.me/forum/private.php",
])
def test_get_refuses_before_any_request(client, path):
    with pytest.raises(UCError, match="refused"):
        client.get(path)
    assert client.store.usage() == (0, 0)  # nothing was sent, nothing was counted


def test_cookies_only_reach_the_forum(client):
    """End to end through curl: point both a look-alike host and the real forum
    host at a loopback server and see which one receives the login."""
    seen = {}

    class Echo(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen[self.headers.get("Host").split(":")[0]] = self.headers.get("Cookie")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        session = client._session
        session.curl_options[CurlOpt.RESOLVE] = [
            f"www.unknowncheats.me:{port}:127.0.0.1",
            f"evil.example:{port}:127.0.0.1",
        ]
        session.get(f"http://evil.example:{port}/")
        session.get(f"http://www.unknowncheats.me:{port}/")
    finally:
        server.shutdown()

    assert seen["evil.example"] is None
    assert "bbsessionhash=test-session" in seen["www.unknowncheats.me"]


def test_blocked_and_guest_pages_are_detected(client):
    with pytest.raises(uc_http.BlockedError, match="Cloudflare"):
        client._check("<title>Just a moment...</title>", uc_http.BASE)
    with pytest.raises(uc_http.BlockedError, match="guest"):
        client._check('var SECURITYTOKEN = "guest";', uc_http.BASE)
    with pytest.raises(uc_http.BlockedError, match="login"):
        client._check("", uc_http.BASE + "login.php?do=login")
    client._check('<a href="usercp.php">User CP</a>', uc_http.BASE)


def test_rotated_session_cookie_replaces_the_exported_one(client):
    # The forum rotates bbsessionhash host-only on www; sending both made the
    # session flip to guest at random.
    s = client._session
    s.cookies.set("bbsessionhash", "rotated", domain="www.unknowncheats.me")
    s.cookies.set("bbsessionhash", "evil", domain="evil.com")
    client._drop_superseded_cookies(s)
    got = {(c.domain, c.name): c.value for c in s.cookies.jar}
    assert got[("www.unknowncheats.me", "bbsessionhash")] == "rotated"
    assert (uc_http.COOKIE_DOMAIN, "bbsessionhash") not in got
    assert got[(uc_http.COOKIE_DOMAIN, "cf_clearance")] == "test-clearance"


def _page(token: str, body: str = "") -> str:
    return f'<script>var SECURITYTOKEN = "{token}";</script>{body}'


def test_error_template_with_a_logged_in_token_is_not_guest(client):
    # Error and throttle pages have no User CP or logout link; reading them as
    # a guest page hid "token has expired" behind a bogus login error.
    expired = _page(f"{int(time.time())}-abc", "Your submission could not be "
                    "processed because the token has expired.")
    client._check(expired, uc_http.BASE + "search.php?do=process")
    assert uc_http.TOKEN_ERROR.search(expired)
    with pytest.raises(uc_http.BlockedError, match="guest"):
        client._check(_page("guest"), uc_http.BASE)
    with pytest.raises(uc_http.BlockedError, match="guest"):
        client._check("<html>no token, no links</html>", uc_http.BASE)


def test_token_freshness():
    now = time.time()
    assert uc_http.token_fresh(f"{int(now - 60)}-abc", now)
    assert not uc_http.token_fresh(f"{int(now - 3 * 3600)}-abc", now)
    for bad in (None, "", "guest", "junk"):
        assert not uc_http.token_fresh(bad, now)


def test_stale_token_is_replaced_from_a_live_page(client, monkeypatch):
    now = int(time.time())
    stale, live = f"{now - 4 * 3600}-stale", f"{now - 5}-live"
    client.store.meta_set("securitytoken", stale)
    # A cached search.php outlives the token in it; it must not be the source.
    client.store.put("GET " + uc_http.BASE + "search.php",
                     uc_http.BASE + "search.php", _page(stale))
    monkeypatch.setattr(client.limiter, "acquire", lambda kind: None)
    monkeypatch.setattr(client._session, "get", lambda url, **kw: types.SimpleNamespace(
        text=_page(live), url=url, status_code=200))

    assert client.security_token() == live
    assert client.store.meta_get("securitytoken") == live
    assert client.token_status().startswith("fresh")
