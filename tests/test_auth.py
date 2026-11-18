"""The sign-in window and the saved login, without opening a browser or
touching the forum: the window is a stand-in that hands over cookies, and the
forum is whatever `probe` is told to answer."""

import io
import sys

import pytest

import uc_auth
import uc_cli
import uc_http
from uc_browser import Cookies

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/158.0.0.0"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_MCP_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(uc_auth.time, "sleep", lambda s: None)
    return tmp_path / "home"


def login(**jar):
    return Cookies(jar or {"bbuserid": "42", "bbpassword": "hash"}, UA, "chrome",
                   "login", uc_auth.LABEL)


def test_saved_login_round_trips_and_is_not_plain_text_on_windows(home):
    assert uc_auth.load() is None
    path = uc_auth.save(login(bbuserid="42", bbpassword="s3cret-hash"))
    got = uc_auth.load()
    assert got.jar == {"bbuserid": "42", "bbpassword": "s3cret-hash"}
    assert (got.user_agent, got.impersonate, got.source) == (UA, "chrome", "login")
    assert path.parent == home
    if sys.platform == "win32":
        assert b"s3cret-hash" not in path.read_bytes()
    else:
        assert path.stat().st_mode & 0o777 == 0o600


def test_forget_removes_login_and_profile(home):
    uc_auth.save(login())
    (uc_auth.profile_dir() / "Default").mkdir(parents=True)
    assert len(uc_auth.forget()) == 2
    assert uc_auth.load() is None and not uc_auth.profile_dir().exists()
    assert uc_auth.forget() == []


class FakeWindow:
    """A profile that holds `before` until the user is shown a window, and
    `after` once they have closed it."""

    def __init__(self, before, after, closes=True):
        self.jar, self.after, self.closes = before, after, closes
        self.shown = self.killed = False

    def __call__(self, exe):
        return self

    def show(self):
        self.shown = True

    def wait_closed(self, timeout):
        self.jar = self.after
        return self.closes

    def kill(self):
        self.killed = True

    def read(self):
        return dict(self.jar), UA


MEMBER = {"bbuserid": "42", "bbpassword": "h", "cf_clearance": "c"}
GUEST = {"bblastvisit": "1", "cf_clearance": "c"}


def _forum(monkeypatch, window, answer=lambda c: ("ok", "tester")):
    probed = []

    def probe(cookies):
        probed.append(cookies.jar)
        return answer(cookies)
    monkeypatch.setattr(uc_auth, "Window", window)
    monkeypatch.setattr(uc_auth, "probe", probe)
    return probed


def test_sign_in_reads_the_login_after_the_window_is_closed(monkeypatch):
    window = FakeWindow(GUEST, MEMBER)
    probed = _forum(monkeypatch, window)
    said = []

    got = uc_auth.sign_in(say=said.append, browser="chrome")
    # The forum is asked once, and only after the user is done.
    assert window.shown and probed == [MEMBER]
    assert got.jar == MEMBER == uc_auth.load().jar
    assert "then close the window" in said[0] and said[-1] == "Logged in as tester. Login saved."


def test_profile_already_logged_in_needs_no_window(monkeypatch):
    (uc_auth.profile_dir() / "Default").mkdir(parents=True)
    window = FakeWindow(MEMBER, MEMBER)
    _forum(monkeypatch, window)
    assert uc_auth.sign_in(browser="chrome").jar == MEMBER
    assert not window.shown


def test_refused_clearance_is_left_out_of_the_saved_login(monkeypatch):
    window = FakeWindow(GUEST, MEMBER)
    _forum(monkeypatch, window, lambda c: (
        ("blocked", "Cloudflare") if "cf_clearance" in c.jar else ("ok", "tester")))
    got = uc_auth.sign_in(browser="chrome")
    assert got.jar == {"bbuserid": "42", "bbpassword": "h"} == uc_auth.load().jar


def test_closing_without_remember_me_says_so_and_asks_the_forum_nothing(monkeypatch):
    window = FakeWindow(GUEST, GUEST)
    probed = _forum(monkeypatch, window)
    with pytest.raises(uc_auth.LoginError, match="Remember Me"):
        uc_auth.sign_in(browser="chrome")
    assert probed == [] and uc_auth.load() is None


def test_window_left_open_is_closed_at_the_timeout(monkeypatch):
    window = FakeWindow(GUEST, GUEST, closes=False)
    _forum(monkeypatch, window)
    with pytest.raises(uc_auth.LoginError, match="still open after 60s"):
        uc_auth.sign_in(timeout=60, browser="chrome")
    assert window.killed and uc_auth.load() is None


def test_no_browser_says_what_else_works(monkeypatch):
    monkeypatch.setattr(uc_auth, "find_browser", lambda: None)
    with pytest.raises(uc_auth.LoginError, match="--paste"):
        uc_auth.sign_in()


def test_window_keeps_only_forum_login_cookies():
    window = uc_auth.Window("chrome")
    window.call = lambda method, params=None: {"cookies": [
        {"name": "bbuserid", "value": "42", "domain": "www.unknowncheats.me", "expires": 9e9},
        {"name": "cf_clearance", "value": "old", "domain": ".unknowncheats.me", "expires": 1e9},
        {"name": "cf_clearance", "value": "new", "domain": ".unknowncheats.me", "expires": 2e9,
         "partitionKey": {"topLevelSite": "https://unknowncheats.me"}},
        {"name": "_ga", "value": "tracking", "domain": ".unknowncheats.me", "expires": 9e9},
        {"name": "bbpassword", "value": "other", "domain": "evilunknowncheats.me"},
        {"name": "SID", "value": "google", "domain": ".google.com"},
    ]}
    assert window.cookies() == {"bbuserid": "42", "cf_clearance": "new"}


BASH = ("curl 'https://www.unknowncheats.me/forum/index.php' \\\n"
        "  -H 'accept: text/html' \\\n"
        "  -b 'bbsessionhash=abc; _ga=GA1; bbuserid=42; cf_clearance=def' \\\n"
        f"  -H 'user-agent: {UA}'")
CMD = ('curl ^"https://www.unknowncheats.me/forum/index.php^" ^\n'
       '  -H ^"cookie: bbsessionhash=abc; bbuserid=42; cf_clearance=def^" ^\n'
       f'  -H ^"user-agent: {UA}^"')
RAW = ("Host: www.unknowncheats.me\n"
       "Cookie: bbsessionhash=abc; bbuserid=42; cf_clearance=def\n"
       f"User-Agent: {UA}\n")


@pytest.mark.parametrize("text", [BASH, CMD, RAW], ids=["bash", "cmd", "raw headers"])
def test_paste_is_understood_in_every_flavour(text):
    got = uc_auth.parse_paste(text)
    assert got.jar == {"bbsessionhash": "abc", "bbuserid": "42", "cf_clearance": "def"}
    assert got.user_agent == UA and got.impersonate == "chrome"


def test_paste_without_cookies_or_agent_explains_itself():
    with pytest.raises(uc_auth.LoginError, match="no forum cookies"):
        uc_auth.parse_paste("curl 'https://www.unknowncheats.me/'")
    with pytest.raises(uc_auth.LoginError, match="no User-Agent"):
        uc_auth.parse_paste("cookie: bbsessionhash=abc")


@pytest.fixture
def client():
    uc_http.reset_client()
    yield uc_http.client()
    uc_http.reset_client()


def test_saved_login_comes_first_and_missing_one_is_not_an_error(client, monkeypatch):
    monkeypatch.setattr(uc_http.uc_browser, "firefox", lambda: None)
    monkeypatch.setattr(uc_http, "SOURCE", "auto")
    found, problems = client._candidates()
    assert [c.source for c in found] == ["file"] and "nobody has signed in yet" in problems
    uc_auth.save(login())
    assert [c.source for c in client._candidates()[0]] == ["login", "file"]


def test_nothing_to_log_in_with_points_at_the_login(client, monkeypatch):
    monkeypatch.setattr(uc_http, "SOURCE", "login")
    with pytest.raises(uc_http.UCError, match="uc_login"):
        client.load_cookies()


def test_cli_logout_and_status(capsys):
    uc_auth.save(login())
    assert uc_cli.run(["logout"], status=None) == 0
    assert uc_auth.load() is None
    assert uc_cli.run(["status"], status=lambda: "uc-mcp error: not signed in") == 1
    assert uc_cli.run(["status"], status=lambda: "session : logged in") == 0
    assert "session : logged in" in capsys.readouterr().out


def test_cli_paste_saves_only_a_login_that_works(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(BASH + "\n\n"))
    monkeypatch.setattr(uc_auth, "probe", lambda c: ("guest", "not logged in"))
    assert uc_cli.run(["login", "--paste"], status=None) == 1 and uc_auth.load() is None
    monkeypatch.setattr(sys, "stdin", io.StringIO(BASH + "\n\n"))
    monkeypatch.setattr(uc_auth, "probe", lambda c: ("ok", "tester"))
    assert uc_cli.run(["login", "--paste"], status=None) == 0
    assert uc_auth.load().jar["bbuserid"] == "42"
    assert "Logged in as tester" in capsys.readouterr().out
