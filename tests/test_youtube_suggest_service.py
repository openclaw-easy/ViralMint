# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend/services/youtube_suggest_service.py.

Covers the JSONP parsing of YouTube's autocomplete endpoint, the in-memory
TTL cache, the demand-expansion bucketing (primary / question / long-tail),
keyword extraction and the demand-summary builder. httpx.AsyncClient is
patched so NO real network I/O happens.
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services import youtube_suggest_service as ys


@pytest.fixture(autouse=True)
def _clear_cache():
    ys._cache.clear()
    yield
    ys._cache.clear()


def _jsonp_resp(suggestions):
    inner = ",".join(f'["{s}",0,[512]]' for s in suggestions)
    text = 'window.google.ac.h(["seed",[' + inner + '],{"a":"b"}])'
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.text = text
    return r


def _patch_client(resp=None, side_effect=None):
    client = MagicMock()
    if side_effect is not None:
        client.get = AsyncMock(side_effect=side_effect)
    else:
        client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch.object(ys.httpx, "AsyncClient", MagicMock(return_value=ctx)), client


class TestCacheHelpers:
    def test_set_and_get(self):
        ys._cache_set("k", ["a", "b"])
        assert ys._cache_get("k") == ["a", "b"]

    def test_expired_entry_evicted(self):
        ys._cache["k"] = (["a"], time.time() - (ys.CACHE_TTL + 10))
        assert ys._cache_get("k") is None
        assert "k" not in ys._cache

    def test_missing_key_returns_none(self):
        assert ys._cache_get("nope") is None

    def test_bounded_eviction(self):
        for i in range(505):
            ys._cache[f"k{i}"] = (["x"], float(i))
        ys._cache_set("new", ["y"])
        # eviction triggers when len > 500 → drops 100 oldest
        assert len(ys._cache) <= 500
        assert ys._cache_get("new") == ["y"]


class TestGetSuggestions:
    async def test_short_query_returns_empty(self):
        assert await ys.get_suggestions("a") == []
        assert await ys.get_suggestions("  ") == []

    async def test_parses_jsonp(self):
        p, client = _patch_client(_jsonp_resp(["save money fast", "save money tips"]))
        with p:
            out = await ys.get_suggestions("save money")
        assert out == ["save money fast", "save money tips"]

    async def test_respects_max_results(self):
        p, _ = _patch_client(_jsonp_resp([f"s{i}" for i in range(10)]))
        with p:
            out = await ys.get_suggestions("query", max_results=3)
        assert len(out) == 3

    async def test_caches_result(self):
        p, client = _patch_client(_jsonp_resp(["one", "two"]))
        with p:
            await ys.get_suggestions("finance")
            # second call served from cache — client.get not called again
            out2 = await ys.get_suggestions("finance")
        assert out2 == ["one", "two"]
        assert client.get.call_count == 1

    async def test_unparseable_response_returns_empty(self):
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.text = "garbage not jsonp"
        p, _ = _patch_client(r)
        with p:
            out = await ys.get_suggestions("query")
        assert out == []

    async def test_http_error_returns_empty(self):
        p, _ = _patch_client(side_effect=RuntimeError("boom"))
        with p:
            out = await ys.get_suggestions("query")
        assert out == []


class TestExtractTopKeywords:
    def test_counts_frequency_and_drops_stopwords_and_niche(self):
        suggestions = [
            "budgeting tips for beginners",
            "budgeting apps free",
            "budgeting spreadsheet budgeting",
        ]
        out = ys._extract_top_keywords("personal finance", suggestions)
        # "budgeting" appears >=2 times, is not a stopword/niche word
        assert "budgeting" in out
        # stopwords / short words excluded
        assert "for" not in out

    def test_niche_words_excluded(self):
        out = ys._extract_top_keywords("crypto trading", ["crypto trading guide crypto"])
        assert "crypto" not in out and "trading" not in out


class TestBuildDemandSummary:
    def test_levels_scale_with_count(self):
        many = [f"s{i}" for i in range(15)]
        summary = ys._build_demand_summary("x", many, [], [])
        assert "Very high" in summary

    def test_low_when_sparse(self):
        summary = ys._build_demand_summary("x", ["only one"], [], [])
        assert "Low" in summary

    def test_includes_sections(self):
        s = ys._build_demand_summary("x", ["p1"], ["q1"], ["l1"])
        assert "Top searches" in s and "Common questions" in s and "Long-tail" in s


class TestGetSearchDemand:
    async def test_empty_niche_returns_empty_structure(self):
        out = await ys.get_search_demand("   ")
        assert out["primary_suggestions"] == []
        assert out["top_keywords"] == []

    async def test_buckets_by_prefix_and_dedups(self):
        # prefixes[0]=niche → primary; [1..4] → questions; [5..6] → long_tail
        async def fake_suggest(prefix, language="en", n=8):
            mapping = {
                "finance": ["finance basics", "dup"],
                "how to finance": ["how to finance a car"],
                "what is finance": ["what is finance dept"],
                "best finance": ["best finance apps"],
                "why finance": ["why finance matters"],
                "finance for beginners": ["finance for beginners 2026"],
                "finance tips": ["dup", "finance tips daily"],  # "dup" already seen
            }
            return mapping.get(prefix, [])

        with patch.object(ys, "get_suggestions", side_effect=fake_suggest):
            out = await ys.get_search_demand("finance")

        assert "finance basics" in out["primary_suggestions"]
        assert "how to finance a car" in out["question_keywords"]
        assert "finance for beginners 2026" in out["long_tail_keywords"]
        # dedup: "dup" appears once total across all buckets
        all_sug = (out["primary_suggestions"] + out["question_keywords"]
                   + out["long_tail_keywords"])
        assert all_sug.count("dup") == 1
        assert out["niche"] == "finance"
        assert isinstance(out["demand_summary"], str) and out["demand_summary"]

    async def test_tolerates_exceptions_from_gather(self):
        async def fake_suggest(prefix, language="en", n=8):
            if prefix == "finance":
                return ["finance basics"]
            raise RuntimeError("one prefix failed")

        with patch.object(ys, "get_suggestions", side_effect=fake_suggest):
            out = await ys.get_search_demand("finance")
        # the one good prefix still produced a primary suggestion
        assert out["primary_suggestions"] == ["finance basics"]
