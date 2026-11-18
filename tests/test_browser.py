"""Reading the login out of a Firefox profile, and recovering from a dead one
without the user doing anything. The profile here is built by hand: the suite
never opens a real browser's cookie store."""

import sqlite3
import time
import types

import pytest

import uc_browser
import uc_http

PAGE = '<script>var SECURITYTOKEN = "{}-abc";</script>'
CHALLENGE = "<title>Just a moment...</title>"


def make_profile(root, name, rows, version="160.0_2026/2026", prefs=""):
    profile = root / name
    profile.mkdir()
    db = sqlite3.connect(profile / "cookies.sqlite")
    db.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, expiry INTEGER,"
               " lastAccessed INTEGER, originAttributes TEXT)")
    db.executemany("INSERT INTO moz_cookies VALUES (?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    (profile / "compatibility.ini").write_text(f"[Compatibility]\nLastVersion={version}\n")
    (profile / "prefs.js").write_text(prefs)
    return profile


def row(name, value, *, host="www.unknowncheats.me", days=30, used=0, attrs=""):
    now = time.time()
    return (name, value, host, int((now + days * 86400) * 1000),
            int((now - used) * 1e6), attrs)


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(uc_browser, "profile_dirs",
                        lambda: [p for p in tmp_path.iterdir() if p.is_dir()])
    monkeypatch.delenv("UC_MCP_USER_AGENT", raising=False)
    return tmp_path


def test_reads_login_and_derives_the_user_agent(profiles):
    make_profile(profiles, "main", [
        row("bbuserid", "42"), row("bbpassword", "hash"),
        row("cf_clearance", "old", host=".unknowncheats.me", days=100),
        # Cloudflare's current, partitioned copy: set later, so it expires later.
        row("cf_clearance", "new", host=".unknowncheats.me", days=365,
            attrs="^partitionKey=%28https%2Cunknowncheats.me%29"),
        row("_ga", "tracking", host=".unknowncheats.me"),
        row("bbpassword", "other-site", host="www.example.com"),
        row("bblastvisit", "expired", days=-1),
    ])
    got = uc_browser.firefox()
    assert got.jar == {"bbuserid": "42", "bbpassword": "hash", "cf_clearance": "new"}
    assert got.impersonate == "firefox" and got.source == "firefox"
    assert "rv:160.0) Gecko/20100101 Firefox/160.0" in got.user_agent


def test_picks_the_profile_used_last_and_skips_logged_out_ones(profiles):
    make_profile(profiles, "stale", [row("bbuserid", "1"), row("bbpassword", "a", used=9000)])
    make_profile(profiles, "live", [row("bbuserid", "2"), row("bbpassword", "b", used=5)])
    make_profile(profiles, "guest", [row("cf_clearance", "c", host=".unknowncheats.me")])
    assert uc_browser.firefox().jar["bbuserid"] == "2"


def test_containers_are_not_mixed(profiles):
    make_profile(profiles, "main", [
        row("bbuserid", "1", used=500), row("bbpassword", "a", used=500),
        row("bbuserid", "2", attrs="^userContextId=3"),
        row("bbpassword", "b", attrs="^userContextId=3"),
    ])
    assert uc_browser.firefox().jar == {"bbuserid": "2", "bbpassword": "b"}


def test_user_agent_override_wins(profiles):
    make_profile(profiles, "main", [row("bbsessionhash", "s")],
                 prefs='user_pref("general.useragent.override", "Custom/1.0");\n')
    assert uc_browser.firefox().user_agent == "Custom/1.0"


def test_no_profile_is_not_an_error(profiles):
    assert uc_browser.firefox() is None


@pytest.fixture
def client(monkeypatch):
    uc_http.reset_client()
    c = uc_http.client()
    monkeypatch.setattr(c.limiter, "acquire", lambda kind: None)
    yield c
    # The request ledger is shared by the whole suite; leave it as it was found.
    with c.store._lock:
        c.store._db.execute("DELETE FROM hits")
        c.store._db.commit()
    uc_http.reset_client()


def _answer(monkeypatch, client_, working):
    """A forum that passes only the session installed with `working` cookies."""
    real_install = client_._install

    def install(cookies):
        real_install(cookies)
        ok = cookies.fingerprint == working.fingerprint
        monkeypatch.setattr(client_._session, "get", lambda url, **kw: types.SimpleNamespace(
            text=PAGE.format(int(time.time())) if ok else CHALLENGE,
            url=url, status_code=200 if ok else 403))
    monkeypatch.setattr(client_, "_install", install)
    return install


def test_blocked_request_recovers_with_fresh_cookies(client, monkeypatch):
    stale = uc_browser.Cookies({"cf_clearance": "old"}, "ua", "firefox", "firefox", "ff")
    fresh = uc_browser.Cookies({"cf_clearance": "new"}, "ua", "firefox", "firefox", "ff")
    install = _answer(monkeypatch, client, fresh)
    install(stale)
    monkeypatch.setattr(client, "_candidates", lambda: ([fresh], []))

    got = client.get("index.php", refresh=True)
    assert "SECURITYTOKEN" in got.text
    assert client.recoveries == 1 and client._fingerprint == fresh.fingerprint


def test_unchanged_cookies_are_not_retried(client, monkeypatch):
    stale = uc_browser.Cookies({"cf_clearance": "old"}, "ua", "firefox", "firefox", "ff")
    fresh = uc_browser.Cookies({"cf_clearance": "new"}, "ua", "firefox", "firefox", "ff")
    install = _answer(monkeypatch, client, fresh)
    install(stale)
    monkeypatch.setattr(client, "_candidates", lambda: ([stale], []))
    before = client.store.usage()[0]

    with pytest.raises(uc_http.BlockedError, match="in Firefox"):
        client.get("index.php", refresh=True)
    assert client.store.usage()[0] == before + 1  # one request, no pointless retry


def test_falls_back_to_the_file_when_firefox_is_dead(client, monkeypatch):
    dead = uc_browser.Cookies({"cf_clearance": "old"}, "ua", "firefox", "firefox", "ff")
    newer = uc_browser.Cookies({"cf_clearance": "also-dead"}, "ua", "firefox", "firefox", "ff")
    file_ = uc_browser.Cookies({"bbsessionhash": "s"}, "ua", "chrome", "file", "cookies.json")
    install = _answer(monkeypatch, client, file_)
    install(dead)
    monkeypatch.setattr(client, "_candidates", lambda: ([newer, file_], []))

    client.get("index.php", refresh=True)
    assert client.source == "file" and client.recoveries == 1


def test_refused_clearance_is_dropped_on_recovery(client, monkeypatch):
    with_cf = uc_browser.Cookies({"bbsessionhash": "s", "cf_clearance": "refused"},
                                 "ua", "chrome", "file", "cookies.json")
    install = _answer(monkeypatch, client, with_cf.without("cf_clearance"))
    install(with_cf)
    monkeypatch.setattr(client, "_candidates", lambda: ([with_cf], []))

    client.get("index.php", refresh=True)
    assert client.cookie_names == ["bbsessionhash"] and client.recoveries == 1


def test_source_selection(client, monkeypatch):
    ff = uc_browser.Cookies({"bbsessionhash": "s"}, "ua", "firefox", "firefox", "ff")
    monkeypatch.setattr(uc_browser, "firefox", lambda: ff)
    monkeypatch.setattr(uc_http, "SOURCE", "auto")
    assert [c.source for c in client._candidates()[0]] == ["firefox", "file"]
    monkeypatch.setattr(uc_http, "SOURCE", "file")
    assert [c.source for c in client._candidates()[0]] == ["file"]
    monkeypatch.setattr(uc_browser, "firefox", lambda: None)
    monkeypatch.setattr(uc_http, "SOURCE", "firefox")
    found, problems = client._candidates()
    assert not found and "no Firefox profile" in problems[0]
