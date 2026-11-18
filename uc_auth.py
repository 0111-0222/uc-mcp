"""Sign in through a real browser window, and keep the result somewhere safe.

Copying a Cookie header out of dev tools into a JSON file is the step people
get wrong, and it has to be repeated every time Cloudflare reissues its
clearance. This replaces it: open the user's own Chrome, Edge or Brave on a
profile that belongs to this server, let them log in the way they always do,
and once that window is closed, ask the profile what cookies it kept.

Nothing here types a password, solves a challenge or touches the profile the
user browses with. The browser does the logging in; this only reads what it
ends up holding, for one host.

The saved login lives outside the checkout, in the user's own data directory.
On Windows it is sealed with DPAPI (readable by this Windows account only);
elsewhere it is a file with owner-only permissions.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from curl_cffi import requests as cr

from uc_browser import HOST, Cookies

FORUM = f"https://www.{HOST}/forum/"
LABEL = "saved login"


class LoginError(Exception):
    """Sign-in did not finish; the message says what the user should do."""


# -- where things live ---------------------------------------------------
def home() -> Path:
    override = os.environ.get("UC_MCP_HOME")
    if override:
        return Path(override)
    user = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", user / "AppData" / "Local")) / "uc-mcp"
    if sys.platform == "darwin":
        return user / "Library" / "Application Support" / "uc-mcp"
    return Path(os.environ.get("XDG_DATA_HOME", user / ".local" / "share")) / "uc-mcp"


def session_file() -> Path:
    return home() / "session"


def profile_dir() -> Path:
    return home() / "browser"


# -- sealed storage ------------------------------------------------------
def _dpapi(data: bytes, seal: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = Blob(len(data), buf), Blob()
    fn = crypt32.CryptProtectData if seal else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("Windows could not " + ("seal" if seal else "open") + " the saved login")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def save(cookies: Cookies) -> Path:
    blob = json.dumps({"jar": cookies.jar, "user_agent": cookies.user_agent,
                       "impersonate": cookies.impersonate, "saved": time.time()}).encode()
    if sys.platform == "win32":
        blob = _dpapi(blob, seal=True)
    path = session_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # Created owner-only from the first byte, then swapped in whole, so a
    # server reading mid-write never sees half a login.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)
    return path


def load() -> Cookies | None:
    """The saved login, or None when nobody has signed in on this machine."""
    try:
        blob = session_file().read_bytes()
    except FileNotFoundError:
        return None
    if sys.platform == "win32":
        blob = _dpapi(blob, seal=False)
    data = json.loads(blob)
    return Cookies(dict(data["jar"]), data.get("user_agent", ""),
                   data.get("impersonate", "chrome"), "login", LABEL)


def forget() -> list[str]:
    """Delete the saved login and the sign-in browser profile."""
    gone = []
    for path in (session_file(), profile_dir()):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
        else:
            continue
        gone.append(str(path))
    return gone


# -- checking a login against the forum ----------------------------------
def probe(cookies: Cookies) -> tuple[str, str]:
    """One live request with these cookies: ("ok" | "guest" | "blocked", detail).

    This goes through the same TLS-impersonating client the server uses, so
    "ok" means the server will get in, not merely that the browser did.
    """
    import uc_http

    session = uc_http.make_session(cookies)
    try:
        resp = session.get(FORUM + "index.php", headers={"Referer": FORUM})
    except Exception as e:
        return "blocked", f"request failed: {type(e).__name__}: {e}"
    finally_url, text = str(resp.url), resp.text
    if not uc_http.on_host(finally_url):
        return "blocked", f"redirected off-site to {finally_url}"
    if any(m in text[:4000] for m in uc_http.BLOCK_MARKERS) or resp.status_code in (403, 503):
        return "blocked", "Cloudflare did not accept the browser's clearance"
    m = uc_http.TOKEN_RE.search(text)
    if not m or m.group(1) in ("guest", ""):
        return "guest", "not logged in"
    name = re.search(r"Welcome,\s*<(?:strong|b)>(?:<a[^>]*>)?\s*([^<]+)", text)
    return "ok", name.group(1).strip() if name else ""


def settle(cookies: Cookies) -> tuple[str, str, Cookies]:
    """Probe, and return the form of this login the server can actually use.

    Cloudflare checks a cf_clearance against the exact browser build that
    earned it. When the TLS profile here trails the user's browser, presenting
    the cookie gets a challenge where presenting none gets the page, so a
    clearance that is refused is left behind rather than saved.
    """
    state, detail = probe(cookies)
    if state == "blocked" and "cf_clearance" in cookies.jar:
        bare = cookies.without("cf_clearance")
        if bare.can_log_in():
            time.sleep(4)
            again = probe(bare)
            if again[0] != "blocked":
                return (*again, bare)
    return state, detail, cookies


# -- the sign-in window --------------------------------------------------
_WINDOWS = [r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
            r"Chromium\Application\chrome.exe"]
_MAC = ["Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"]
_LINUX = ["google-chrome", "google-chrome-stable", "microsoft-edge", "microsoft-edge-stable",
          "brave-browser", "chromium", "chromium-browser"]


def find_browser() -> str | None:
    """A Chromium-family browser already installed here. Firefox needs no
    window: its login is read straight from the profile (see uc_browser)."""
    override = os.environ.get("UC_MCP_BROWSER")
    if override:
        return override if os.path.exists(override) else shutil.which(override)
    if sys.platform == "win32":
        roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        paths = [os.path.join(r, p) for p in _WINDOWS for r in roots if r]
    elif sys.platform == "darwin":
        paths = [f"{base}/Applications/{n}.app/Contents/MacOS/{n}"
                 for n in _MAC for base in ("", str(Path.home()))]
    else:
        paths = [p for p in map(shutil.which, _LINUX) if p]
    return next((p for p in paths if os.path.exists(p)), None)


class Window:
    """A browser on the server's own profile, in two separate lives.

    `show` is a plain window with nothing attached to it, for the user to log
    in through. `read` is a second, invisible run of the same profile that
    opens no page at all and is only asked what cookies it holds. Cloudflare
    stalls a login made in a browser that has a debugging connection, so the
    forum never sees one: by the time the cookies are read, the window the
    user logged in with is gone.
    """

    def __init__(self, exe: str):
        self.exe = exe
        self.proc: subprocess.Popen | None = None
        self._ws = None
        self._id = 0

    def _launch(self, *args: str) -> subprocess.Popen:
        profile_dir().mkdir(parents=True, exist_ok=True)
        # stdio belongs to the MCP protocol when the server launches this, so
        # the browser gets none of it.
        return subprocess.Popen(
            [self.exe, f"--user-data-dir={profile_dir()}", "--no-first-run",
             "--no-default-browser-check", "--disable-background-mode", *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # -- the window the user sees ---------------------------------------
    def show(self) -> None:
        self.proc = self._launch("--new-window", FORUM)

    def wait_closed(self, timeout: float) -> bool:
        try:
            self.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=10)

    # -- asking the profile what it holds --------------------------------
    def read(self) -> tuple[dict[str, str], str]:
        """The forum's login cookies in the profile, and its User-Agent."""
        port_file = profile_dir() / "DevToolsActivePort"
        with contextlib.suppress(OSError):
            port_file.unlink()
        proc = self._launch("--headless=new", "--remote-debugging-port=0", "about:blank")
        try:
            deadline = time.time() + 30
            while self._ws is None:
                if time.time() > deadline or proc.poll() is not None:
                    raise LoginError(
                        f"{self.exe} could not open the sign-in profile. If a sign-in "
                        "window is still open, close it and try again.")
                try:
                    port, path = port_file.read_text().split()[:2]
                    self._ws = cr.Session().ws_connect(f"ws://127.0.0.1:{int(port)}{path}")
                except Exception:
                    time.sleep(0.25)
            agent = self.call("Browser.getVersion").get("userAgent", "")
            return self.cookies(), agent.replace("HeadlessChrome", "Chrome")
        finally:
            with contextlib.suppress(Exception):
                self.call("Browser.close")
            with contextlib.suppress(Exception):
                self._ws.close()
            self._ws = None
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._ws.send_json({"id": self._id, "method": method, "params": params or {}})
        while True:
            msg = self._ws.recv_json()
            if msg.get("id") == self._id:
                return msg.get("result") or {}

    def cookies(self) -> dict[str, str]:
        picked: dict[str, tuple[float, str]] = {}
        for c in self.call("Storage.getCookies").get("cookies", []):
            domain = c.get("domain", "").lstrip(".").lower()
            name = c.get("name", "")
            if not (domain == HOST or domain.endswith("." + HOST)):
                continue
            if not (name.startswith("bb") or name == "cf_clearance"):
                continue
            # Cloudflare keeps a partitioned and a plain cf_clearance side by
            # side; the one that expires last was set last.
            expires = float(c.get("expires") or -1)
            if name not in picked or expires > picked[name][0]:
                picked[name] = (expires, c.get("value", ""))
        return {name: value for name, (_exp, value) in picked.items()}


def _remembered(jar: dict[str, str]) -> bool:
    # A closed browser keeps only the "Remember Me" pair; bbsessionhash dies
    # with the window.
    return {"bbuserid", "bbpassword"} <= jar.keys()


def sign_in(timeout: float = 300, say=lambda msg: None, browser: str | None = None) -> Cookies:
    """Get a working login out of the sign-in profile, showing a window only
    if the profile is not already logged in. Saves the login and returns it.

    The forum gets one request per attempt (two if a clearance is refused).
    """
    exe = browser or find_browser()
    if not exe:
        raise LoginError(
            "no Chrome, Edge, Brave or Chromium found. Set UC_MCP_BROWSER to the browser's "
            "path, log into the forum in Firefox instead (it is picked up on its own), or "
            "run the login with --paste.")
    window = Window(exe)

    def check(wait_first: bool) -> tuple[str, str]:
        jar, agent = window.read()
        if not _remembered(jar):
            return "guest", "the browser is not holding a login"
        if wait_first:
            time.sleep(4)
        state, detail, usable = settle(Cookies(jar, agent, "chrome", "login", LABEL))
        if state == "ok":
            save(usable)
            say("Logged in" + (f" as {detail}" if detail else "") + ". Login saved.")
        return state, detail

    # Logged in here on an earlier day: nothing for the user to do.
    if (profile_dir() / "Default").exists() and check(False)[0] == "ok":
        return load()

    window.show()
    say(f"Opened {Path(exe).stem} on unknowncheats.me. Log in there with 'Remember Me' "
        "ticked, then close the window. If the forum drops you back on its login "
        "page after 'logging you in', the login still counted: just close the window.")
    if not window.wait_closed(timeout):
        window.kill()
        raise LoginError(
            f"the sign-in window was still open after {int(timeout)}s, so it was closed. "
            "Run the login again: log in with 'Remember Me' ticked, then close the window.")
    state, detail = check(True)
    if state == "ok":
        return load()
    if state == "guest":
        raise LoginError(
            "the window was closed without a login the browser kept. Log in with "
            "'Remember Me' ticked (without it the login is forgotten when the window "
            "closes), then close the window.")
    raise LoginError(f"the login was read but the forum did not accept it: {detail}.")


# -- pasted request, for machines with no browser ------------------------
def parse_paste(text: str) -> Cookies:
    """Cookies and User-Agent out of a pasted "Copy as cURL" or raw headers.

    Accepts what the dev tools of any browser put on the clipboard: bash or
    cmd flavoured cURL, or the raw request-header block.
    """
    if '^"' in text:  # cmd-flavoured cURL escapes everything with carets
        text = text.replace("^", "")
    header = (re.search(r"""(?is)(?:-b|--cookie)\s+(['"])(.*?)\1""", text)
              or re.search(r"""(?im)\bcookie:\s*()([^'"\r\n]+)""", text))
    agent = re.search(r"""(?im)\buser-agent:\s*([^'"\r\n]+)""", text)
    jar = {}
    for part in (header.group(2) if header else "").split(";"):
        name, eq, value = part.strip().partition("=")
        if eq and (name.startswith("bb") or name == "cf_clearance"):
            jar[name] = value.strip()
    if not jar:
        raise LoginError("no forum cookies in what was pasted. Copy the request with "
                         "right-click > Copy > Copy as cURL, from a logged-in tab.")
    if not agent:
        raise LoginError("no User-Agent in what was pasted. Cloudflare ties the login to "
                         "it, so copy the whole request (Copy as cURL), not just the cookie.")
    ua = agent.group(1).strip()
    return Cookies(jar, ua, "firefox" if "Firefox/" in ua else "chrome", "login", LABEL)
