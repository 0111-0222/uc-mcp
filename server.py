"""uc-mcp — read-only MCP access to the UnknownCheats forum.

Five tools, deliberately. Every tool description is paid for in context on every
turn of every session, so the surface is kept small and the wording is spent on
telling the model *when* to reach for it rather than on restating the arguments.

Read-only by design: nothing here can post, reply, rate, report or PM.
"""

from __future__ import annotations

import functools
import sys
import time
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import uc_dates
import uc_parse as P
import uc_render as R
from uc_http import (
    BUDGET_DAY,
    BUDGET_HOUR,
    INTERVALS,
    TTL,
    BlockedError,
    BudgetError,
    UCError,
    client,
    reset_client,
)

# stdio transport is JSON-RPC: stdout belongs to the protocol, diagnostics go to
# stderr, and the pipe must be UTF-8 or a post with a box-drawing character kills
# the server.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

mcp = FastMCP("unknowncheats")

SINCE = {
    "any": "0", "week": "lastweek", "2weeks": "twoweeks", "month": "lastmonth",
    "3months": "threemonth", "6months": "sixmonth", "year": "lastyear",
}
SORTS = {"newest": ("lastpost", "descending"), "oldest": ("lastpost", "ascending"),
         "replies": ("replycount", "descending"), "views": ("views", "descending")}
MODES = ("posts", "threads")

# Nothing here writes, and every result comes from a third-party site.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=True)


def _tool(fn):
    """Register a tool whose failures come back as readable text.

    A parse slip or an expired cookie must not surface as a protocol error: the
    model needs the message to tell the user what to fix.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return f"uc-mcp error: {e}"
    return mcp.tool(annotations=READ_ONLY)(wrapper)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _search_id(key: str) -> str | None:
    """A server-side searchid for this exact query, if the forum still holds it.

    vBulletin expires stored searches, so an old id pages into an error page
    rather than results; only ids younger than the search TTL are reused.
    """
    saved = client().store.meta_get(key)
    if isinstance(saved, dict) and time.time() - saved.get("ts", 0) < TTL["search"]:
        return saved.get("id")
    return None


def _forum_id(slug: str) -> str | None:
    """Map a slug to the numeric forum id that search scoping needs.

    The board index carries ids for every board and the sub-forums listed under
    it; anything deeper falls back to reading the id off its own page. Both
    results are cached.
    """
    c = client()
    table = c.store.meta_get("forum_ids") or {}
    if table.get(slug):
        return table[slug]

    if slug not in table:
        index = c.get("index.php", kind="meta", ttl_key="index")
        found = {row["slug"]: row["id"] for row in P.parse_forum_index(index.text)}
        table.update({k: v for k, v in found.items() if v or k not in table})
        if table.get(slug):
            c.store.meta_set("forum_ids", table)
            return table[slug]

    try:
        page = c.get(f"{slug}/", kind="read", ttl_key="forum")
    except (BlockedError, BudgetError):
        raise  # a dead session is not a bad slug; say what is actually wrong
    except UCError:
        return None
    fid = P.forum_id_from_page(page.text)
    if fid:
        table[slug] = fid
        c.store.meta_set("forum_ids", table)
    return fid


@_tool
def uc_search(query: str, forum: str = "", since: str = "any", sort: str = "newest",
              mode: str = "posts", title_only: bool = False,
              page: int = 1, limit: int = 15) -> str:
    """Search UnknownCheats — the reference source for game hacking, reverse
    engineering and anti-cheat work.

    Reach for this FIRST, before answering from memory or searching the web,
    whenever the question touches: game cheat/hack development, anti-cheat
    (EAC, BattlEye, VAC, Vanguard, FACEIT, ACE), memory reading/writing,
    offsets, signatures/patterns, structs and RE of a specific game, DMA and
    hardware cheats, hooking, injection, driver and kernel-mode work, Unity and
    Unreal internals, ESP/aimbot technique, or bypass and detection-vector
    questions. Public documentation on these topics is thin, stale or wrong;
    this forum is where the current answers actually are.

    Skip it for anything outside that domain — a call costs ~17s.

    Args:
        query: ONE "quoted phrase", or one or two rare words. The engine ORs
            loose terms and stops counting at 500, so extra words WIDEN the
            result set and common words like "detection" or "injection"
            saturate it — a long natural-language query returns the forum's
            newest posts, not its best ones. Measured: '"manual mapping"' with
            mode="threads", title_only=True gives 105 on-topic threads;
            '"manual map" injection detection' gives 500 of nothing.
        forum: optional subforum slug (e.g. "rust", "anti-cheat-bypass") to
            scope the search. Get slugs from uc_forum(). Measured on the same
            query: 105 hits unscoped, 23 in anti-cheat-bypass, 2 in rust.
        since: any | week | 2weeks | month | 3months | 6months | year. Use a
            window when the answer must reflect the game's current build —
            anything older is likely dead on arrival.
        sort: newest | oldest | replies | views. Newest is the safe default here
            because this material rots; sort by replies to find the canonical
            long-running thread on a topic.
        mode: "posts" returns individual matching posts with a dated snippet —
            best for a how-do-I question, but it saturates the 500-result cap
            easily. "threads" returns one row per thread — pair it with
            title_only for the sharpest results this forum can give.
        title_only: match thread titles only. The strongest precision lever
            here by a wide margin. Start with title_only=True, mode="threads";
            fall back to post mode only if that finds nothing.
        page: results page, 20 per page.
        limit: rows to render from that page.
    """
    c = client()
    now = _now()
    query = query.strip()
    forum = forum.strip("/ ").lower()
    if len(query.strip('"')) < 3:
        return "uc-mcp error: the forum rejects search words under 3 characters."
    if since not in SINCE:
        return f"uc-mcp error: since must be one of {', '.join(SINCE)}."
    if sort not in SORTS:
        return f"uc-mcp error: sort must be one of {', '.join(SORTS)}."
    if mode not in MODES:
        return f"uc-mcp error: mode must be one of {', '.join(MODES)}."
    page, limit = _clamp(page, 1, 25), _clamp(limit, 1, 20)
    sortby, order = SORTS[sort]

    # A server-side searchid serves every page of a query as a cheap cached
    # read, skipping both the 15s throttle and the forum's expensive re-query.
    key = f"searchid:{query}|{forum}|{since}|{sort}|{mode}|{title_only}"
    sid = _search_id(key)
    html = url = None
    if sid:
        got = c.get(f"search.php?searchid={sid}&pp=20&page={page}",
                    kind="read", ttl_key="search")
        html, url = got.text, got.url
        # An id the forum has already purged answers with an error, not results.
        if P.search_error(html) not in (None, "no matches"):
            html = None

    if html is None:
        data = {
            "do": "process", "query": query, "securitytoken": c.security_token(),
            "showposts": "1" if mode == "posts" else "0", "quicksearch": "1", "s": "",
            "titleonly": "1" if title_only else "0",
            "searchdate": SINCE[since], "beforeafter": "after",
            "sortby": sortby, "order": order, "exactname": "1",
            "starteronly": "0", "replyless": "0", "replylimit": "0",
        }
        if forum:
            fid = _forum_id(forum)
            if not fid:
                return (f'uc-mcp error: no subforum slug "{forum}". '
                        "Call uc_forum() with no argument to list valid slugs.")
            data["forumchoice[]"] = fid
            data["childforums"] = "1"
        got = c.post("search.php?do=process", data)
        html, url = got.text, got.url

        if "SECURITYTOKEN" in html and "invalid" in html[:3000].lower():
            data["securitytoken"] = c.security_token(refresh=True)
            got = c.post("search.php?do=process", data)
            html, url = got.text, got.url

        sid = P.search_id(url) or P.search_id(html)
        if sid:
            c.store.meta_set(key, {"id": sid, "ts": time.time()})
            # The POST always lands on page 1; fetch the page actually asked for.
            if page > 1:
                got = c.get(f"search.php?searchid={sid}&pp=20&page={page}",
                            kind="read", ttl_key="search")
                html, url = got.text, got.url

    err = P.search_error(html)
    if err == "no matches":
        hint = ""
        if since != "any":
            hint = f' Nothing within "{since}" — widen with since="any".'
        return (f'UC search: {query} — no matches.{hint}'
                " Try fewer or more common keywords.")
    if err:
        return f"uc-mcp: {err}"

    if mode == "posts":
        rows, meta = P.parse_post_search(html, c.tz_offset, now)
        if not rows:
            return ('UC search returned a page with no parsable results. '
                    f'Inspect it with uc_fetch("{url}").')
        return R.hits(rows[:limit], meta, query, mode, sort, now)

    listing = P.parse_thread_list(html, c.tz_offset, now)
    listing.threads = listing.threads[:limit]
    if not listing.threads:
        return ('UC search returned a page with no parsable results. '
                f'Inspect it with uc_fetch("{url}").')
    return R.threads(listing, f'UC search: {query} · threads · sorted {sort}', now)


@_tool
def uc_thread(path: str, page: int | str = 1, chars: int = 1500) -> str:
    """Read the posts of one UnknownCheats thread, each stamped with its date.

    Args:
        path: the thread path or full URL exactly as printed by uc_search or
            uc_forum, e.g. "rust/164256-rust-reversal-structs-offsets.html".
        page: page number, or "last" / -1 for the newest posts. On a thread that
            has run for years the last page is usually the only current part —
            page 1 can be a decade old.
        chars: per-post character budget before truncation. Raise it when a
            single post holds the code or structure you actually need.
    """
    c = client()
    now = _now()
    parts = P.split_thread_url(path)
    if not parts:
        return ('uc-mcp error: could not read a thread path out of '
                f'"{path}". Expected something like '
                '"rust/164256-rust-reversal-structs-offsets.html", as printed '
                "by uc_search or uc_forum.")
    forum, tid, slug, _ = parts
    chars = _clamp(chars, 200, 20000)

    want_last = str(page).strip().lower() in ("last", "-1")
    first = 1 if want_last else max(1, int(page))
    got = c.get(P.thread_path(forum, tid, slug, first), kind="read", ttl_key="thread")
    posts, meta = P.parse_posts(got.text, c.tz_offset, chars, now)

    if want_last and meta.pages > 1:
        got = c.get(P.thread_path(forum, tid, slug, meta.pages),
                    kind="read", ttl_key="thread")
        posts, meta = P.parse_posts(got.text, c.tz_offset, chars, now)
        first = meta.pages

    if not posts:
        return (f"uc-mcp: no posts parsed from {got.url}. The thread may be "
                f'deleted or permission-gated. Inspect with uc_fetch("{got.url}").')
    return R.thread(posts, meta, P.thread_path(forum, tid, slug), first, now)


@_tool
def uc_forum(slug: str = "", page: int = 1, limit: int = 25) -> str:
    """Browse UnknownCheats by subforum.

    With no slug, lists every subforum and its slug — call that once to learn
    which board a game or topic lives in. With a slug, lists that board's
    threads newest-activity first, each with its last-post date, so you can see
    at a glance whether a board is alive and what is current on it.

    Args:
        slug: subforum slug, e.g. "rust", "valorant", "anti-cheat-bypass".
        page: page of the thread list.
        limit: threads to render.
    """
    c = client()
    now = _now()
    if not slug.strip("/ "):
        page_ = c.get("index.php", kind="meta", ttl_key="index")
        rows = P.parse_forum_index(page_.text)
        if rows:
            c.store.meta_set("forum_ids", {r["slug"]: r["id"] for r in rows})
        return R.forums(rows, now)

    slug = slug.strip("/ ").lower()
    page, limit = _clamp(page, 1, 10000), _clamp(limit, 1, 50)
    known = c.store.meta_get("forum_ids") or {}
    if known and slug not in known:
        return (f'uc-mcp error: no subforum slug "{slug}". Call uc_forum() with no '
                "argument to list valid slugs — it must be the URL segment, not the "
                "display name.")
    path = f"{slug}/" if page <= 1 else f"{slug}/index{page}.html"
    got = c.get(path, kind="read", ttl_key="forum")
    listing = P.parse_thread_list(got.text, c.tz_offset, now)
    listing.threads = listing.threads[:limit]
    if not listing.threads:
        return (f'uc-mcp: no threads parsed for "{slug}". Check the slug with '
                "uc_forum() (no argument) — it must be the URL segment, not the "
                "display name.")
    return R.threads(listing, f"UC forum /{slug}", now)


@_tool
def uc_fetch(path: str, chars: int = 8000) -> str:
    """Fetch any UnknownCheats page as cleaned text. Fallback only.

    Use when a structured tool returns nothing parsable, or for a page the other
    tools do not model (member profiles, wiki pages). Requests to any host other
    than unknowncheats.me are refused, as are account and posting endpoints, so
    a link quoted inside a forum post cannot redirect this anywhere.

    Args:
        path: path under /forum/ or a full unknowncheats.me URL.
        chars: output budget.
    """
    got = client().get(path, kind="read", ttl_key="raw")
    text = P.readable(got.text, _clamp(chars, 200, 40000))
    return f"{got.url}{' (cached)' if got.cached else ''}\n{R.BANNER}\n\n{text}"


@_tool
def uc_session(reload: bool = False, clear_cache: bool = False) -> str:
    """Session health, request budget and cache state.

    Call with reload=True after re-exporting cookies.json — the cf_clearance
    cookie is short-lived, so a run of Cloudflare errors is normally fixed by
    refreshing that file and reloading, with no restart needed.
    """
    if reload:
        reset_client()
    c = client()
    cleared = c.store.clear_pages() if clear_cache else 0
    hour, day = c.store.usage()
    cached, newest = c.store.cache_stats()
    probe = "not checked (reload=True probes it)"
    if reload:
        c.get("index.php", kind="meta", ttl_key="index", refresh=True)
        probe = "logged in, Cloudflare passed"
    info = {
        "session": probe,
        "cookies": ", ".join(c.cookie_names) or "none",
        "cf_clearance": "present" if c.has_clearance() else
                        "MISSING — Cloudflare will block every request",
        "bbpassword": ("present — a long-lived login token; it can be left out of "
                       "the export (see README)") if c.has_persistent_login() else
                      "absent (session expires on its own)",
        "forum_timezone": f"GMT{c.tz_offset:+d} (dates are converted to UTC)",
        "requests_this_hour": f"{hour}/{BUDGET_HOUR}",
        "requests_today": f"{day}/{BUDGET_DAY}",
        "cached_pages": f"{cached}{f' (cleared {cleared})' if clear_cache else ''}",
        "cache_newest": (uc_dates.stamp(
            datetime.fromtimestamp(newest, timezone.utc)) if newest else "empty"),
        "limits": (f"{INTERVALS['search']:.0f}s between searches, "
                   f"{INTERVALS['read']:.0f}s between reads, "
                   f"{BUDGET_HOUR}/hour, {BUDGET_DAY}/day"),
    }
    return R.status(info)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
