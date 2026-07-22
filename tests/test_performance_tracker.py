# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend.services.performance_tracker.

Covers post-upload metrics tracking: the poll-cadence gate
(_should_poll_now), metric persistence + history shaping
(_save_metrics / get_video_performance), aggregate summaries
(get_performance_summary), the optimal-posting-time recommender
(recommend_posting_time), the on-demand poll loop, and the
YouTube/TikTok fetch helpers.

All DB-backed rows live under an isolated user_id and are cleaned up.
Network calls (httpx) and the two fetch helpers are mocked — nothing
leaves the process.
"""
import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

from backend.services import performance_tracker as pt

_UID = "test-perf-tracker"


async def _cleanup():
    from backend.database import AsyncSessionLocal
    from backend.models.generated_video import GeneratedVideo
    from backend.models.video_metrics import VideoMetrics
    from sqlalchemy import delete, select

    async with AsyncSessionLocal() as db:
        # Delete metrics tied to this user's videos first (no user_id on metrics).
        vids = (await db.execute(
            select(GeneratedVideo.id).where(GeneratedVideo.user_id == _UID)
        )).scalars().all()
        if vids:
            await db.execute(
                delete(VideoMetrics).where(VideoMetrics.generated_video_id.in_(vids))
            )
        await db.execute(delete(GeneratedVideo).where(GeneratedVideo.user_id == _UID))
        await db.commit()


def _run(coro_factory):
    async def go():
        from backend.database import init_db
        await init_db()
        await _cleanup()
        try:
            await coro_factory()
        finally:
            await _cleanup()
    asyncio.run(go())


async def _add_uploaded_video(db, **overrides):
    from backend.models.generated_video import GeneratedVideo
    kwargs = dict(
        user_id=_UID,
        status="uploaded",
        title="A video",
        uploaded_platforms_json=json.dumps(["youtube"]),
        created_at=datetime.utcnow(),
    )
    kwargs.update(overrides)
    gv = GeneratedVideo(**kwargs)
    db.add(gv)
    await db.flush()
    return gv


# ── _should_poll_now (pure cadence logic) ─────────────────────────────────────

def test_should_poll_now_cadence_branches():
    from backend.models.generated_video import GeneratedVideo
    now = datetime.utcnow()

    # No updated_at → always poll.
    v = GeneratedVideo(created_at=now, updated_at=None)
    assert pt._should_poll_now(v) is True

    # < 24h old: needs >= 2h since last update.
    v = GeneratedVideo(created_at=now - timedelta(hours=1),
                       updated_at=now - timedelta(hours=1))
    assert pt._should_poll_now(v) is False
    v.updated_at = now - timedelta(hours=3)
    assert pt._should_poll_now(v) is True

    # 2-7 days old: needs >= 6h.
    v = GeneratedVideo(created_at=now - timedelta(days=3),
                       updated_at=now - timedelta(hours=3))
    assert pt._should_poll_now(v) is False
    v.updated_at = now - timedelta(hours=7)
    assert pt._should_poll_now(v) is True

    # 7-30 days old: needs >= 1 day.
    v = GeneratedVideo(created_at=now - timedelta(days=10),
                       updated_at=now - timedelta(hours=12))
    assert pt._should_poll_now(v) is False
    v.updated_at = now - timedelta(days=2)
    assert pt._should_poll_now(v) is True

    # > 30 days: never poll again.
    v = GeneratedVideo(created_at=now - timedelta(days=40),
                       updated_at=now - timedelta(days=40))
    assert pt._should_poll_now(v) is False


# ── _save_metrics + get_video_performance ─────────────────────────────────────

def test_save_metrics_and_video_performance_history():
    async def body():
        from backend.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            gv = await _add_uploaded_video(db)
            vid = gv.id
            await db.commit()

        await pt._save_metrics(vid, "youtube", {"views": 100, "likes": 10, "comments": 2, "shares": 0})
        await pt._save_metrics(vid, "youtube", {"views": 250, "likes": 25, "comments": 4, "shares": 0})
        await pt._save_metrics(vid, "tiktok", {"views": 500, "likes": 60})

        perf = await pt.get_video_performance(vid)
        assert perf["generated_video_id"] == vid
        assert set(perf["history"].keys()) == {"youtube", "tiktok"}
        assert len(perf["history"]["youtube"]) == 2
        # "latest" reflects the newest snapshot per platform.
        assert perf["latest"]["youtube"]["views"] == 250
        assert perf["latest"]["tiktok"]["views"] == 500

    _run(body)


# ── get_performance_summary ───────────────────────────────────────────────────

def test_performance_summary_empty():
    async def body():
        summary = await pt.get_performance_summary(_UID)
        assert summary == {"total_views": 0, "total_likes": 0,
                           "total_videos": 0, "best_video": None}

    _run(body)


def test_performance_summary_totals_and_best_video():
    async def body():
        from backend.database import AsyncSessionLocal
        from backend.models.video_metrics import VideoMetrics

        async with AsyncSessionLocal() as db:
            low = await _add_uploaded_video(db, title="low")
            high = await _add_uploaded_video(db, title="high")
            db.add(VideoMetrics(generated_video_id=low.id, platform="youtube",
                                views=100, likes=10, fetched_at=datetime.utcnow()))
            db.add(VideoMetrics(generated_video_id=high.id, platform="youtube",
                                views=900, likes=90, fetched_at=datetime.utcnow()))
            high_id = high.id
            await db.commit()

        summary = await pt.get_performance_summary(_UID)
        assert summary["total_videos"] == 2
        assert summary["total_views"] == 1000
        assert summary["total_likes"] == 100
        assert summary["best_video"]["id"] == high_id
        assert summary["best_video"]["views"] == 900

    _run(body)


# ── recommend_posting_time ────────────────────────────────────────────────────

def test_recommend_posting_time_no_videos():
    async def body():
        rec = await pt.recommend_posting_time(_UID, platform="youtube")
        assert rec["recommendation"] is None
        assert rec["sample_size"] == 0
        assert "No uploaded videos" in rec["message"]

    _run(body)


def test_recommend_posting_time_too_few_platform_videos():
    async def body():
        from backend.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            # Two videos, only one on youtube → below the 3-video threshold.
            await _add_uploaded_video(db, uploaded_platforms_json=json.dumps(["youtube"]))
            await _add_uploaded_video(db, uploaded_platforms_json=json.dumps(["tiktok"]))
            await db.commit()

        rec = await pt.recommend_posting_time(_UID, platform="youtube")
        assert rec["recommendation"] is None
        assert rec["sample_size"] == 1
        assert "at least 3" in rec["message"]

    _run(body)


def test_recommend_posting_time_metrics_but_no_early_velocity():
    async def body():
        from backend.database import AsyncSessionLocal
        from backend.models.video_metrics import VideoMetrics

        # 3 youtube videos, but every first-metric snapshot has 0 views, so
        # they're all skipped as having no early velocity. Exercises the
        # metric-gathering loop, then the "not enough performance data" return
        # (without reaching the buggy day_breakdown builder — see note below).
        async with AsyncSessionLocal() as db:
            for i in range(3):
                gv = await _add_uploaded_video(
                    db, uploaded_platforms_json=json.dumps(["youtube"]))
                db.add(VideoMetrics(generated_video_id=gv.id, platform="youtube",
                                    views=0, fetched_at=datetime.utcnow()))
            await db.commit()

        rec = await pt.recommend_posting_time(_UID, platform="youtube")
        assert rec["recommendation"] is None
        assert rec["sample_size"] == 3
        assert "Not enough performance data" in rec["message"]

    _run(body)


# NOTE: a test for the happy "produces a recommendation" path of
# recommend_posting_time was intentionally DROPPED. It exposes a genuine OSS
# bug (not an API mismatch): backend/services/performance_tracker.py line ~419
# builds `day_breakdown` as `{d: round(avg, 1) for d, avg in
# day_performance.items()}`, but day_performance's values are *lists* of view
# counts, so `round([...], 1)` raises TypeError. Because any successful
# recommendation path populates day_performance, the entire happy path crashes.
# The fix belongs in source (use the already-computed `day_avgs`), which the
# test-only mandate forbids touching — so the path is left uncovered here.


# ── poll_all_uploaded_videos ──────────────────────────────────────────────────

def test_poll_all_uploaded_videos_fetches_and_saves():
    async def body():
        from backend.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            gv = await _add_uploaded_video(
                db, youtube_video_id="yt-abc",
                uploaded_platforms_json=json.dumps(["youtube"]),
                created_at=datetime.utcnow(), updated_at=None)
            vid = gv.id
            await db.commit()

        fake_metrics = {"views": 4242, "likes": 42, "comments": 4, "shares": 0}
        # _save_metrics is mocked so the poll loop (which scans ALL uploaded
        # videos, not just this user's) never writes rows for real videos.
        save_mock = AsyncMock()
        with patch.object(pt, "fetch_youtube_metrics",
                          new=AsyncMock(return_value=fake_metrics)), \
             patch.object(pt, "fetch_tiktok_metrics",
                          new=AsyncMock(return_value=None)), \
             patch.object(pt, "_save_metrics", new=save_mock):
            await pt.poll_all_uploaded_videos()

        # Our seeded video was fetched + persisted with the fetched metrics.
        calls = [c.args for c in save_mock.call_args_list]
        assert (vid, "youtube", fake_metrics) in calls

    _run(body)


# ── fetch helpers ─────────────────────────────────────────────────────────────

def test_fetch_youtube_metrics_no_key_returns_none():
    async def body():
        from backend.config import settings
        with patch.object(settings, "YOUTUBE_API_KEY", ""):
            assert await pt.fetch_youtube_metrics("abc") is None

    _run(body)


def test_fetch_youtube_metrics_parses_api_response():
    async def body():
        from backend.config import settings

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "items": [{"statistics": {
                "viewCount": "1500", "likeCount": "120", "commentCount": "8"}}]
        }
        with patch.object(settings, "YOUTUBE_API_KEY", "fake-key"), \
             patch("httpx.get", return_value=fake_resp):
            metrics = await pt.fetch_youtube_metrics("abc")

        assert metrics == {"views": 1500, "likes": 120, "comments": 8, "shares": 0}

    _run(body)


def test_fetch_tiktok_metrics_no_token_returns_none():
    async def body():
        # Mock the settings lookup to a row with no token → None, no network.
        # (Uses a mock session so we never mutate the real "local" settings row.)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        session = MagicMock()
        session.execute = AsyncMock(return_value=result)

        class FakeCM:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *a):
                return False

        with patch.object(pt, "AsyncSessionLocal", return_value=FakeCM()):
            assert await pt.fetch_tiktok_metrics("pub-123") is None

    _run(body)
