# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Coverage suite for `backend.agents.news_scout.NewsScoutAgent` (OSS).

The agent runs a news-research pipeline:
  - multi-source scrape (parallel) → dedup → full-text fetch → AI analysis
    → store as ScoutResult(platform="news") → WS `news_results`
  - a direct-URL mode that skips search and analyzes a single article,
    with a last-resort AI text-extraction fallback
  - empty-sources handling (retry once, then a constraint warning + a
    success job with 0 results)
  - AI-analysis-failure handling (job failed + `job_failed`)

All network / AI / DB collaborators are mocked, so the tests are hermetic
and never touch RSS feeds or an LLM. `asyncio.sleep` is patched out so the
empty-sources 6-second retry pause doesn't slow the suite.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


def _run(coro):
    return asyncio.run(coro)


def _article(url, title="Headline", **overrides):
    base = {
        "url": url,
        "title": title,
        "source": "Example News",
        "source_domain": "example.com",
        "engagement": 100,
        "image_url": None,
        "published_at": datetime(2026, 7, 1),
        "virality_score": 42,
    }
    base.update(overrides)
    return base


def _make_session(scalar_queue):
    """Return (factory, session). One shared fake session is yielded from
    every `async with AsyncSessionLocal()` block so `.add` accumulates
    across the user-settings load and the store step.

    `scalar_queue` is popped once per `execute()`; exhausted ⇒ None
    (i.e. 'no existing row' / 'no user settings')."""
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()

    async def execute(stmt):
        result = MagicMock()
        val = scalar_queue.pop(0) if scalar_queue else None
        result.scalar_one_or_none = MagicMock(return_value=val)
        return result

    session.execute = execute

    @asynccontextmanager
    async def factory():
        yield session

    return factory, session


@pytest.fixture
def infra():
    with patch("backend.agents.news_scout.ws_manager.send", new=AsyncMock()) as send_ws, \
         patch("backend.agents.news_scout.ws_manager.send_constraint_warning",
               new=AsyncMock()) as warn, \
         patch("backend.agents.news_scout.update_job_status", new=AsyncMock()) as ujs, \
         patch("backend.agents.news_scout.asyncio.sleep", new=AsyncMock()):
        yield {"send": send_ws, "warn": warn, "ujs": ujs}


def _final_status_calls(ujs):
    out = []
    for c in ujs.await_args_list:
        if len(c.args) >= 2:
            out.append(c.args[1])
        elif "status" in c.kwargs:
            out.append(c.kwargs["status"])
    return out


def _ws_types(send_ws):
    return [
        c.args[0].get("type")
        for c in send_ws.await_args_list
        if c.args and isinstance(c.args[0], dict)
    ]


class TestMultiSourceFlow:
    def test_aggregates_fetches_analyzes_and_stores(self, infra):
        from backend.agents.news_scout import NewsScoutAgent
        factory, session = _make_session([])  # all execute() → None (new rows)

        arts = [_article("https://example.com/a"), _article("https://example.com/b")]
        analyzed = [
            _article("https://example.com/a", full_text="body a",
                     word_count=200, analysis={"angle": "x"}),
            _article("https://example.com/b", full_text="body b",
                     word_count=180, analysis={"angle": "y"}),
        ]

        with patch("backend.agents.news_scout.AsyncSessionLocal",
                   side_effect=lambda: factory()), \
             patch("backend.services.news_scraper.scrape_news",
                   new=AsyncMock(return_value=arts)), \
             patch("backend.services.news_scraper.fetch_article_text",
                   new=AsyncMock(return_value={"text": "full body", "word_count": 200})), \
             patch("backend.services.news_analyzer_service.analyze_articles",
                   new=AsyncMock(return_value=analyzed)):
            _run(NewsScoutAgent().run("job_1", "ai news", user_id="local"))

        assert "success" in _final_status_calls(infra["ujs"])
        types = _ws_types(infra["send"])
        assert "news_results" in types
        assert "job_complete" in types
        # Two net-new news rows persisted.
        assert session.add.call_count == 2
        session.commit.assert_awaited()

    def test_expanded_queries_are_scraped(self, infra):
        from backend.agents.news_scout import NewsScoutAgent
        factory, _session = _make_session([])
        scrape = AsyncMock(return_value=[_article("https://example.com/a")])

        with patch("backend.agents.news_scout.AsyncSessionLocal",
                   side_effect=lambda: factory()), \
             patch("backend.services.news_scraper.scrape_news", new=scrape), \
             patch("backend.services.news_scraper.fetch_article_text",
                   new=AsyncMock(return_value={"text": "x", "word_count": 5})), \
             patch("backend.services.news_analyzer_service.analyze_articles",
                   new=AsyncMock(return_value=[_article("https://example.com/a",
                                                        full_text="x", analysis={})])):
            _run(NewsScoutAgent().run(
                "job_1", "ai news",
                expanded_queries=["ai tools", "ai agents", "ignored-3rd"],
                user_id="local",
            ))
        # query + first 2 expanded queries = 3 scrape calls (3rd is dropped).
        assert scrape.await_count == 3

    def test_empty_sources_completes_success_with_zero(self, infra):
        from backend.agents.news_scout import NewsScoutAgent
        factory, _session = _make_session([])
        scrape = AsyncMock(return_value=[])  # both passes empty

        with patch("backend.agents.news_scout.AsyncSessionLocal",
                   side_effect=lambda: factory()), \
             patch("backend.services.news_scraper.scrape_news", new=scrape):
            _run(NewsScoutAgent().run("job_1", "obscure query", user_id="local"))

        # Retried once (2 passes × 1 query = 2 scrape calls).
        assert scrape.await_count == 2
        assert any(
            c.kwargs.get("constraint") == "news_sources_empty"
            for c in infra["warn"].await_args_list
        )
        assert "success" in _final_status_calls(infra["ujs"])
        # job_complete carried 0 results.
        complete = [
            c.args[0] for c in infra["send"].await_args_list
            if c.args and c.args[0].get("type") == "job_complete"
        ]
        assert complete and complete[0]["result"]["total_results"] == 0

    def test_ai_analysis_failure_marks_job_failed(self, infra):
        from backend.agents.news_scout import NewsScoutAgent
        factory, _session = _make_session([])

        with patch("backend.agents.news_scout.AsyncSessionLocal",
                   side_effect=lambda: factory()), \
             patch("backend.services.news_scraper.scrape_news",
                   new=AsyncMock(return_value=[_article("https://example.com/a")])), \
             patch("backend.services.news_scraper.fetch_article_text",
                   new=AsyncMock(return_value={"text": "x", "word_count": 5})), \
             patch("backend.services.news_analyzer_service.analyze_articles",
                   new=AsyncMock(side_effect=RuntimeError("LLM down"))):
            _run(NewsScoutAgent().run("job_1", "ai news", user_id="local"))

        assert "failed" in _final_status_calls(infra["ujs"])
        assert "job_failed" in _ws_types(infra["send"])

    def test_analysis_returns_empty_completes_with_zero(self, infra):
        from backend.agents.news_scout import NewsScoutAgent
        factory, _session = _make_session([])

        with patch("backend.agents.news_scout.AsyncSessionLocal",
                   side_effect=lambda: factory()), \
             patch("backend.services.news_scraper.scrape_news",
                   new=AsyncMock(return_value=[_article("https://example.com/a")])), \
             patch("backend.services.news_scraper.fetch_article_text",
                   new=AsyncMock(return_value={"text": "x", "word_count": 5})), \
             patch("backend.services.news_analyzer_service.analyze_articles",
                   new=AsyncMock(return_value=[])):
            _run(NewsScoutAgent().run("job_1", "ai news", user_id="local"))

        assert "success" in _final_status_calls(infra["ujs"])
        complete = [
            c.args[0] for c in infra["send"].await_args_list
            if c.args and c.args[0].get("type") == "job_complete"
        ]
        assert complete and complete[0]["result"]["total_results"] == 0


class TestDirectUrlMode:
    def test_direct_url_success(self, infra):
        from backend.agents.news_scout import NewsScoutAgent
        factory, session = _make_session([])
        art = _article("https://example.com/story", full_text="the body text",
                       word_count=120)

        with patch("backend.agents.news_scout.AsyncSessionLocal",
                   side_effect=lambda: factory()), \
             patch("backend.services.news_scraper.fetch_direct_url",
                   new=AsyncMock(return_value=art)), \
             patch("backend.services.news_analyzer_service.analyze_single_article",
                   new=AsyncMock(return_value={**art, "analysis": {"angle": "z"},
                                              "virality_score": 55})):
            _run(NewsScoutAgent().run(
                "job_1", "manual", direct_url="https://example.com/story",
                user_id="local",
            ))

        assert "success" in _final_status_calls(infra["ujs"])
        assert "news_results" in _ws_types(infra["send"])
        assert session.add.call_count == 1

    def test_direct_url_extraction_fails_then_ai_extract_also_fails(self, infra):
        from backend.agents.news_scout import NewsScoutAgent
        factory, _session = _make_session([])
        art = _article("https://example.com/paywalled", full_text="")  # no text

        ai_client = MagicMock()
        ai_client.chat = AsyncMock(return_value=None)  # AI extraction yields nothing

        with patch("backend.agents.news_scout.AsyncSessionLocal",
                   side_effect=lambda: factory()), \
             patch("backend.services.news_scraper.fetch_direct_url",
                   new=AsyncMock(return_value=art)), \
             patch("backend.services.news_scraper._fetch_html",
                   new=AsyncMock(return_value="<html><body>" + "word " * 200 + "</body></html>")), \
             patch("backend.core.ai_provider.get_ai_client", return_value=ai_client):
            _run(NewsScoutAgent().run(
                "job_1", "manual", direct_url="https://example.com/paywalled",
                user_id="local",
            ))

        assert "failed" in _final_status_calls(infra["ujs"])
        assert "job_failed" in _ws_types(infra["send"])

    def test_direct_url_ai_extract_rescues_empty_body(self, infra):
        from backend.agents.news_scout import NewsScoutAgent
        factory, session = _make_session([])
        art = _article("https://example.com/hard", full_text="")

        ai_client = MagicMock()
        ai_client.chat = AsyncMock(return_value="A clean extracted article body " * 10)

        with patch("backend.agents.news_scout.AsyncSessionLocal",
                   side_effect=lambda: factory()), \
             patch("backend.services.news_scraper.fetch_direct_url",
                   new=AsyncMock(return_value=art)), \
             patch("backend.services.news_scraper._fetch_html",
                   new=AsyncMock(return_value="<html><body>" + "word " * 300 + "</body></html>")), \
             patch("backend.core.ai_provider.get_ai_client", return_value=ai_client), \
             patch("backend.services.news_analyzer_service.analyze_single_article",
                   new=AsyncMock(side_effect=lambda a, us: {**a, "analysis": {},
                                                            "virality_score": 30})):
            _run(NewsScoutAgent().run(
                "job_1", "manual", direct_url="https://example.com/hard",
                user_id="local",
            ))

        assert "success" in _final_status_calls(infra["ujs"])
        assert session.add.call_count == 1


class TestStoreResultsDedup:
    """`_store_results` dedups by md5(url) within platform='news', so a
    re-scout that overlaps an existing row inserts only the net-new ones."""

    def test_existing_row_is_skipped(self, infra):
        from backend.agents.news_scout import NewsScoutAgent
        # Article #1 → no existing row (None) ⇒ inserted.
        # Article #2 → existing row returned ⇒ skipped.
        factory, session = _make_session([None, MagicMock()])
        articles = [
            _article("https://example.com/new", analysis={"a": 1}),
            _article("https://example.com/dupe", analysis={"a": 2}),
        ]
        with patch("backend.agents.news_scout.AsyncSessionLocal",
                   side_effect=lambda: factory()):
            stored = _run(NewsScoutAgent()._store_results(
                articles, "ai news", "job_1", "local",
            ))
        # Only the net-new article made it into storage + the display payload.
        assert len(stored) == 1
        assert session.add.call_count == 1
        session.commit.assert_awaited()

    def test_all_new_inserts_every_article(self, infra):
        from backend.agents.news_scout import NewsScoutAgent
        factory, session = _make_session([])  # always None ⇒ nothing pre-exists
        articles = [
            _article("https://example.com/1"),
            _article("https://example.com/2"),
            _article("https://example.com/3"),
        ]
        with patch("backend.agents.news_scout.AsyncSessionLocal",
                   side_effect=lambda: factory()):
            stored = _run(NewsScoutAgent()._store_results(
                articles, "ai news", "job_1", "local",
            ))
        assert len(stored) == 3
        assert session.add.call_count == 3
