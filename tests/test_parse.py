from datetime import datetime, timezone

import pytest

import uc_parse as P

UTC = timezone.utc


@pytest.mark.parametrize("href, expected", [
    ("rust/164256-rust-reversal-structs-offsets.html",
     ("rust", "164256", "rust-reversal-structs-offsets", 1)),
    ("https://www.unknowncheats.me/forum/rust/164256-rust-reversal-structs-offsets-12.html",
     ("rust", "164256", "rust-reversal-structs-offsets", 12)),
    ("/forum/anti-cheat-bypass/500-some-topic-new-post.html",
     ("anti-cheat-bypass", "500", "some-topic", 1)),
    ("example/77-a-thread-post123456.html", ("example", "77", "a-thread", 1)),
])
def test_split_thread_url(href, expected):
    assert P.split_thread_url(href) == expected


def test_split_thread_url_rejects_non_threads():
    assert P.split_thread_url("members/111.html") is None
    assert P.split_thread_url("") is None


def test_thread_path_round_trip():
    assert P.thread_path("rust", "1", "a-b") == "rust/1-a-b.html"
    assert P.thread_path("rust", "1", "a-b", 3) == "rust/1-a-b-3.html"


def test_parse_posts(fixture, now):
    posts, meta = P.parse_posts(fixture("thread.html"), 1, 1500, now)
    assert meta.title == "[Release] Example Entity List Walker"
    assert (meta.page, meta.pages) == (1, 3)
    assert [p.number for p in posts] == [1, 2]

    first, second = posts
    assert first.author == "alice_dev"
    assert first.posted == datetime(2015, 12, 12, 10, 38, tzinfo=UTC)
    assert (first.joined, first.post_count, first.reputation) == ("Jun 2014", 1234, 5678)
    assert first.edited.startswith("alice_dev 2016-01-04")
    assert second.reputation == -3
    assert second.posted == datetime(2026, 9, 29, 3, 2, tzinfo=UTC)


def test_post_body_keeps_code_and_collapses_quotes(fixture, now):
    posts, _ = P.parse_posts(fixture("thread.html"), 1, 1500, now)
    body = posts[0].body
    assert "```\nuintptr_t list = read<uintptr_t>(base + 0x1234);" in body
    assert "    walk(list, i);" in body          # indentation inside code survives
    assert "[quotes bob_re]" in body
    assert "does this still work" not in body    # quoted text is dropped
    assert "Code:" not in body and "Quote:" not in body
    assert "the repo <https://github.com/example/walker>" in body
    assert ":Smile:" in body


def test_post_body_truncates(fixture, now):
    posts, _ = P.parse_posts(fixture("thread.html"), 1, 40, now)
    assert "more chars, raise chars= to see" in posts[0].body


def test_parse_thread_list(fixture, now):
    listing = P.parse_thread_list(fixture("forum.html"), 1, now)
    assert (listing.page, listing.pages) == (1, 42)
    sticky, question = listing.threads

    assert sticky.path == "example-game/2000001-example-structs-offsets.html"
    assert sticky.title == "Example Structs and Offsets"
    assert sticky.prefix == "Coding"
    assert sticky.sticky is True
    assert sticky.pages == 617
    assert sticky.author == "carol_sdk"
    assert (sticky.replies, sticky.views) == (12345, 2345678)
    assert sticky.last_post == datetime(2026, 9, 30, 10, 29, tzinfo=UTC)

    assert question.prefix == "Question"
    assert question.sticky is False
    assert question.pages is None
    assert question.last_post == datetime(2019, 3, 14, 19, 15, tzinfo=UTC)


def test_parse_post_search(fixture, now):
    hits, meta = P.parse_post_search(fixture("search_posts.html"), 1, now)
    assert (meta.total, meta.page, meta.pages) == (27, 2, 2)
    (hit,) = hits
    assert hit.pid == "3000001"
    assert hit.title == "Visibility check with raycasts"
    assert hit.prefix == "Release"
    assert hit.path == "example-game/3000100-visibility-check-raycasts.html"
    assert hit.author == "grace"
    assert (hit.replies, hit.views) == (57, 8431)
    assert hit.posted == datetime(2025, 3, 3, 20, 15, tzinfo=UTC)
    assert hit.snippet.startswith("Only the static geometry needs")
    assert "..." not in hit.snippet   # the duplicated permalink line is dropped


def test_parse_forum_index_includes_subforum_ids(fixture):
    rows = P.parse_forum_index(fixture("index.html"))
    assert [(r["slug"], r["id"]) for r in rows] == [
        ("example-game", "87"),
        ("example-game-releases", "584"),
        ("example-game-questions", "585"),
        ("anti-cheat-bypass", "191"),
    ]
    assert rows[1]["name"] == "Example Game / Releases"


def test_forum_id_from_page(fixture):
    assert P.forum_id_from_page(fixture("forum.html")) == "555"
    assert P.forum_id_from_page("<html></html>") is None


def test_search_id():
    assert P.search_id("https://www.unknowncheats.me/forum/search.php?searchid=123") == "123"
    assert P.search_id("nothing") is None


@pytest.mark.parametrize("html, expected", [
    ("This forum requires that you wait 15 seconds between searches. Please try "
     "again in 9 seconds.", "it wants 15s between searches, 9s left"),
    ("Sorry - no matches. Please try some different terms.", "no matches"),
    ("Invalid Search specified. If you followed a valid link", "no longer holds"),
    ("<html>fine</html>", None),
])
def test_search_error(html, expected):
    got = P.search_error(html)
    if expected is None:
        assert got is None
    else:
        assert expected in got


def test_readable_strips_chrome():
    html = "<html><head><title>t</title><script>x()</script></head><body><p>hello</p></body></html>"
    assert P.readable(html) == "hello"
