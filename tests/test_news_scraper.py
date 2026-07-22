# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Hermetic unit tests for `backend.services.news_scraper`.

No real network. The httpx client, `feedparser`, and `trafilatura` are all
faked (`feedparser` isn't even installed in the OSS test env, so a stub is
injected into `sys.modules`). Coverage focuses on the pure extraction /
normalization / dedup helpers plus the source-scraper parsing logic driven by
canned RSS-entry / JSON payloads.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime

import pytest

import backend.services.news_scraper as ns


# ── Fake feedparser (module not installed in OSS env) ────────────────────────


class _FPEntry(dict):
    """feedparser FeedParserDict-alike: dict access + attribute access."""
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


def _install_fake_feedparser(monkeypatch, entries):
    mod = types.ModuleType("feedparser")

    def parse(text):
        feed = types.SimpleNamespace()
        feed.entries = entries
        return feed

    mod.parse = parse
    monkeypatch.setitem(sys.modules, "feedparser", mod)


# ── Fake httpx client ────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, text="", json_data=None, status_code=200, url=""):
        self.text = text
        self._json = json_data
        self.status_code = status_code
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            req = httpx.Request("GET", "http://example.com")
            resp = httpx.Response(self.status_code, request=req)
            raise httpx.HTTPStatusError("err", request=req, response=resp)

    def json(self):
        return self._json


def _install_fake_client(monkeypatch, responses):
    """Patch news_scraper.httpx.AsyncClient. Each instantiation pops the next
    response (a `_FakeResp` or a callable that raises)."""
    queue = list(responses)

    class _FakeClient:
        def __init__(self, *a, **k):
            self._resp = queue.pop(0) if queue else _FakeResp("")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def _reply(self, *a, **k):
            r = self._resp
            if callable(r):
                return r()
            return r

        get = _reply
        head = _reply

    monkeypatch.setattr(ns.httpx, "AsyncClient", _FakeClient)


# ── Pure utilities ───────────────────────────────────────────────────────────


class TestNormalizeUrl:
    def test_strips_trailing_slash_and_lowercases(self):
        assert ns._normalize_url("https://CNBC.com/story/") == "https://cnbc.com/story"

    def test_drops_query_and_fragment(self):
        got = ns._normalize_url("https://x.com/a?utm=1#frag")
        assert got == "https://x.com/a"


class TestDeduplicate:
    def test_removes_same_normalized_url(self):
        arts = [
            {"url": "https://x.com/a/", "title": "one"},
            {"url": "https://x.com/a?ref=2", "title": "dup"},
            {"url": "https://x.com/b", "title": "two"},
        ]
        out = ns._deduplicate(arts)
        assert [a["title"] for a in out] == ["one", "two"]

    def test_empty_url_dedups_to_one(self):
        out = ns._deduplicate([{"url": ""}, {"url": ""}])
        assert len(out) == 1


class TestExtractDomain:
    def test_strips_www(self):
        assert ns._extract_domain("https://www.cnbc.com/story") == "cnbc.com"

    def test_no_scheme_returns_empty(self):
        assert ns._extract_domain("not a url") == ""

    def test_subdomain_kept(self):
        assert ns._extract_domain("https://rss.nytimes.com/x") == "rss.nytimes.com"


class TestExtractTitleFromUrl:
    def test_last_segment_humanized(self):
        got = ns._extract_title_from_url("https://x.com/news/big-story-here.html")
        assert got == "Big Story Here"

    def test_no_path_returns_placeholder(self):
        assert ns._extract_title_from_url("https://x.com") == "Untitled Article"


class TestStripHtml:
    def test_removes_tags(self):
        assert ns._strip_html("<p>hi <b>there</b></p>") == "hi there"


class TestParseDate:
    def test_none(self):
        assert ns._parse_date(None) is None

    def test_iso_datetime(self):
        assert ns._parse_date("2026-07-22T10:30:00") == datetime(2026, 7, 22, 10, 30, 0)

    def test_date_only(self):
        assert ns._parse_date("2026-07-22") == datetime(2026, 7, 22)

    def test_garbage_returns_none(self):
        assert ns._parse_date("last tuesday") is None


class TestExtractOgMeta:
    def test_property_then_content(self):
        html = '<meta property="og:title" content="Hello &amp; Bye">'
        assert ns._extract_og_meta(html, "og:title") == "Hello & Bye"

    def test_content_then_property_reverse_order(self):
        html = '<meta content="Reversed" property="og:image">'
        assert ns._extract_og_meta(html, "og:image") == "Reversed"

    def test_missing_returns_none(self):
        assert ns._extract_og_meta("<html></html>", "og:title") is None


class TestExtractHtmlTitle:
    def test_basic(self):
        assert ns._extract_html_title("<title>My Page</title>") == "My Page"

    def test_strips_site_suffix(self):
        got = ns._extract_html_title("<title>Breaking News Story Today | CNN</title>")
        assert got == "Breaking News Story Today"

    def test_keeps_when_prefix_too_short(self):
        # left side <= 15 chars → don't strip
        got = ns._extract_html_title("<title>Short | Reuters</title>")
        assert got == "Short | Reuters"

    def test_none_when_absent(self):
        assert ns._extract_html_title("<html></html>") is None


class TestExtractMetaDescription:
    def test_prefers_og_description(self):
        html = '<meta property="og:description" content="' + "x" * 60 + '">'
        assert ns._extract_meta_description(html) == "x" * 60

    def test_falls_back_to_name_description(self):
        long = "y" * 60
        html = f'<meta name="description" content="{long}">'
        assert ns._extract_meta_description(html) == long

    def test_short_description_rejected(self):
        html = '<meta name="description" content="too short">'
        assert ns._extract_meta_description(html) is None


class TestExtractParagraphs:
    def test_pulls_article_paragraphs(self):
        html = (
            "<html><body><article>"
            "<p>" + "A" * 60 + "</p>"
            "<p>tiny</p>"  # < 30 chars, skipped
            "<p>" + "B" * 60 + "</p>"
            "</article></body></html>"
        )
        out = ns._extract_paragraphs(html)
        assert out is not None
        assert "A" * 60 in out and "B" * 60 in out
        assert "tiny" not in out

    def test_strips_script_and_style(self):
        html = (
            "<html><body>"
            "<script>var x=1;</script>"
            "<p>" + "Z" * 120 + "</p>"
            "</body></html>"
        )
        out = ns._extract_paragraphs(html)
        assert out is not None and "var x" not in out

    def test_too_little_returns_none(self):
        assert ns._extract_paragraphs("<html><body><p>hi</p></body></html>") is None


class TestExtractImageFromEntry:
    def test_media_content_image(self):
        e = _FPEntry(media_content=[{"url": "http://img/a.jpg", "type": "image/jpeg"}])
        assert ns._extract_image_from_entry(e) == "http://img/a.jpg"

    def test_media_thumbnail(self):
        e = _FPEntry(media_thumbnail=[{"url": "http://img/t.jpg"}])
        assert ns._extract_image_from_entry(e) == "http://img/t.jpg"

    def test_enclosure_image(self):
        e = _FPEntry(enclosures=[{"type": "image/png", "href": "http://img/e.png"}])
        assert ns._extract_image_from_entry(e) == "http://img/e.png"

    def test_img_in_summary(self):
        e = _FPEntry(summary='<img src="http://img/s.jpg">')
        assert ns._extract_image_from_entry(e) == "http://img/s.jpg"

    def test_none_when_absent(self):
        assert ns._extract_image_from_entry(_FPEntry()) is None


class TestExtractWithTrafilatura:
    def _fake_traf(self, monkeypatch, return_value=None, raises=False):
        mod = types.ModuleType("trafilatura")

        def bare_extraction(html, **k):
            if raises:
                raise RuntimeError("boom")
            return return_value

        mod.bare_extraction = bare_extraction
        monkeypatch.setitem(sys.modules, "trafilatura", mod)

    def test_dict_result_1x(self, monkeypatch):
        self._fake_traf(monkeypatch, {"text": "x" * 60, "title": "T", "author": "A"})
        out = ns._extract_with_trafilatura("<html>", "http://x")
        assert out["text"] == "x" * 60
        assert out["title"] == "T"
        assert out["author"] == "A"

    def test_object_result_2x(self, monkeypatch):
        doc = types.SimpleNamespace(text="y" * 60, title="OT", author="OA",
                                    date="2026-01-01", image="http://i")
        self._fake_traf(monkeypatch, doc)
        out = ns._extract_with_trafilatura("<html>", "http://x")
        assert out["text"] == "y" * 60
        assert out["title"] == "OT"
        assert out["image"] == "http://i"

    def test_short_text_becomes_none(self, monkeypatch):
        self._fake_traf(monkeypatch, {"text": "short", "title": "T"})
        out = ns._extract_with_trafilatura("<html>", "http://x")
        assert out["text"] is None

    def test_none_result(self, monkeypatch):
        self._fake_traf(monkeypatch, None)
        assert ns._extract_with_trafilatura("<html>", "http://x") == {}

    def test_exception_returns_empty(self, monkeypatch):
        self._fake_traf(monkeypatch, raises=True)
        assert ns._extract_with_trafilatura("<html>", "http://x") == {}


# ── _fetch_html (UA rotation) ────────────────────────────────────────────────


class TestFetchHtml:
    async def test_success_first_ua(self, monkeypatch):
        _install_fake_client(monkeypatch, [_FakeResp("<html>ok</html>")])
        assert await ns._fetch_html("http://x") == "<html>ok</html>"

    async def test_403_then_success_next_ua(self, monkeypatch):
        _install_fake_client(
            monkeypatch,
            [_FakeResp("", status_code=403), _FakeResp("<html>2</html>")],
        )
        assert await ns._fetch_html("http://x") == "<html>2</html>"

    async def test_generic_error_returns_none(self, monkeypatch):
        def _raise():
            raise ValueError("network down")
        _install_fake_client(monkeypatch, [_raise])
        assert await ns._fetch_html("http://x") is None


# ── _resolve_google_news_url ─────────────────────────────────────────────────


class TestResolveGoogleNewsUrl:
    async def test_non_google_returned_as_is(self):
        assert await ns._resolve_google_news_url("http://real.com/a") == "http://real.com/a"

    async def test_resolves_redirect(self, monkeypatch):
        _install_fake_client(monkeypatch, [_FakeResp(url="http://real.com/final")])
        got = await ns._resolve_google_news_url("http://news.google.com/rss/x")
        assert got == "http://real.com/final"

    async def test_unresolved_returns_none(self, monkeypatch):
        # head still lands on google.com → None
        _install_fake_client(monkeypatch, [_FakeResp(url="http://news.google.com/y")])
        assert await ns._resolve_google_news_url("http://news.google.com/rss/x") is None


# ── Source scrapers ──────────────────────────────────────────────────────────


class TestScrapeGoogleNews:
    async def test_parses_title_source_and_date(self, monkeypatch):
        entry = _FPEntry(
            link="http://news.google.com/rss/articles/abc",
            title="Big Story - Reuters",
            summary="<p>summary</p>",
            published_parsed=(2026, 7, 22, 9, 0, 0, 0, 0, 0),
        )
        _install_fake_feedparser(monkeypatch, [entry])
        _install_fake_client(monkeypatch, [_FakeResp("<rss/>")])
        monkeypatch.setattr(ns, "_resolve_google_news_url",
                            _amock("http://reuters.com/real"))
        out = await ns._scrape_google_news("story", max_results=5)
        assert len(out) == 1
        a = out[0]
        assert a["title"] == "Big Story"
        assert a["source_domain"] == "Reuters"
        assert a["url"] == "http://reuters.com/real"
        assert a["published_at"] == datetime(2026, 7, 22, 9, 0, 0)
        assert a["source"] == "Google News"


class TestScrapeBingNews:
    async def test_basic_parse(self, monkeypatch):
        entry = _FPEntry(link="http://x.com/story", title="Bing Story",
                         summary="<b>sum</b>")
        _install_fake_feedparser(monkeypatch, [entry])
        _install_fake_client(monkeypatch, [_FakeResp("<rss/>")])
        out = await ns._scrape_bing_news("story")
        assert out[0]["title"] == "Bing Story"
        assert out[0]["source"] == "Bing News"
        assert out[0]["summary"] == "sum"
        assert out[0]["published_at"] is None


class TestScrapeHackerNews:
    async def test_parses_hits(self, monkeypatch):
        data = {"hits": [
            {"title": "HN post", "url": "http://ext.com/x", "author": "bob",
             "points": 42, "created_at": "2026-07-22T00:00:00Z", "objectID": "1"},
            {"title": "Self post", "url": "", "objectID": "99", "points": 5,
             "created_at": None},
        ]}
        _install_fake_client(monkeypatch, [_FakeResp(json_data=data)])
        out = await ns._scrape_hackernews("q")
        assert out[0]["url"] == "http://ext.com/x"
        assert out[0]["engagement"] == 42
        assert out[0]["published_at"] == datetime(2026, 7, 22, 0, 0, 0)
        # self-post falls back to HN item link
        assert out[1]["url"] == "https://news.ycombinator.com/item?id=99"


class TestScrapeReddit:
    async def test_external_and_self_posts(self, monkeypatch):
        data = {"data": {"children": [
            {"data": {"title": "Ext", "url": "http://ext.com/a", "is_self": False,
                      "author": "u1", "score": 10, "created_utc": 1_600_000_000,
                      "thumbnail": "http://img/t.jpg"}},
            {"data": {"title": "Self", "url": "http://reddit.com/x", "is_self": True,
                      "permalink": "/r/x/1", "author": "u2", "score": 3,
                      "selftext": "body text", "thumbnail": "self"}},
        ]}}
        _install_fake_client(monkeypatch, [_FakeResp(json_data=data)])
        out = await ns._scrape_reddit_news("q")
        assert out[0]["url"] == "http://ext.com/a"
        assert out[0]["image_url"] == "http://img/t.jpg"
        assert out[0]["engagement"] == 10
        assert out[1]["url"] == "https://reddit.com/r/x/1"
        assert out[1]["source_domain"] == "reddit.com"
        assert out[1]["image_url"] is None  # "self" thumbnail not an http url
        assert out[1]["summary"] == "body text"


class TestScrapeRssFeed:
    async def test_query_filter_and_limit(self, monkeypatch):
        entries = [
            _FPEntry(title="Bitcoin surges", summary="crypto news", link="http://a/1"),
            _FPEntry(title="Weather today", summary="rain", link="http://a/2"),
            _FPEntry(title="Bitcoin ETF approved", summary="", link="http://a/3"),
        ]
        _install_fake_feedparser(monkeypatch, entries)
        _install_fake_client(monkeypatch, [_FakeResp("<rss/>")])
        out = await ns._scrape_rss_feed("http://feed", "Src", "bitcoin", max_results=5)
        titles = [a["title"] for a in out]
        assert titles == ["Bitcoin surges", "Bitcoin ETF approved"]
        assert all(a["source"] == "Src" for a in out)

    async def test_respects_max_results(self, monkeypatch):
        entries = [_FPEntry(title=f"news item {i}", summary="", link=f"http://a/{i}")
                   for i in range(5)]
        _install_fake_feedparser(monkeypatch, entries)
        _install_fake_client(monkeypatch, [_FakeResp("<rss/>")])
        out = await ns._scrape_rss_feed("http://feed", "Src", "news", max_results=2)
        assert len(out) == 2


# ── fetch_article_text extraction chain ──────────────────────────────────────


class TestFetchArticleText:
    async def test_empty_when_no_html(self, monkeypatch):
        monkeypatch.setattr(ns, "_fetch_html", _amock(None))
        out = await ns.fetch_article_text("http://x")
        assert out["text"] is None and out["word_count"] == 0

    async def test_trafilatura_precision_success(self, monkeypatch):
        monkeypatch.setattr(ns, "_fetch_html", _amock("<html>body</html>"))
        monkeypatch.setattr(
            ns, "_extract_with_trafilatura",
            lambda html, url, precision: {
                "text": "one two three four five", "title": "T",
                "author": "A", "date": "2026-01-01", "image": "http://i",
            } if precision else {},
        )
        out = await ns.fetch_article_text("http://x")
        assert out["text"] == "one two three four five"
        assert out["word_count"] == 5
        assert out["title"] == "T"
        assert out["author"] == "A"

    async def test_falls_back_to_paragraphs(self, monkeypatch):
        monkeypatch.setattr(ns, "_fetch_html", _amock("<html>body</html>"))
        monkeypatch.setattr(ns, "_extract_with_trafilatura",
                            lambda html, url, precision: {})
        monkeypatch.setattr(ns, "_extract_paragraphs", lambda html: "para text here")
        monkeypatch.setattr(ns, "_extract_og_meta", lambda html, prop: None)
        monkeypatch.setattr(ns, "_extract_html_title", lambda html: "HT")
        out = await ns.fetch_article_text("http://x")
        assert out["text"] == "para text here"
        assert out["title"] == "HT"

    async def test_falls_back_to_meta_description(self, monkeypatch):
        monkeypatch.setattr(ns, "_fetch_html", _amock("<html>body</html>"))
        monkeypatch.setattr(ns, "_extract_with_trafilatura",
                            lambda html, url, precision: {})
        monkeypatch.setattr(ns, "_extract_paragraphs", lambda html: None)
        monkeypatch.setattr(ns, "_extract_meta_description",
                            lambda html: "meta summary text")
        monkeypatch.setattr(ns, "_extract_og_meta", lambda html, prop: "OG Title")
        out = await ns.fetch_article_text("http://x")
        assert out["text"] == "meta summary text"
        assert out["title"] == "OG Title"


class TestFetchDirectUrl:
    async def test_structures_article(self, monkeypatch):
        monkeypatch.setattr(ns, "fetch_article_text", _amock({
            "text": "full body of the article", "title": "Direct T",
            "author": "Auth", "date": "2026-07-22", "image": "http://i",
            "word_count": 5,
        }))
        out = await ns.fetch_direct_url("http://news.com/some-story")
        assert out["title"] == "Direct T"
        assert out["source"] == "Direct URL"
        assert out["source_domain"] == "news.com"
        assert out["summary"] == "full body of the article"
        assert out["published_at"] == datetime(2026, 7, 22)

    async def test_title_from_url_when_missing(self, monkeypatch):
        monkeypatch.setattr(ns, "fetch_article_text", _amock({
            "text": None, "title": None, "author": None, "date": None,
            "image": None, "word_count": 0,
        }))
        out = await ns.fetch_direct_url("http://news.com/big-headline-story")
        assert out["title"] == "Big Headline Story"
        assert out["full_text"] is None


# ── scrape_news orchestration ────────────────────────────────────────────────


class TestScrapeNews:
    async def test_dedup_and_source_dispatch(self, monkeypatch):
        ns._cache.clear()
        monkeypatch.setattr(ns, "_scrape_google_news",
                            _amock([{"url": "http://a.com/x/", "title": "g"}]))
        monkeypatch.setattr(ns, "_scrape_bing_news",
                            _amock([{"url": "http://a.com/x?ref=1", "title": "b"}]))
        out = await ns.scrape_news("q", sources=["google", "bing"])
        # both point at the same normalized url → deduped to one
        assert len(out) == 1

    async def test_one_source_raises_others_survive(self, monkeypatch):
        ns._cache.clear()

        async def _boom(q, n):
            raise RuntimeError("source down")

        monkeypatch.setattr(ns, "_scrape_google_news", _boom)
        monkeypatch.setattr(ns, "_scrape_bing_news",
                            _amock([{"url": "http://ok.com/1", "title": "ok"}]))
        out = await ns.scrape_news("q", sources=["google", "bing"])
        assert len(out) == 1 and out[0]["title"] == "ok"

    async def test_unknown_source_ignored(self, monkeypatch):
        ns._cache.clear()
        monkeypatch.setattr(ns, "_scrape_google_news",
                            _amock([{"url": "http://ok.com/1", "title": "ok"}]))
        out = await ns.scrape_news("q", sources=["google", "does_not_exist"])
        assert len(out) == 1

    async def test_cache_hit_skips_scraping(self, monkeypatch):
        import time as _t
        ns._cache.clear()
        key = f"q:{','.join(sorted(['google']))}:15"
        ns._cache[key] = (_t.time(), [{"url": "http://cached/1", "title": "cached"}])

        async def _should_not_run(q, n):
            raise AssertionError("scraper ran despite cache hit")

        monkeypatch.setattr(ns, "_scrape_google_news", _should_not_run)
        out = await ns.scrape_news("q", sources=["google"])
        assert out[0]["title"] == "cached"
        ns._cache.clear()


# ── helper: async return value ───────────────────────────────────────────────


def _amock(return_value):
    async def _f(*a, **k):
        return return_value
    return _f
