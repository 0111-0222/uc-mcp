"""HTML to structured records.

Every selector here was confirmed against live authenticated HTML, not guessed.
UnknownCheats runs a vBulletin 3-era template set:

  forum index      td[id^=f<forumid>] holding the forum link
  thread list      td[id^=td_threadtitle_<tid>], a#thread_title_<tid>,
                   and a sibling cell whose title attribute is
                   "Replies: N, Views: M"
  thread page      table[id^=post<postid>], first td.thead carries the date,
                   div[id^=post_message_<postid>] carries the body
  search (posts)   table[id^=post<postid>] with "Forum:" + date in td.thead and
                   an em-wrapped snippet in the body cell
  search (threads) identical to a forum thread list

Post bodies keep code blocks (rendered as pre.prettyprint) because on this forum
the code *is* the answer, but collapse quote blocks to a single attribution line
since nested quotes are almost pure token cost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from bs4 import BeautifulSoup, NavigableString, Tag

import uc_dates

THREAD_HREF = re.compile(
    r"(?:https?://[^/]+)?/?(?:forum/)?([a-z0-9][a-z0-9\-]*)/(\d+)-([a-z0-9\-]+?)"
    r"(?:-(\d+))?\.html", re.I)
FORUM_CELL_ID = re.compile(r"^f(\d+)$")
SUB_ICON_ID = re.compile(r"^forum_statusicon_(\d+)$")
POST_TABLE_ID = re.compile(r"^post(\d+)$")
REPLIES_VIEWS = re.compile(r"Replies:\s*([\d,]+),\s*Views:\s*([\d,]+)")
PAGE_OF = re.compile(r"Page\s+(\d+)\s+of\s+([\d,]+)")
RESULT_COUNT = re.compile(r"Showing results\s+([\d,]+)\s+to\s+([\d,]+)\s+of\s+([\d,]+)")
LAST_EDITED = re.compile(r"Last edited by\s+([^;]+);\s*([^.]+?)\.", re.S)
SEARCHID = re.compile(r"searchid=(\d+)")
JOINED = re.compile(r"Join Date:\s*([A-Za-z]{3}\s+\d{4})")
POSTCOUNT = re.compile(r"Posts:\s*([\d,]+)")
REPUTATION = re.compile(r"Reputation:\s*(-?[\d,]+)")


def _n(text: str) -> str:
    """Collapse every run of whitespace, newlines included, to single spaces."""
    return " ".join((text or "").replace("\xa0", " ").split())


def _int(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d-]", "", text)
    return int(digits) if digits not in ("", "-") else None


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def split_thread_url(href: str) -> tuple[str, str, str, int] | None:
    """('rust', '164256', 'rust-reversal-structs-offsets', page) from any UC thread link."""
    m = THREAD_HREF.search(href or "")
    if not m:
        return None
    forum, tid, slug, page = m.groups()
    # Trailing -postNNNN / -new-post are link decorations, not part of the slug.
    slug = re.sub(r"-(?:new-post|post\d+|print)$", "", slug)
    return forum, tid, slug, int(page) if page else 1


def thread_path(forum: str, tid: str, slug: str, page: int = 1) -> str:
    tail = f"-{page}" if page and page > 1 else ""
    return f"{forum}/{tid}-{slug}{tail}.html"


# ---------------------------------------------------------------- records


@dataclass
class Thread:
    tid: str
    title: str
    forum: str
    path: str
    prefix: str = ""
    author: str | None = None
    replies: int | None = None
    views: int | None = None
    last_post: datetime | None = None
    pages: int | None = None
    sticky: bool = False
    preview: str = ""


@dataclass
class Post:
    pid: str
    number: int | None
    author: str | None
    posted: datetime | None
    body: str
    edited: str | None = None
    joined: str | None = None
    post_count: int | None = None
    reputation: int | None = None


@dataclass
class Hit:
    """One post-level search result."""
    pid: str
    title: str
    forum: str
    path: str
    prefix: str = ""
    author: str | None = None
    posted: datetime | None = None
    replies: int | None = None
    views: int | None = None
    snippet: str = ""


@dataclass
class Listing:
    threads: list[Thread] = field(default_factory=list)
    page: int = 1
    pages: int = 1
    total: int | None = None
    title: str = ""


# ---------------------------------------------------------------- body text


SKIP_SRC = re.compile(r"(smilies|statusicon|ambience|buttons|images/icons|/misc/)", re.I)


def _render_node(node, out: list[str], tz: int, depth: int = 0) -> None:
    if isinstance(node, NavigableString):
        text = str(node)
        if text.strip():
            # Keep one space at each edge, or text either side of a link or
            # smiley is glued to it ("See<a>the repo</a>" -> "Seethe repo").
            lead = " " if text[0].isspace() else ""
            trail = " " if text[-1].isspace() else ""
            out.append(lead + _n(text) + trail)
        elif text and out and not out[-1].endswith(" "):
            out.append(" ")
        return
    if not isinstance(node, Tag):
        return

    name = node.name
    classes = node.get("class") or []

    if name in ("script", "style", "legend"):
        return
    # vBulletin labels each block "Quote:" / "Code:" above it; the rendering
    # below already marks both, so the label is pure duplication.
    if name == "div" and "smallfont" in classes:
        label = _n(node.get_text())
        if label in ("Quote:", "Code:", "PHP Code:", "HTML Code:"):
            return
    if name == "pre" and "prettyprint" in classes:
        # <pre> already carries real newlines; a separator would double them.
        code = re.sub(r"\n{3,}", "\n\n", node.get_text().replace("\r\n", "\n"))
        out.append("\n```\n" + code.strip("\n") + "\n```\n")
        return
    # vBulletin quote block: a cell containing "Originally Posted by <author>".
    if name == "td" and "alt2" in classes:
        who = node.find("strong")
        quoted = _n(node.get_text(" "))
        if "Originally Posted by" in quoted:
            name_of = _n(who.get_text()) if who else "someone"
            out.append(f"\n[quotes {name_of}]\n")
            return
    if name == "img":
        src = node.get("src", "")
        if SKIP_SRC.search(src):
            alt = _n(node.get("alt", ""))
            if alt and len(alt) < 20:
                out.append(f":{alt}:")
            return
        out.append("[img]")
        return
    if name == "a":
        href = node.get("href", "")
        text = _n(node.get_text(" "))
        if href.startswith("http") and text and href not in text:
            out.append(f"{text} <{href}>")
        else:
            out.append(text or href)
        return
    if name in ("br",):
        out.append("\n")
        return
    if name in ("div", "p", "tr", "li", "fieldset", "table"):
        out.append("\n")
        for child in node.children:
            _render_node(child, out, tz, depth + 1)
        out.append("\n")
        return
    for child in node.children:
        _render_node(child, out, tz, depth + 1)


CODE_FENCE = re.compile(r"(```\n.*?\n```)", re.S)


def _tidy(prose: str) -> str:
    prose = re.sub(r"[ \t]+", " ", prose)
    return re.sub(r" *\n *", "\n", prose)


def body_text(node: Tag, tz: int, limit: int = 1500) -> str:
    out: list[str] = []
    _render_node(node, out, tz)
    # Whitespace is squeezed in prose only: inside a code block the indentation
    # is part of the answer.
    parts = CODE_FENCE.split("".join(out))
    text = "".join(part if i % 2 else _tidy(part) for i, part in enumerate(parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if limit and len(text) > limit:
        more = len(text) - limit
        text = text[:limit].rstrip() + f"\n[... {more} more chars, raise chars= to see]"
    return text


# ---------------------------------------------------------------- parsers


def parse_forum_index(html: str) -> list[dict]:
    """Subforums from the board index, anchored on the td[id=f<id>] cell."""
    s = soup(html)
    forums = []
    for cell in s.find_all("td", id=FORUM_CELL_ID):
        link = cell.find("a", href=True)
        if not link:
            continue
        href = link["href"]
        m = re.search(r"/forum/([a-z0-9][a-z0-9\-]*)/?$", href, re.I)
        if not m:
            continue
        name = _n(link.get_text())
        if not name:
            continue
        # Sub-forums are rendered inside the parent's cell; list them as their own
        # rows so every slug the search tool accepts is discoverable. Each link
        # follows an img#forum_statusicon_<id>, which gives its id for free and
        # saves scoping a search from fetching the sub-forum's page to find it.
        subs = []
        for sub in cell.find_all("a", href=True):
            if sub is link:
                continue
            sm = re.search(r"/forum/([a-z0-9][a-z0-9\-]*)/?$", sub["href"], re.I)
            sub_name = _n(sub.get_text())
            if sm and sub_name and sm.group(1) != m.group(1):
                icon = sub.find_previous_sibling()
                im = SUB_ICON_ID.match(icon.get("id", "")) if icon is not None else None
                subs.append((sm.group(1), sub_name, im.group(1) if im else ""))

        forums.append({
            "id": FORUM_CELL_ID.match(cell["id"]).group(1),
            "slug": m.group(1),
            "name": name,
            "desc": "",
        })
        for sub_slug, sub_name, sub_id in subs:
            forums.append({"id": sub_id, "slug": sub_slug,
                           "name": f"{name} / {sub_name}", "desc": ""})
    return forums


def parse_thread_list(html: str, tz: int, now: datetime | None = None) -> Listing:
    """Thread rows from a forumdisplay page or a threads-mode search result."""
    s = soup(html)
    listing = Listing()
    listing.title = page_title(s)

    page_of = PAGE_OF.search(s.get_text(" ")[:60000])
    if page_of:
        listing.page = int(page_of.group(1))
        listing.pages = _int(page_of.group(2)) or 1
    counts = RESULT_COUNT.search(html)
    if counts:
        listing.total = _int(counts.group(3))

    for cell in s.find_all("td", id=re.compile(r"^td_threadtitle_\d+$")):
        tid = cell["id"].rsplit("_", 1)[1]
        link = cell.find("a", id=f"thread_title_{tid}") or cell.find(
            "a", href=THREAD_HREF)
        if not link:
            continue
        parts = split_thread_url(link.get("href", ""))
        if not parts:
            continue
        forum, _tid, slug, _pg = parts

        title_div = link.find_parent("div")
        prefix = ""
        if title_div:
            lead = title_div.get_text(" ").split(_n(link.get_text()))[0]
            pm = re.search(r"\[([^\]]{1,24})\]\s*$", _n(lead))
            if pm:
                prefix = pm.group(1)

        pages = None
        nav = cell.find("span", class_="smallfont")
        if nav and "Multi-page" in str(nav):
            nums = [_int(a.get_text()) for a in nav.find_all("a")
                    if _int(a.get_text()) is not None]
            last = nav.find_all("a")[-1] if nav.find_all("a") else None
            if last is not None:
                p = split_thread_url(last.get("href", ""))
                if p:
                    pages = p[3]
            if not pages and nums:
                pages = max(nums)

        author = None
        starter = cell.find(attrs={"onclick": re.compile(r"members/\d+")})
        if starter:
            author = _n(starter.get_text())

        replies = views = None
        last_post = None
        row = cell.find_parent("tr")
        if row:
            for sibling in row.find_all("td"):
                title_attr = sibling.get("title", "")
                m = REPLIES_VIEWS.search(title_attr)
                if m:
                    replies, views = _int(m.group(1)), _int(m.group(2))
                    last_post = uc_dates.parse(_n(sibling.get_text(" ")), tz, now)
                    break
            if replies is None:
                tail = [c for c in row.find_all("td") if c is not cell]
                nums = [_int(c.get_text()) for c in tail[-2:]]
                if len(nums) == 2 and all(n is not None for n in nums):
                    replies, views = nums

        listing.threads.append(Thread(
            tid=tid,
            title=_n(link.get_text()),
            forum=forum,
            path=thread_path(forum, tid, slug),
            prefix=prefix,
            author=author,
            replies=replies,
            views=views,
            last_post=last_post,
            pages=pages,
            sticky="Sticky" in cell.get_text(" ")[:200],
            preview=_n(cell.get("title", ""))[:300],
        ))
    return listing


def page_title(s: BeautifulSoup) -> str:
    """vBulletin puts the thread/forum name in <title>, suffixed with the board name."""
    if s.title and s.title.string:
        return re.sub(r"\s*-\s*UnknownCheats.*$", "", _n(s.title.string))[:120]
    return ""


def parse_posts(html: str, tz: int, chars: int = 1500,
                now: datetime | None = None) -> tuple[list[Post], Listing]:
    """Posts from a showthread page, plus the page/pagination context."""
    s = soup(html)
    meta = Listing()
    meta.title = page_title(s)
    page_of = PAGE_OF.search(s.get_text(" ")[:60000])
    if page_of:
        meta.page = int(page_of.group(1))
        meta.pages = _int(page_of.group(2)) or 1

    posts: list[Post] = []
    for table in s.find_all("table", id=POST_TABLE_ID):
        pid = POST_TABLE_ID.match(table["id"]).group(1)
        message = table.find("div", id=f"post_message_{pid}")
        if message is None:
            continue

        head = table.find("td", class_="thead")
        posted = uc_dates.parse(_n(head.get_text(" ")) if head else "", tz, now)

        number = None
        counter = table.find(id=f"postcount{pid}")
        if counter is not None:
            number = _int(counter.get("name")) or _int(counter.get_text())

        author = None
        who = table.find("a", class_="bigusername")
        if who is None:
            who = table.find("a", href=re.compile(r"members/\d+\.html"))
        if who is not None:
            author = _n(who.get_text())

        sidebar = table.find("td", class_="alt2")
        joined = post_count = reputation = None
        if sidebar is not None:
            side_text = _n(sidebar.get_text(" "))
            jm = JOINED.search(side_text)
            joined = jm.group(1) if jm else None
            pm = POSTCOUNT.search(side_text)
            post_count = _int(pm.group(1)) if pm else None
            rm = REPUTATION.search(side_text)
            reputation = _int(rm.group(1)) if rm else None

        edited = None
        cell = table.find("td", id=f"td_post_{pid}") or table
        em = LAST_EDITED.search(cell.get_text(" "))
        if em:
            when = uc_dates.parse(_n(em.group(2)), tz, now)
            edited = f"{_n(em.group(1))} {uc_dates.stamp(when, now)}" if when else _n(em.group(1))

        posts.append(Post(
            pid=pid,
            number=number,
            author=author,
            posted=posted,
            body=body_text(message, tz, chars),
            edited=edited,
            joined=joined,
            post_count=post_count,
            reputation=reputation,
        ))
    return posts, meta


def parse_post_search(html: str, tz: int, now: datetime | None = None
                      ) -> tuple[list[Hit], Listing]:
    """Post-mode search results: one table[id=post<id>] per matching post."""
    s = soup(html)
    meta = Listing()
    counts = RESULT_COUNT.search(html)
    if counts:
        meta.total = _int(counts.group(3))
    page_of = PAGE_OF.search(s.get_text(" ")[:60000])
    if page_of:
        meta.page = int(page_of.group(1))
        meta.pages = _int(page_of.group(2)) or 1

    hits: list[Hit] = []
    for table in s.find_all("table", id=POST_TABLE_ID):
        pid = POST_TABLE_ID.match(table["id"]).group(1)
        head = table.find("td", class_="thead")
        if head is None:
            continue
        posted = uc_dates.parse(_n(head.get_text(" ")), tz, now)

        cell = table.find("td", class_="alt1")
        if cell is None:
            continue
        link = None
        for a in cell.find_all("a", href=True):
            if split_thread_url(a["href"]) and _n(a.get_text()):
                link = a
                break
        if link is None:
            continue
        forum, tid, slug, _pg = split_thread_url(link["href"])

        text = _n(cell.get_text(" "))
        rm = re.search(r"Replies:\s*([\d,]+)", text)
        vm = re.search(r"Views:\s*([\d,]+)", text)
        am = cell.find("a", href=re.compile(r"members/\d+\.html"))

        prefix = ""
        holder = link.find_parent("div")
        if holder:
            lead = holder.get_text(" ").split(_n(link.get_text()))[0]
            pm = re.search(r"\[([^\]]{1,24})\]\s*$", _n(lead))
            if pm:
                prefix = pm.group(1)

        snippet = ""
        quote = cell.find("em")
        if quote is not None:
            # The snippet block opens with a permalink whose text repeats the
            # first line of the excerpt; drop it instead of printing it twice.
            head = quote.find("a")
            if head is not None:
                head.decompose()
            snippet = _n(quote.get_text(" "))[:400]

        hits.append(Hit(
            pid=pid,
            title=_n(link.get_text()),
            forum=forum,
            path=thread_path(forum, tid, slug),
            prefix=prefix,
            author=_n(am.get_text()) if am else None,
            posted=posted,
            replies=_int(rm.group(1)) if rm else None,
            views=_int(vm.group(1)) if vm else None,
            snippet=snippet,
        ))
    return hits, meta


FORUM_ID_ON_PAGE = (
    re.compile(r'id="threadbits_forum_(\d+)"'),
    re.compile(r'name="forumid"\s+value="(\d+)"'),
    re.compile(r'name="forumchoice\[\]"\s+value="(\d+)"'),
    re.compile(r"forumdisplay\.php\?f=(\d+)"),
)


def forum_id_from_page(html: str) -> str | None:
    """Numeric forum id from a forumdisplay page — the only place sub-forums expose it."""
    for pattern in FORUM_ID_ON_PAGE:
        m = pattern.search(html)
        if m:
            return m.group(1)
    return None


def search_id(html_or_url: str) -> str | None:
    m = SEARCHID.search(html_or_url or "")
    return m.group(1) if m else None


def search_error(html: str) -> str | None:
    """vBulletin's throttle and empty-result pages, as a readable reason."""
    throttle = re.search(
        r"requires that you wait\s+(\d+)\s+seconds between searches"
        r"(?:[^<]*?try again in\s+(\d+)\s+seconds)?", html, re.I)
    if throttle:
        again = throttle.group(2)
        return (f"the forum's own search throttle rejected this (it wants "
                f"{throttle.group(1)}s between searches"
                + (f", {again}s left" if again else "") + "). Retry shortly.")
    if re.search(r"perform another search|only perform.{0,40}search", html, re.I):
        return "the forum's own search throttle rejected this. Retry shortly."
    if re.search(r"Invalid\s+Search(?:\s+ID)?\s+specified", html, re.I):
        return "the forum no longer holds this search. Run it again."
    if re.search(r"Sorry[^<]{0,40}no matches|no results were found|returned no matches",
                 html, re.I):
        return "no matches"
    if "search.php" in html and "must be at least" in html.lower():
        return "the query was rejected: search words must be at least 3 characters."
    return None


def readable(html: str, limit: int = 8000) -> str:
    s = soup(html)
    for tag in s(["script", "style", "nav", "footer", "head"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", s.get_text("\n")))
    return text.strip()[:limit]
