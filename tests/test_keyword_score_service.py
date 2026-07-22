# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend/services/keyword_score_service.py.

Covers the opportunity-score formula + classification labels (with the two
data-fetchers mocked), the pytrends-backed volume + rising-keyword logic
(pytrends.request.TrendReq patched with a fake returning real pandas frames),
and the YouTube competition-index bucketing (httpx.get patched). NO real
network / trends I/O.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from backend.services import keyword_score_service as ks


# ── Fake pytrends ──────────────────────────────────────────────────────────
class FakeTrendReq:
    interest_df = None
    related_map = None
    topics_map = None

    def __init__(self, *a, **k):
        pass

    def build_payload(self, *a, **k):
        pass

    def interest_over_time(self):
        return type(self).interest_df

    def related_queries(self):
        return type(self).related_map or {}

    def related_topics(self):
        return type(self).topics_map or {}


class TestScoreKeyword:
    async def test_formula_and_labels(self):
        vol = {"volume_index": 50, "trend_direction": "stable", "related": ["a", "b"]}
        comp = {"competition_index": 90}
        with patch.object(ks, "_get_search_volume", AsyncMock(return_value=vol)), \
             patch.object(ks, "_get_competition", AsyncMock(return_value=comp)):
            out = await ks.score_keyword("finance")
        # raw = (0.5*0.6 + 0.5*0.1)/(0.9*0.3 + 0.01) = 0.35/0.28 = 1.25 → *30 = 37.5
        assert out["opportunity_score"] == pytest.approx(37.5, abs=0.01)
        assert out["search_volume"] == "medium"   # 50 in [30,60)
        assert out["competition"] == "high"        # 90 >= 60
        assert out["trend_direction"] == "stable"
        assert out["related_keywords"] == ["a", "b"]

    async def test_rising_trend_boosts_freshness_and_clamps_100(self):
        vol = {"volume_index": 90, "trend_direction": "rising", "related": []}
        comp = {"competition_index": 10}
        with patch.object(ks, "_get_search_volume", AsyncMock(return_value=vol)), \
             patch.object(ks, "_get_competition", AsyncMock(return_value=comp)):
            out = await ks.score_keyword("x")
        assert out["opportunity_score"] == 100.0
        assert out["search_volume"] == "high"
        assert out["competition"] == "low"

    async def test_defaults_when_fetchers_return_bare(self):
        with patch.object(ks, "_get_search_volume", AsyncMock(return_value={})), \
             patch.object(ks, "_get_competition", AsyncMock(return_value={})):
            out = await ks.score_keyword("x")
        assert out["volume_index"] == 50
        assert out["competition_index"] == 50
        assert out["trend_direction"] == "stable"


class TestGetSearchVolume:
    async def test_happy_path_rising(self):
        FakeTrendReq.interest_df = pd.DataFrame(
            {"kw": [10, 20, 30, 40, 50, 60], "isPartial": [False] * 6}
        )
        FakeTrendReq.related_map = {
            "kw": {"top": pd.DataFrame({"query": ["r1", "r2"], "value": [1, 2]})}
        }
        with patch("pytrends.request.TrendReq", FakeTrendReq):
            out = await ks._get_search_volume("kw")
        assert out["volume_index"] == 35  # avg of 10..60
        assert out["trend_direction"] == "rising"
        assert out["related"] == ["r1", "r2"]

    async def test_declining_direction(self):
        FakeTrendReq.interest_df = pd.DataFrame({"kw": [60, 50, 40, 20, 10, 5]})
        FakeTrendReq.related_map = {}
        with patch("pytrends.request.TrendReq", FakeTrendReq):
            out = await ks._get_search_volume("kw")
        assert out["trend_direction"] == "declining"

    async def test_empty_frame_defaults(self):
        FakeTrendReq.interest_df = pd.DataFrame()
        with patch("pytrends.request.TrendReq", FakeTrendReq):
            out = await ks._get_search_volume("kw")
        assert out == {"volume_index": 50, "trend_direction": "stable", "related": []}

    async def test_import_or_runtime_error_defaults(self):
        with patch("pytrends.request.TrendReq", side_effect=RuntimeError("boom")):
            out = await ks._get_search_volume("kw")
        assert out["volume_index"] == 50
        assert out["trend_direction"] == "stable"


class TestGetCompetition:
    async def test_no_key_returns_neutral(self, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "", raising=False)
        out = await ks._get_competition("kw", youtube_api_key="")
        assert out == {"competition_index": 50}

    async def test_low_band(self):
        resp = MagicMock(status_code=200)
        resp.json = MagicMock(return_value={"pageInfo": {"totalResults": 5000}})
        with patch("httpx.get", return_value=resp):
            out = await ks._get_competition("kw", youtube_api_key="K")
        # total < 10k → int(5000/10000*30) = 15
        assert out["competition_index"] == 15

    async def test_medium_band(self):
        resp = MagicMock(status_code=200)
        resp.json = MagicMock(return_value={"pageInfo": {"totalResults": 255_000}})
        with patch("httpx.get", return_value=resp):
            out = await ks._get_competition("kw", youtube_api_key="K")
        # 30 + int((255000-10000)/490000*40) = 30 + int(20.0) = 50
        assert out["competition_index"] == 50

    async def test_high_band_caps_100(self):
        resp = MagicMock(status_code=200)
        resp.json = MagicMock(return_value={"pageInfo": {"totalResults": 100_000_000}})
        with patch("httpx.get", return_value=resp):
            out = await ks._get_competition("kw", youtube_api_key="K")
        assert out["competition_index"] == 100

    async def test_non_200_returns_neutral(self):
        resp = MagicMock(status_code=403)
        with patch("httpx.get", return_value=resp):
            out = await ks._get_competition("kw", youtube_api_key="K")
        assert out["competition_index"] == 50

    async def test_exception_returns_neutral(self):
        with patch("httpx.get", side_effect=RuntimeError("net")):
            out = await ks._get_competition("kw", youtube_api_key="K")
        assert out["competition_index"] == 50


class TestDiscoverRisingKeywords:
    async def test_parses_rising_top_and_topics_sorted(self):
        FakeTrendReq.interest_df = pd.DataFrame({"seed": [1, 2, 3]})
        FakeTrendReq.related_map = {
            "seed": {
                "rising": pd.DataFrame({"query": ["kw1", "kw2"], "value": [300, "Breakout"]}),
                "top": pd.DataFrame({"query": ["kw3"], "value": [1]}),
            }
        }
        FakeTrendReq.topics_map = {
            "seed": {"rising": pd.DataFrame({"topic_title": ["Topic A"], "value": [900]})}
        }
        with patch("pytrends.request.TrendReq", FakeTrendReq):
            out = await ks.discover_rising_keywords("seed", max_results=15)
        kws = {r["keyword"]: r for r in out}
        assert "kw1" in kws and "kw2" in kws and "kw3" in kws and "Topic A" in kws
        # Breakout mapped to growth 5000 and sorts first
        assert kws["kw2"]["growth_pct"] == 5000 and kws["kw2"]["is_breakout"] is True
        assert kws["kw3"]["source"] == "top_query" and kws["kw3"]["growth_pct"] == 0
        assert out[0]["keyword"] == "kw2"  # highest growth first

    async def test_seed_absent_returns_empty(self):
        FakeTrendReq.interest_df = pd.DataFrame({"seed": [1, 2, 3]})
        FakeTrendReq.related_map = {}   # seed not present
        FakeTrendReq.topics_map = {}
        with patch("pytrends.request.TrendReq", FakeTrendReq):
            out = await ks.discover_rising_keywords("seed")
        assert out == []

    async def test_error_returns_empty(self):
        with patch("pytrends.request.TrendReq", side_effect=RuntimeError("boom")):
            out = await ks.discover_rising_keywords("seed")
        assert out == []
