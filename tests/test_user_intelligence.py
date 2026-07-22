# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend.core.user_intelligence.UserIntelligence.

Covers the personalization engine: raw event recording + the profile
update counter, the lightweight context summary aggregates (top
niches/platforms, funnel counts), rule-based + AI smart suggestions,
AI profile synthesis, performance insights, news memory, recent
failures, and the pure next-best-action deriver.

All DB-backed. Each test seeds rows under an isolated user_id and
cleans up afterwards so it never touches real data. AI + network are
mocked — nothing leaves the process.
"""
import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

from backend.core.user_intelligence import UserIntelligence, PROFILE_UPDATE_THRESHOLD

_UID = "test-user-intel"


async def _cleanup():
    from backend.database import AsyncSessionLocal
    from backend.models.user_behavior import UserBehavior
    from backend.models.user_profile import UserProfile
    from backend.models.generated_video import GeneratedVideo
    from backend.models.downloaded_video import DownloadedVideo
    from backend.models.video_metrics import VideoMetrics
    from backend.models.scout_result import ScoutResult
    from backend.models.job import Job
    from sqlalchemy import delete

    async with AsyncSessionLocal() as db:
        await db.execute(delete(UserBehavior).where(UserBehavior.user_id == _UID))
        await db.execute(delete(UserProfile).where(UserProfile.user_id == _UID))
        await db.execute(delete(GeneratedVideo).where(GeneratedVideo.user_id == _UID))
        await db.execute(delete(DownloadedVideo).where(DownloadedVideo.user_id == _UID))
        await db.execute(delete(ScoutResult).where(ScoutResult.user_id == _UID))
        await db.execute(delete(Job).where(Job.user_id == _UID))
        # VideoMetrics has no user_id — cleaned via generated_video_id where relevant.
        await db.commit()


async def _seed_events(events):
    """events: list of (event_type, data_dict, created_at_offset_seconds)."""
    from backend.database import AsyncSessionLocal
    from backend.models.user_behavior import UserBehavior

    base = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        for i, (etype, data, offset) in enumerate(events):
            db.add(UserBehavior(
                user_id=_UID,
                event_type=etype,
                data_json=json.dumps(data),
                created_at=base - timedelta(seconds=offset),
            ))
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


# ── record_event + profile counter ───────────────────────────────────────────

def test_record_event_writes_row_and_seeds_profile_counter():
    async def body():
        from backend.database import AsyncSessionLocal
        from backend.models.user_behavior import UserBehavior
        from backend.models.user_profile import UserProfile
        from sqlalchemy import select

        ui = UserIntelligence()
        await ui.record_event("niche_searched", {"niche": "ai tools"}, user_id=_UID)

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(UserBehavior).where(UserBehavior.user_id == _UID)
            )).scalars().all()
            assert len(rows) == 1
            assert rows[0].event_type == "niche_searched"
            assert json.loads(rows[0].data_json)["niche"] == "ai tools"

            prof = (await db.execute(
                select(UserProfile).where(UserProfile.user_id == _UID)
            )).scalar_one()
            assert prof.events_since_last_update == 1

        # A second event increments the counter on the existing profile.
        await ui.record_event("video_generated", {}, user_id=_UID)
        async with AsyncSessionLocal() as db:
            prof = (await db.execute(
                select(UserProfile).where(UserProfile.user_id == _UID)
            )).scalar_one()
            assert prof.events_since_last_update == 2

    _run(body)


def test_should_update_profile_threshold():
    async def body():
        from backend.database import AsyncSessionLocal
        from backend.models.user_profile import UserProfile

        ui = UserIntelligence()
        # No profile at all → False.
        assert await ui.should_update_profile(_UID) is False

        async with AsyncSessionLocal() as db:
            db.add(UserProfile(user_id=_UID,
                               events_since_last_update=PROFILE_UPDATE_THRESHOLD - 1))
            await db.commit()
        assert await ui.should_update_profile(_UID) is False

        async with AsyncSessionLocal() as db:
            from sqlalchemy import select
            prof = (await db.execute(
                select(UserProfile).where(UserProfile.user_id == _UID)
            )).scalar_one()
            prof.events_since_last_update = PROFILE_UPDATE_THRESHOLD
            await db.commit()
        assert await ui.should_update_profile(_UID) is True

    _run(body)


# ── get_context_summary ───────────────────────────────────────────────────────

def test_context_summary_empty_is_first_session():
    async def body():
        ctx = await UserIntelligence().get_context_summary(_UID)
        assert ctx["is_first_session"] is True
        assert ctx["total_scouts"] == 0
        assert ctx["top_niches"] == []
        assert ctx["top_platforms"] == []
        assert ctx["last_niche"] is None

    _run(body)


def test_context_summary_aggregates_niches_platforms_and_funnel():
    async def body():
        # More recent events have smaller offsets. "ai tools" is most recent
        # and most frequent; youtube is the most frequent platform.
        await _seed_events([
            ("niche_searched", {"niche": "ai tools", "platforms": ["youtube", "tiktok"]}, 1),
            ("niche_searched", {"niche": "ai tools", "platforms": ["youtube"]}, 10),
            ("niche_searched", {"niche": "cooking", "platforms": ["youtube"]}, 20),
            ("video_downloaded", {}, 30),
            ("video_downloaded", {}, 31),
            ("video_downloaded", {}, 32),
            ("video_generated", {}, 40),
            ("video_uploaded", {}, 50),
        ])
        ctx = await UserIntelligence().get_context_summary(_UID)

        assert ctx["is_first_session"] is False
        assert ctx["total_scouts"] == 3
        assert ctx["total_generated"] == 1
        assert ctx["total_uploads"] == 1
        assert ctx["last_niche"] == "ai tools"          # most recent niche_searched
        assert ctx["top_niches"][0] == "ai tools"       # highest frequency first
        assert "cooking" in ctx["top_niches"]
        assert ctx["top_platforms"][0] == "youtube"     # 3 occurrences vs 1
        # 3 downloaded − 1 generated = 2 in the "downloaded but not made" bucket.
        assert ctx["downloaded_not_generated"] == 2
        # 1 generated − 1 uploaded = 0 ready-but-unshipped.
        assert ctx["generated_not_uploaded"] == 0

    _run(body)


# ── get_user_profile ──────────────────────────────────────────────────────────

def test_get_user_profile_variants():
    async def body():
        from backend.database import AsyncSessionLocal
        from backend.models.user_profile import UserProfile

        ui = UserIntelligence()
        # No row → None.
        assert await ui.get_user_profile(_UID) is None

        async with AsyncSessionLocal() as db:
            db.add(UserProfile(user_id=_UID,
                               profile_json=json.dumps({"niches": ["finance"]})))
            await db.commit()
        prof = await ui.get_user_profile(_UID)
        assert prof == {"niches": ["finance"]}

        # Corrupt JSON → None, no raise.
        async with AsyncSessionLocal() as db:
            from sqlalchemy import select
            row = (await db.execute(
                select(UserProfile).where(UserProfile.user_id == _UID)
            )).scalar_one()
            row.profile_json = "{not valid json"
            await db.commit()
        assert await ui.get_user_profile(_UID) is None

    _run(body)


# ── _next_best_action (pure) ──────────────────────────────────────────────────

def test_next_best_action_priority_ladder():
    nba = UserIntelligence._next_best_action

    # Highest priority: downloaded but no short made.
    s = {"downloaded_not_generated": 2, "downloaded_not_analyzed": 1}
    assert "downloaded" in nba(s).lower()

    # Next: analyzed nudge.
    assert "analyz" in nba({"downloaded_not_analyzed": 1}).lower()

    # Scouted but downloaded none.
    s = {"last_scout": {"niche": "x", "results": 5}, "downloaded_total": 0}
    assert "scouted" in nba(s).lower()

    # Finished but not uploaded → publish nudge.
    assert "upload" in nba({"generated_not_uploaded": 3}).lower()

    # Brand-new user.
    s = {"generated_total": 0, "downloaded_total": 0}
    out = nba(s).lower()
    assert "brand-new" in out or "first" in out

    # Nothing actionable → None.
    assert nba({"downloaded_total": 5, "generated_total": 5}) is None


# ── get_smart_suggestions ─────────────────────────────────────────────────────

def test_smart_suggestions_first_session_empty():
    async def body():
        assert await UserIntelligence().get_smart_suggestions(_UID) == []

    _run(body)


def test_smart_suggestions_rule_based_when_no_profile():
    async def body():
        # Activity present, but NO profile → AI path skipped, rule-based used.
        await _seed_events([
            ("video_downloaded", {}, 10),
            ("video_downloaded", {}, 11),
        ])
        sugg = await UserIntelligence().get_smart_suggestions(_UID)
        assert isinstance(sugg, list)
        assert 1 <= len(sugg) <= 3
        # downloaded_not_generated = 2 → a "Generate ..." suggestion.
        assert any("generate" in s.lower() for s in sugg)

    _run(body)


def test_smart_suggestions_ai_path_uses_and_caches_ai_output():
    async def body():
        from backend.database import AsyncSessionLocal
        from backend.models.user_profile import UserProfile
        from sqlalchemy import select

        # Activity signal >= 3 (3 scouts) + a profile present → AI path.
        await _seed_events([
            ("niche_searched", {"niche": "ai tools"}, 5),
            ("niche_searched", {"niche": "ai tools"}, 6),
            ("niche_searched", {"niche": "ai tools"}, 7),
        ])
        async with AsyncSessionLocal() as db:
            db.add(UserProfile(user_id=_UID,
                               profile_json=json.dumps({"niches": ["ai tools"]})))
            await db.commit()

        fake_ai = MagicMock()
        fake_ai.chat = AsyncMock(return_value='["do A", "do B", "do C"]')
        with patch("backend.core.ai_provider.get_ai_client", return_value=fake_ai):
            sugg = await UserIntelligence().get_smart_suggestions(_UID)

        assert sugg == ["do A", "do B", "do C"]
        fake_ai.chat.assert_awaited()

        # Result cached on the profile row with a fresh timestamp.
        async with AsyncSessionLocal() as db:
            prof = (await db.execute(
                select(UserProfile).where(UserProfile.user_id == _UID)
            )).scalar_one()
            assert json.loads(prof.suggestions_json) == ["do A", "do B", "do C"]
            assert prof.suggestions_updated_at is not None

    _run(body)


# ── update_profile_with_ai ────────────────────────────────────────────────────

def test_update_profile_with_ai_saves_and_resets_counter():
    async def body():
        from backend.database import AsyncSessionLocal
        from backend.models.user_profile import UserProfile
        from sqlalchemy import select

        ui = UserIntelligence()
        # record_event seeds a profile with events_since_last_update = 3.
        await ui.record_event("niche_searched", {"niche": "finance"}, _UID)
        await ui.record_event("video_downloaded", {}, _UID)
        await ui.record_event("video_generated", {}, _UID)

        profile_obj = {"niches": ["finance"], "primary_language": "en"}
        fake_ai = MagicMock()
        fake_ai.chat = AsyncMock(return_value=json.dumps(profile_obj))
        with patch("backend.core.ai_provider.get_ai_client", return_value=fake_ai):
            await ui.update_profile_with_ai(_UID)

        assert await ui.get_user_profile(_UID) == profile_obj
        async with AsyncSessionLocal() as db:
            prof = (await db.execute(
                select(UserProfile).where(UserProfile.user_id == _UID)
            )).scalar_one()
            assert prof.events_since_last_update == 0
            assert prof.last_profile_update is not None

    _run(body)


def test_update_profile_with_ai_strips_code_fences():
    async def body():
        ui = UserIntelligence()
        await ui.record_event("niche_searched", {"niche": "x"}, _UID)

        fenced = '```json\n{"niches": ["x"]}\n```'
        fake_ai = MagicMock()
        fake_ai.chat = AsyncMock(return_value=fenced)
        with patch("backend.core.ai_provider.get_ai_client", return_value=fake_ai):
            await ui.update_profile_with_ai(_UID)

        assert await ui.get_user_profile(_UID) == {"niches": ["x"]}

    _run(body)


def test_update_profile_with_ai_no_events_skips_ai():
    async def body():
        ui = UserIntelligence()
        fake_ai = MagicMock()
        fake_ai.chat = AsyncMock(return_value="{}")
        with patch("backend.core.ai_provider.get_ai_client", return_value=fake_ai):
            await ui.update_profile_with_ai(_UID)   # no events → early return
        fake_ai.chat.assert_not_awaited()
        assert await ui.get_user_profile(_UID) is None

    _run(body)


def test_update_profile_with_ai_bad_json_is_swallowed():
    async def body():
        ui = UserIntelligence()
        await ui.record_event("niche_searched", {"niche": "x"}, _UID)

        fake_ai = MagicMock()
        fake_ai.chat = AsyncMock(return_value="totally not json")
        with patch("backend.core.ai_provider.get_ai_client", return_value=fake_ai):
            await ui.update_profile_with_ai(_UID)   # must not raise
        # Garbage was never persisted.
        assert await ui.get_user_profile(_UID) is None

    _run(body)


# ── _build_event_summary_for_profile ──────────────────────────────────────────

def test_build_event_summary_empty_and_populated():
    async def body():
        ui = UserIntelligence()
        assert await ui._build_event_summary_for_profile(_UID) == ""

        await _seed_events([
            ("niche_searched", {"niche": "ai"}, 10),
            ("niche_searched", {"niche": "ai"}, 11),
            ("video_generated", {"tier": "free"}, 12),
        ])
        summary = await ui._build_event_summary_for_profile(_UID)
        assert "Total events analyzed: 3" in summary
        assert "niche_searched" in summary
        assert "video_generated" in summary

    _run(body)


# ── get_performance_insights ──────────────────────────────────────────────────

def test_performance_insights_none_when_too_few_videos():
    async def body():
        # Fewer than 3 uploaded videos → None.
        from backend.database import AsyncSessionLocal
        from backend.models.generated_video import GeneratedVideo
        async with AsyncSessionLocal() as db:
            db.add(GeneratedVideo(user_id=_UID, status="uploaded", niche="a"))
            await db.commit()
        assert await UserIntelligence().get_performance_insights(_UID) is None

    _run(body)


def test_performance_insights_aggregates_when_enough_data():
    async def body():
        from backend.database import AsyncSessionLocal
        from backend.models.generated_video import GeneratedVideo
        from backend.models.video_metrics import VideoMetrics

        async with AsyncSessionLocal() as db:
            for i in range(3):
                gv = GeneratedVideo(user_id=_UID, status="uploaded",
                                    niche="finance" if i < 2 else "cooking",
                                    title=f"vid {i}", gen_tier="standard",
                                    aspect_ratio="9:16")
                db.add(gv)
                await db.flush()
                db.add(VideoMetrics(
                    generated_video_id=gv.id, platform="youtube",
                    views=1000 * (i + 1), likes=100, comments=10,
                    fetched_at=datetime.utcnow(),
                ))
            await db.commit()

        insights = await UserIntelligence().get_performance_insights(_UID)
        assert insights is not None
        assert insights["total_videos_analyzed"] == 3
        assert "finance" in insights["best_niches"]
        assert insights["avg_views"] > 0
        assert len(insights["top_videos"]) <= 5
        assert "finance" in insights["engagement_rate_by_niche"]

    _run(body)


# ── get_news_context ──────────────────────────────────────────────────────────

def test_news_context_empty():
    async def body():
        ctx = await UserIntelligence().get_news_context(_UID)
        assert ctx == {"total_news_scouts": 0}

    _run(body)


def test_news_context_aggregates_queries_sources_and_saved():
    async def body():
        await _seed_events([
            ("news_scouted", {"query": "ai regulation", "sources": ["reuters", "bbc"]}, 5),
            ("news_scouted", {"query": "ai regulation", "sources": ["reuters"]}, 6),
            ("news_saved", {"query": "ai regulation", "count": 3}, 7),
            ("news_generated", {}, 8),
            ("news_dismissed", {"topic": "crypto"}, 9),
        ])
        ctx = await UserIntelligence().get_news_context(_UID)
        assert ctx["total_news_scouts"] == 2
        assert ctx["top_news_niches"][0] == "ai regulation"
        assert ctx["last_news_query"] == "ai regulation"
        assert ctx["preferred_sources"][0] == "reuters"    # most frequent source
        assert ctx["total_articles_saved"] == 3
        assert ctx["total_news_videos_generated"] == 1
        assert "crypto" in ctx["dismissed_topics"]
        assert ctx["saved_not_generated"] == 2             # 3 saved − 1 generated

    _run(body)


# ── get_recent_failures ───────────────────────────────────────────────────────

def test_recent_failures_empty_and_populated():
    async def body():
        from backend.database import AsyncSessionLocal
        from backend.models.job import Job

        ui = UserIntelligence()
        assert await ui.get_recent_failures(_UID) == []

        async with AsyncSessionLocal() as db:
            db.add(Job(user_id=_UID, job_type="generate", status="failed",
                       title="My Video", error_message="ffmpeg exploded",
                       created_at=datetime.utcnow()))
            # An old failure (>24h) must be excluded.
            db.add(Job(user_id=_UID, job_type="scout", status="failed",
                       title="Old", error_message="stale",
                       created_at=datetime.utcnow() - timedelta(hours=30)))
            # A success must be excluded.
            db.add(Job(user_id=_UID, job_type="download", status="success",
                       title="ok", created_at=datetime.utcnow()))
            await db.commit()

        failures = await ui.get_recent_failures(_UID)
        assert len(failures) == 1
        assert "ffmpeg exploded" in failures[0]
        assert "My Video" in failures[0]

    _run(body)


# ── get_credential_status ─────────────────────────────────────────────────────

def test_credential_status_shape_without_settings():
    async def body():
        status = await UserIntelligence().get_credential_status(None, _UID)
        # Voice generation always works (Edge TTS needs no key).
        assert status["voice_generation"]["configured"] is True
        # All expected capability keys are present.
        for key in ("ai_provider", "youtube_scout", "tiktok_scout",
                    "douyin_scout", "youtube_upload", "tiktok_upload",
                    "stock_footage"):
            assert key in status
            assert "configured" in status[key]

    _run(body)
