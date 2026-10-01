"""The tool layer, exercised without touching the network: registration,
schemas, argument validation and error handling."""

import asyncio
import time
import types

import pytest

import server
import uc_parse as P
import uc_render as R

TOOLS = {"uc_search", "uc_thread", "uc_forum", "uc_fetch", "uc_session"}


def _tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def test_exactly_five_read_only_tools():
    tools = _tools()
    assert set(tools) == TOOLS
    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False


def test_schemas_survive_the_error_wrapper():
    search = _tools()["uc_search"].inputSchema
    assert search["required"] == ["query"]
    assert {"forum", "since", "sort", "mode", "title_only", "page", "limit"} <= \
        set(search["properties"])
    assert "Search UnknownCheats" in _tools()["uc_search"].description


@pytest.mark.parametrize("kwargs, message", [
    ({"query": "ab"}, "under 3 characters"),
    ({"query": '"a"'}, "under 3 characters"),
    ({"query": "offsets", "since": "decade"}, "since must be one of"),
    ({"query": "offsets", "sort": "best"}, "sort must be one of"),
    ({"query": "offsets", "mode": "users"}, "mode must be one of"),
])
def test_search_validates_before_sending(kwargs, message):
    assert message in server.uc_search(**kwargs)


def test_thread_rejects_unparsable_path():
    assert "could not read a thread path" in server.uc_thread("members/1.html")


def test_fetch_refuses_foreign_hosts_as_text():
    out = server.uc_fetch("https://evil.com?.unknowncheats.me/")
    assert out.startswith("uc-mcp error: refused")


def test_errors_become_text_not_exceptions(monkeypatch):
    def boom():
        raise RuntimeError("parser exploded")
    monkeypatch.setattr(server, "client", boom)
    assert server.uc_thread("rust/1-a.html") == "uc-mcp error: parser exploded"


def test_session_reports_without_a_request():
    out = server.uc_session()
    assert "not checked" in out
    assert "cf_clearance" in out and "present" in out
    assert "bbpassword" in out and "absent" in out


def test_render_flags_stale_content_and_result_cap(fixture, now):
    hits, meta = P.parse_post_search(fixture("search_posts.html"), 1, now)
    meta.total = 500
    out = R.hits(hits, meta, "x", "posts", "newest", now)
    assert "500+ (result cap)" in out
    assert "WARNING: this content is stale" in out  # 20 months old on this forum
    assert "<- OLD" in out and "<- ANCIENT" not in out
    assert R.BANNER in out


def test_render_thread_points_at_last_page(fixture, now):
    posts, meta = P.parse_posts(fixture("thread.html"), 1, 1500, now)
    out = R.thread(posts, meta, "x/1-a.html", 1, now)
    assert 'uc_thread("x/1-a.html", page="last")' in out
    assert "<- ANCIENT" in out


def test_search_retries_once_with_a_new_token(monkeypatch, fixture):
    c = server.client()
    now = int(time.time())
    monkeypatch.setattr(c.limiter, "acquire", lambda kind: None)
    c.store.meta_set("securitytoken", f"{now - 60}-old")
    sent = []

    def post(url, data=None, **kw):
        sent.append(data["securitytoken"])
        if len(sent) == 1:
            text = (f'var SECURITYTOKEN = "{now}-new"; Your submission could not '
                    "be processed because the token has expired.")
        else:
            text = fixture("search_posts.html")
        return types.SimpleNamespace(text=text, url=url, status_code=200)

    def get(url, **kw):
        return types.SimpleNamespace(
            text=f'var SECURITYTOKEN = "{now}-new";', url=url, status_code=200)

    monkeypatch.setattr(c._session, "post", post)
    monkeypatch.setattr(c._session, "get", get)
    out = server.uc_search("widowmaker", since="year")
    assert sent == [f"{now - 60}-old", f"{now}-new"]
    assert not out.startswith("uc-mcp error"), out
