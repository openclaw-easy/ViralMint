# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend/services/trend_velocity_service.py.

Covers the Google-Trends velocity classifier (pytrends.request.TrendReq
patched with a fake over real pandas frames), the list/ranking helpers,
the cross-platform correlation scoring, the YouTube + Reddit signal parsers
(httpx.get patched), and the DB-driven per-user velocity sweep (AsyncSessionLocal
+ ws_manager patched). NO real network / DB / trends I/O.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from backend.services import trend_velocity_service as tv


class FakeTrendReq:
    df = None

    def __init__(self, *a, **k):
        pass

    def build_payload(self, *a, **k):
        pass

    def interest_over_time(self):
        return type(self).df


def _set_df(frame):
    FakeTrendReq.df = frame


class TestCheckKeywordVelocity:
    async def test_spike(self):
        _set_df(pd.DataFrame({"kw": [10] * 6 + [40] * 2}))
        with patch("pytrends.request.TrendReq", FakeTrendReq):
            a = await tv.check_keyword_velocity("kw")
        assert a.alert_level == "spike"
        assert a.velocity_multiplier == 4.0
        assert a.baseline_interest == 10.0 and a.current_interest == 40.0

    async def test_rising(self):
        _set_df(pd.DataFrame({"kw": [10] * 6 + [20] * 2}))
        with patch("pytrends.request.TrendReq", FakeTrendReq):
            a = await tv.check_keyword_velocity("kw")
        assert a.alert_level == "rising"

    async def test_steady(self):
        _set_df(pd.DataFrame({"kw": [10] * 8}))
        with patch("pytrends.request.TrendReq", FakeTrendReq):
            a = await tv.check_keyword_velocity("kw")
        assert a.alert_level == "steady"

    async def test_declining(self):
        _set_df(pd.DataFrame({"kw": [40] * 6 + [5] * 2}))
        with patch("pytrends.request.TrendReq", FakeTrendReq):
            a = await tv.check_keyword_velocity("kw")
        assert a.alert_level == "declining"

    async def test_empty_frame_steady(self):
        _set_df(pd.DataFrame())
        with patch("pytrends.request.TrendReq", FakeTrendReq):
            a = await tv.check_keyword_velocity("kw")
        assert a.alert_level == "steady"
        assert a.current_interest == 0

    async def test_single_point_steady(self):
        _set_df(pd.DataFrame({"kw": [42]}))
        with patch("pytrends.request.TrendReq", FakeTrendReq):
            a = await tv.check_keyword_velocity("kw")
        assert a.alert_level == "steady"
        assert a.current_interest == 42

    async def test_error_steady(self):
        with patch("pytrends.request.TrendReq", side_effect=RuntimeError("boom")):
            a = await tv.check_keyword_velocity("kw")
        assert a.alert_level == "steady"
        assert a.velocity_multiplier == 1.0


class TestGetTrendingKeywords:
    async def test_sorted_by_velocity(self):
        def _alert(kw):
            vel = {"a": 1.2, "b": 3.5, "c": 0.5}[kw]
            return tv.TrendAlert(kw, 10, 10, vel, "rising")

        with patch.object(tv, "check_keyword_velocity", AsyncMock(side_effect=lambda k: _alert(k))), \
             patch.object(tv.asyncio, "sleep", AsyncMock()):
            out = await tv.get_trending_keywords(["a", "b", "c"])
        assert [r["keyword"] for r in out] == ["b", "a", "c"]
        assert out[0]["velocity"] == 3.5

    async def test_caps_at_10(self):
        with patch.object(tv, "check_keyword_velocity",
                          AsyncMock(return_value=tv.TrendAlert("x", 1, 1, 1.0, "steady"))), \
             patch.object(tv.asyncio, "sleep", AsyncMock()):
            out = await tv.get_trending_keywords([f"k{i}" for i in range(20)])
        assert len(out) == 10


class TestCrossPlatformCorrelation:
    async def test_all_three_trending_very_high(self):
        alert = tv.TrendAlert("kw", 80, 20, 4.0, "spike")
        yt = {"is_trending": True, "recency_score": 2.0}
        rd = {"is_trending": True, "engagement_score": 2.0}
        with patch.object(tv, "check_keyword_velocity", AsyncMock(return_value=alert)), \
             patch.object(tv, "_check_youtube_signal", AsyncMock(return_value=yt)), \
             patch.object(tv, "_check_reddit_signal", AsyncMock(return_value=rd)):
            out = await tv.cross_platform_correlation("kw")
        assert out["confidence"] == "very_high"
        assert out["platform_count"] == 3
        assert set(out["platforms_trending"]) == {"google_trends", "youtube", "reddit"}
        assert out["cross_platform_score"] > 0

    async def test_none_trending_low(self):
        alert = tv.TrendAlert("kw", 5, 5, 1.0, "steady")
        with patch.object(tv, "check_keyword_velocity", AsyncMock(return_value=alert)), \
             patch.object(tv, "_check_youtube_signal", AsyncMock(return_value={"is_trending": False})), \
             patch.object(tv, "_check_reddit_signal", AsyncMock(return_value={"is_trending": False})):
            out = await tv.cross_platform_correlation("kw")
        assert out["confidence"] == "low"
        assert out["platform_count"] == 0
        assert out["cross_platform_score"] == 0

    async def test_two_trending_high(self):
        alert = tv.TrendAlert("kw", 80, 20, 2.0, "rising")
        with patch.object(tv, "check_keyword_velocity", AsyncMock(return_value=alert)), \
             patch.object(tv, "_check_youtube_signal", AsyncMock(return_value={"is_trending": True, "recency_score": 1.0})), \
             patch.object(tv, "_check_reddit_signal", AsyncMock(return_value={"is_trending": False})):
            out = await tv.cross_platform_correlation("kw")
        assert out["confidence"] == "high"
        assert out["platform_count"] == 2


class TestYouTubeSignal:
    async def test_no_api_key(self, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "", raising=False)
        out = await tv._check_youtube_signal("kw")
        assert out == {"is_trending": False, "reason": "no_api_key"}

    async def test_trending_when_many_recent(self, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "K", raising=False)
        resp = MagicMock(status_code=200)
        resp.json = MagicMock(return_value={
            "pageInfo": {"totalResults": 120},
            "items": [{"snippet": {"title": f"t{i}"}} for i in range(5)],
        })
        with patch("httpx.get", return_value=resp):
            out = await tv._check_youtube_signal("kw")
        assert out["is_trending"] is True
        assert out["recent_videos_count"] == 120
        assert out["sample_titles"] == ["t0", "t1", "t2"]

    async def test_not_trending_few(self, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "K", raising=False)
        resp = MagicMock(status_code=200)
        resp.json = MagicMock(return_value={"pageInfo": {"totalResults": 3}, "items": []})
        with patch("httpx.get", return_value=resp):
            out = await tv._check_youtube_signal("kw")
        assert out["is_trending"] is False

    async def test_api_error(self, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "K", raising=False)
        with patch("httpx.get", return_value=MagicMock(status_code=500)):
            out = await tv._check_youtube_signal("kw")
        assert out == {"is_trending": False, "reason": "api_error"}


class TestRedditSignal:
    async def test_trending_high_score(self):
        posts = {"data": {"children": [
            {"data": {"score": 300, "num_comments": 20}},
            {"data": {"score": 100, "num_comments": 5}},
        ]}}
        resp = MagicMock(status_code=200)
        resp.json = MagicMock(return_value=posts)
        with patch("httpx.get", return_value=resp):
            out = await tv._check_reddit_signal("kw")
        assert out["is_trending"] is True   # avg 200 > 100
        assert out["post_count"] == 2
        assert out["total_comments"] == 25

    async def test_no_posts(self):
        resp = MagicMock(status_code=200)
        resp.json = MagicMock(return_value={"data": {"children": []}})
        with patch("httpx.get", return_value=resp):
            out = await tv._check_reddit_signal("kw")
        assert out == {"is_trending": False, "post_count": 0, "engagement_score": 0}

    async def test_api_error(self):
        with patch("httpx.get", return_value=MagicMock(status_code=429)):
            out = await tv._check_reddit_signal("kw")
        assert out == {"is_trending": False, "reason": "api_error"}

    async def test_exception(self):
        with patch("httpx.get", side_effect=RuntimeError("net")):
            out = await tv._check_reddit_signal("kw")
        assert out["is_trending"] is False


class _FakeSession:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, query):
        result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=self._events)
        result.scalars = MagicMock(return_value=scalars)
        return result


class TestCheckUserKeywordsVelocity:
    async def test_no_niches_returns_empty(self):
        events = [SimpleNamespace(data_json=json.dumps({"other": "x"}))]
        with patch.object(tv, "AsyncSessionLocal", lambda: _FakeSession(events)):
            out = await tv.check_user_keywords_velocity("local")
        assert out == []

    async def test_spike_sends_ws_alert(self):
        events = [SimpleNamespace(data_json=json.dumps({"niche": "finance"}))]
        alert = tv.TrendAlert("finance", 80, 20, 4.0, "spike")
        send = AsyncMock()
        with patch.object(tv, "AsyncSessionLocal", lambda: _FakeSession(events)), \
             patch.object(tv, "check_keyword_velocity", AsyncMock(return_value=alert)), \
             patch.object(tv.asyncio, "sleep", AsyncMock()), \
             patch("backend.core.ws_manager.ws_manager.send", send):
            out = await tv.check_user_keywords_velocity("local")
        assert len(out) == 1 and out[0].keyword == "finance"
        assert send.await_count == 1
        payload = send.await_args.args[0]
        assert payload["type"] == "trend_alert" and payload["alert_level"] == "spike"

    async def test_steady_niche_no_alert(self):
        events = [SimpleNamespace(data_json=json.dumps({"niche": "finance"}))]
        alert = tv.TrendAlert("finance", 10, 10, 1.0, "steady")
        send = AsyncMock()
        with patch.object(tv, "AsyncSessionLocal", lambda: _FakeSession(events)), \
             patch.object(tv, "check_keyword_velocity", AsyncMock(return_value=alert)), \
             patch.object(tv.asyncio, "sleep", AsyncMock()), \
             patch("backend.core.ws_manager.ws_manager.send", send):
            out = await tv.check_user_keywords_velocity("local")
        assert out == []
        assert send.await_count == 0
