# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Tracks user behavior to personalize the Planner Agent over time.
Two layers:
  1. Raw events in user_behavior table (every action)
  2. AI-generated UserProfile (distilled summary, updated every ~20 interactions)
"""
import json
import logging
from datetime import datetime, timedelta
from sqlalchemy import select, func
from backend.database import AsyncSessionLocal
from backend.models.user_behavior import UserBehavior
from backend.models.user_settings import UserSettings
from backend.models.user_profile import UserProfile

logger = logging.getLogger(__name__)

# How many events before triggering an AI profile update
PROFILE_UPDATE_THRESHOLD = 20


PROFILE_GENERATION_PROMPT = """Analyze the following user activity data from ViralMint (a video content strategy app) and generate a structured JSON profile that captures who this user is and how they work.

## Raw Event Summary
{event_summary}

## Previous Profile (if any)
{previous_profile}

## Instructions
Generate a JSON object with these fields. Be specific and observational — this profile will be used to personalize the AI assistant's responses. If you don't have enough data for a field, use null.

{{
  "niches": ["list of content niches they focus on, ordered by frequency"],
  "primary_language": "the language they write in most (2-letter code)",
  "content_languages": ["all languages they create content in"],
  "preferred_platforms": ["platforms they scout/upload to most, ordered"],
  "preferred_video_style": "short description of their preferred video format/style, or null",
  "preferred_voice_tone": "voice/tone they choose for generated videos, or null",
  "active_hours": "when they're most active (e.g. 'evenings UTC+8'), or null",
  "generation_preferences": {{
    "aspect_ratio": "most used aspect ratio or null",
    "caption_style": "most used caption style or null",
    "tts_provider": "most used TTS provider or null",
    "music_genre": "most used music genre or null"
  }},
  "behavior_patterns": [
    "3-5 short observations about how they use the app",
    "e.g. 'Downloads top 3 results per scout, rarely downloads more'",
    "e.g. 'Always generates videos same day as analysis'",
    "e.g. 'Prefers YouTube over TikTok for uploads'"
  ],
  "content_strategy": "1-2 sentence summary of their content creation approach, or null",
  "niche_crossover_opportunities": [
    "1-3 creative content ideas that combine two or more of the user's niches",
    "e.g. 'Personal finance + morning routines = Morning Money Habits'",
    "e.g. 'Tech reviews + cooking = Smart Kitchen Gadget Reviews'",
    "Only suggest if user has 2+ distinct niches, otherwise use null"
  ],
  "ai_interaction_style": "how they prefer to interact with the AI — terse/detailed, commanding/collaborative, or null",
  "ai_notes": "2-3 sentences of free-form observations that would help personalize future interactions. What makes this user unique?"
}}

Return ONLY the JSON object, no markdown fences or explanation."""


class UserIntelligence:
    async def record_event(self, event_type: str, data: dict, user_id: str = "local"):
        """Record a behavior event and increment the profile update counter."""
        async with AsyncSessionLocal() as db:
            event = UserBehavior(
                user_id=user_id,
                event_type=event_type,
                data_json=json.dumps(data),
            )
            db.add(event)

            # Increment profile event counter
            result = await db.execute(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )
            profile = result.scalar_one_or_none()
            if profile:
                profile.events_since_last_update = (profile.events_since_last_update or 0) + 1
            else:
                profile = UserProfile(
                    user_id=user_id,
                    events_since_last_update=1,
                )
                db.add(profile)

            await db.commit()

    async def should_update_profile(self, user_id: str = "local") -> bool:
        """Check if enough events have accumulated to warrant a profile update."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )
            profile = result.scalar_one_or_none()
            if not profile:
                return False
            return (profile.events_since_last_update or 0) >= PROFILE_UPDATE_THRESHOLD

    async def update_profile_with_ai(self, user_id: str = "local"):
        """
        Use AI to generate/update the user profile from accumulated events.
        Called periodically (every ~20 interactions) from the chat flow.
        """
        from backend.core.ai_provider import get_ai_client

        # Build event summary
        event_summary = await self._build_event_summary_for_profile(user_id)
        if not event_summary:
            return

        # Load previous profile
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )
            profile = result.scalar_one_or_none()

        previous = profile.profile_json if profile else "None — this is the first profile generation."

        prompt = PROFILE_GENERATION_PROMPT.format(
            event_summary=event_summary,
            previous_profile=previous,
        )

        try:
            ai = get_ai_client()
            response = await ai.chat(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
            )

            # Parse and validate JSON
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            profile_data = json.loads(cleaned)

            # Save updated profile
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(UserProfile).where(UserProfile.user_id == user_id)
                )
                existing = result.scalar_one_or_none()
                if existing:
                    existing.profile_json = json.dumps(profile_data)
                    existing.events_since_last_update = 0
                    existing.last_profile_update = datetime.utcnow()
                else:
                    db.add(UserProfile(
                        user_id=user_id,
                        profile_json=json.dumps(profile_data),
                        events_since_last_update=0,
                        last_profile_update=datetime.utcnow(),
                    ))
                await db.commit()

            logger.info(f"User profile updated for {user_id}")

        except Exception as e:
            logger.warning(f"Profile update failed (non-critical): {e}")

    async def get_user_profile(self, user_id: str = "local") -> dict | None:
        """Load the AI-generated user profile."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )
            profile = result.scalar_one_or_none()
            if profile and profile.profile_json:
                try:
                    return json.loads(profile.profile_json)
                except json.JSONDecodeError:
                    return None
        return None

    async def get_context_summary(self, user_id: str = "local") -> dict:
        """Build context summary from raw events (lightweight, every message)."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(UserBehavior)
                .where(UserBehavior.user_id == user_id)
                .order_by(UserBehavior.created_at.desc())
                .limit(500)
            )
            events = result.scalars().all()

            counts = {}
            niches = []
            platforms = []
            last_niche = None

            for e in events:
                counts[e.event_type] = counts.get(e.event_type, 0) + 1
                if e.event_type == "niche_searched":
                    data = json.loads(e.data_json or "{}")
                    if data.get("niche"):
                        niches.append(data["niche"])
                        if not last_niche:
                            last_niche = data["niche"]
                    if data.get("platforms"):
                        platforms.extend(data["platforms"])

            niche_counts = {}
            for n in niches:
                niche_counts[n] = niche_counts.get(n, 0) + 1
            top_niches = sorted(niche_counts, key=niche_counts.get, reverse=True)[:3]

            platform_counts = {}
            for p in platforms:
                platform_counts[p] = platform_counts.get(p, 0) + 1
            top_platforms = sorted(platform_counts, key=platform_counts.get, reverse=True)[:3]

            return {
                "total_scouts":     counts.get("niche_searched", 0),
                "total_generated":  counts.get("video_generated", 0),
                "total_uploads":    counts.get("video_uploaded", 0),
                "top_niches":       top_niches,
                "last_niche":       last_niche,
                "top_platforms":    top_platforms,
                "is_first_session": len(events) == 0,
                "downloaded_not_generated": max(
                    counts.get("video_downloaded", 0) - counts.get("video_generated", 0), 0
                ),
                "generated_not_uploaded": max(
                    counts.get("video_generated", 0) - counts.get("video_uploaded", 0), 0
                ),
            }

    async def get_pipeline_state(self, user_id: str = "local") -> dict:
        """Where the user is in the content funnel RIGHT NOW — read live from the
        content tables (not the event log). This is what lets the planner be
        genuinely proactive ("you downloaded 3 videos but haven't made a short
        from any — want me to?") instead of waiting to be asked. Cheap (a few
        indexed counts); computed every turn. Returns {} on any error so it can
        never break a chat turn.

        OSS funnel is download -> generate -> UPLOAD (the OSS build ships the
        uploader), so the nudges culminate in "upload the finished video".
        """
        from backend.models.downloaded_video import DownloadedVideo
        from backend.models.generated_video import GeneratedVideo
        from backend.models.scout_result import ScoutResult
        from backend.models.job import Job
        try:
            async with AsyncSessionLocal() as db:
                dl_rows = (await db.execute(
                    select(DownloadedVideo.id, DownloadedVideo.insights_json)
                    .where(DownloadedVideo.user_id == user_id)
                )).all()
                downloaded_total = len(dl_rows)
                downloaded_not_analyzed = sum(1 for r in dl_rows if not r.insights_json)
                gen_source_ids = set((await db.execute(
                    select(GeneratedVideo.source_downloaded_video_id)
                    .where(GeneratedVideo.user_id == user_id,
                           GeneratedVideo.source_downloaded_video_id.isnot(None))
                )).scalars().all())
                downloaded_not_generated = sum(1 for r in dl_rows if r.id not in gen_source_ids)

                gen_rows = (await db.execute(
                    select(GeneratedVideo.video_path, GeneratedVideo.uploaded_platforms_json,
                           GeneratedVideo.source_type)
                    .where(GeneratedVideo.user_id == user_id)
                )).all()
                generated_total = len(gen_rows)
                # "Ready to ship" = has a rendered file but hasn't been uploaded yet.
                generated_not_uploaded = sum(
                    1 for r in gen_rows
                    if r.video_path and (not r.uploaded_platforms_json
                                         or r.uploaded_platforms_json.strip() in ("", "[]"))
                )
                clips_total = sum(1 for r in gen_rows if r.source_type == "clip_extraction")

                last_scout = None
                last_job = (await db.execute(
                    select(Job).where(
                        Job.user_id == user_id, Job.job_type == "scout", Job.status == "success"
                    ).order_by(Job.completed_at.desc()).limit(1)
                )).scalar_one_or_none()
                if last_job:
                    n_results = (await db.execute(
                        select(func.count()).select_from(ScoutResult)
                        .where(ScoutResult.job_id == last_job.id)
                    )).scalar() or 0
                    niche = None
                    try:
                        niche = json.loads(last_job.input_json or "{}").get("niche")
                    except (TypeError, ValueError):
                        pass
                    last_scout = {"niche": niche, "results": n_results}

            state = {
                "downloaded_total": downloaded_total,
                "downloaded_not_analyzed": downloaded_not_analyzed,
                "downloaded_not_generated": downloaded_not_generated,
                "generated_total": generated_total,
                "generated_not_uploaded": generated_not_uploaded,
                "clips_total": clips_total,
                "last_scout": last_scout,
            }
            state["next_best_action"] = self._next_best_action(state)
            return state
        except Exception as e:
            logger.debug("get_pipeline_state failed: %s", e)
            return {}

    @staticmethod
    def _next_best_action(s: dict) -> str | None:
        """The single highest-value next step, derived from funnel state. The
        planner surfaces this proactively (one nudge, not a checklist)."""
        if s.get("downloaded_not_generated", 0) > 0:
            return (f"{s['downloaded_not_generated']} downloaded video(s) have no short made "
                    "from them yet — offer to generate one.")
        if s.get("downloaded_not_analyzed", 0) > 0:
            return (f"{s['downloaded_not_analyzed']} downloaded video(s) aren't analyzed yet — "
                    "offer to analyze for hook/structure/angle.")
        ls = s.get("last_scout")
        if ls and ls.get("results", 0) > 0 and s.get("downloaded_total", 0) == 0:
            return (f"Scouted {ls['results']} videos for '{ls.get('niche')}' but downloaded none — "
                    "offer to grab the top performers.")
        if s.get("generated_not_uploaded", 0) > 0:
            return (f"{s['generated_not_uploaded']} finished video(s) haven't been uploaded yet — "
                    "offer to publish to YouTube/TikTok.")
        if s.get("generated_total", 0) == 0 and s.get("downloaded_total", 0) == 0:
            return ("Brand-new — no content yet. Suggest scouting a niche or making a first video "
                    "from a topic.")
        return None

    async def get_smart_suggestions(self, user_id: str = "local") -> list[str]:
        """Get suggestions — uses AI if the user has enough history, else rule-based."""
        ctx = await self.get_context_summary(user_id)
        if ctx["is_first_session"]:
            return []

        profile = await self.get_user_profile(user_id)

        # Try AI suggestions once the user shows real activity. Count downloads
        # and uploads too (not just scouts+generates) — the behavior engine now
        # records the full funnel, so a user who downloaded + analyzed but hasn't
        # generated still has strong signal worth tailoring to. Threshold 5->3.
        activity_signal = (
            ctx["total_scouts"]
            + ctx["total_generated"]
            + ctx.get("total_uploads", 0)
            + ctx.get("downloaded_not_generated", 0)
        )
        if profile and activity_signal >= 3:
            ai_suggestions = await self._get_ai_suggestions(user_id, ctx, profile)
            if ai_suggestions:
                return ai_suggestions

        # Fallback: rule-based suggestions
        return await self._get_rule_based_suggestions(user_id, ctx, profile)

    async def _get_ai_suggestions(self, user_id: str, ctx: dict, profile: dict) -> list[str] | None:
        """AI-generated suggestions based on full context. Cached in UserProfile for 1h."""
        # Check cache
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )
            up = result.scalar_one_or_none()
            if up and up.suggestions_json:
                try:
                    cached = json.loads(up.suggestions_json)
                    cache_age = (datetime.utcnow() - (up.suggestions_updated_at or datetime.min)).total_seconds()
                    if cache_age < 3600:  # 1 hour TTL
                        return cached
                except (json.JSONDecodeError, TypeError):
                    pass

        # Generate fresh suggestions via AI
        perf = await self.get_performance_insights(user_id)
        try:
            from backend.core.ai_provider import get_ai_client
            ai = get_ai_client()

            prompt = f"""You are ViralMint's suggestion engine. Given this creator's context, generate exactly 3 short, specific, actionable suggestions.

Context: {json.dumps(ctx)}
Profile: {json.dumps(profile, ensure_ascii=False)[:1000]}
Performance: {json.dumps(perf)[:500] if perf else "No data yet"}

Rules:
- Each suggestion must be 1 sentence, under 80 characters
- Be specific (mention actual niches, numbers, platforms)
- Prioritize: unfinished pipeline steps > trending opportunities > automation
- Never suggest something generic like "explore new content"

Return a JSON array of 3 strings. No explanation."""

            response = await ai.chat(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=256,
            )
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            suggestions = json.loads(cleaned)
            if isinstance(suggestions, list) and len(suggestions) >= 1:
                suggestions = [s for s in suggestions[:3] if isinstance(s, str)]

                # Cache the result
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(UserProfile).where(UserProfile.user_id == user_id)
                    )
                    up = result.scalar_one_or_none()
                    if up:
                        up.suggestions_json = json.dumps(suggestions)
                        up.suggestions_updated_at = datetime.utcnow()
                        await db.commit()

                return suggestions
        except Exception as e:
            logger.debug(f"AI suggestions failed (falling back to rule-based): {e}")
        return None

    async def _get_rule_based_suggestions(self, user_id: str, ctx: dict, profile: dict | None) -> list[str]:
        """Original rule-based suggestions as fallback."""
        suggestions = []

        if ctx["downloaded_not_generated"] > 0:
            best = await self._get_best_ungenerated_video(user_id)
            if best:
                title_short = (best["title"] or "Untitled")[:50]
                score = best.get("virality_score")
                if score:
                    suggestions.append(f"Generate a video from '{title_short}' (virality score: {score:.0f})")
                else:
                    suggestions.append(f"Generate a video from '{title_short}'")
            else:
                n = ctx["downloaded_not_generated"]
                suggestions.append(f"Generate videos from {n} analyzed competitor video{'s' if n > 1 else ''}")

        if ctx["generated_not_uploaded"] > 0:
            n = ctx["generated_not_uploaded"]
            suggestions.append(f"Upload {n} ready video{'s' if n > 1 else ''} to YouTube or TikTok")

        if profile:
            niches = profile.get("niches", [])
            if len(niches) >= 2 and ctx["total_scouts"] >= 5:
                recent_niche = ctx["last_niche"]
                other_niches = [n for n in niches if n != recent_niche]
                if other_niches:
                    suggestions.append(f"Scout '{other_niches[0]}' — it's been a while since you checked that niche")

        if ctx["last_niche"] and ctx["total_scouts"] > 0 and len(suggestions) < 2:
            suggestions.append(f"Scout fresh '{ctx['last_niche']}' content — new trending videos may have appeared")

        return suggestions[:3]

    async def _get_best_ungenerated_video(self, user_id: str) -> dict | None:
        """Find the best downloaded+analyzed video that hasn't been used to generate yet."""
        from backend.models.downloaded_video import DownloadedVideo
        from backend.models.generated_video import GeneratedVideo
        from backend.models.scout_result import ScoutResult

        async with AsyncSessionLocal() as db:
            # Get IDs of downloaded videos that already have generated videos
            generated_source_ids = set()
            gen_result = await db.execute(
                select(GeneratedVideo.source_downloaded_video_id)
                .where(GeneratedVideo.user_id == user_id)
                .where(GeneratedVideo.source_downloaded_video_id.isnot(None))
            )
            for row in gen_result.fetchall():
                generated_source_ids.add(row[0])

            # Get downloaded videos with insights, not yet generated
            result = await db.execute(
                select(DownloadedVideo)
                .where(DownloadedVideo.user_id == user_id)
                .where(DownloadedVideo.insights_json.isnot(None))
                .order_by(DownloadedVideo.created_at.desc())
                .limit(20)
            )
            downloads = result.scalars().all()

            best = None
            best_score = -1
            for d in downloads:
                if d.id in generated_source_ids:
                    continue
                # Try to get virality score from scout result
                score = 0
                title = None
                if d.scout_result_id:
                    sr = await db.execute(
                        select(ScoutResult).where(ScoutResult.id == d.scout_result_id)
                    )
                    scout = sr.scalar_one_or_none()
                    if scout:
                        score = scout.virality_score or 0
                        title = scout.title
                if score > best_score:
                    best_score = score
                    best = {"id": d.id, "title": title or d.video_path, "virality_score": score if score > 0 else None}

            return best

    async def get_credential_status(self, user_settings, user_id: str = "local") -> dict:
        """Return which services are configured vs missing, with health info for cookies."""
        from backend.config import settings as env

        # All keys are BYOK from .env (or per-user UserSettings for AI provider).
        ai_user_key = bool(user_settings and user_settings.ai_api_key_encrypted)
        status = {
            "ai_provider": {
                "configured": bool(env.ANTHROPIC_API_KEY or env.OPENAI_API_KEY or ai_user_key),
            },
            "youtube_scout": {
                "configured": bool(env.YOUTUBE_API_KEY),
            },
            "tiktok_scout": {
                "configured": bool(env.TIKHUB_API_KEY) or (
                    user_settings and bool(user_settings.tiktok_cookie_encrypted)
                ),
            },
            "douyin_scout": {
                "configured": bool(env.TIKHUB_API_KEY) or (
                    user_settings and bool(user_settings.douyin_cookie_encrypted)
                ),
            },
            "youtube_upload": {
                "configured": user_settings and bool(user_settings.youtube_credentials_json_encrypted),
                "setup_wizard": "youtube_auth",
            },
            "tiktok_upload": {
                "configured": user_settings and (bool(user_settings.tiktok_upload_token_encrypted) or bool(user_settings.tiktok_cookie_encrypted)),
                "setup_wizard": "tiktok_upload_auth",
            },
            "voice_generation": {
                # Edge TTS works without any key; OpenAI TTS uses OPENAI_API_KEY.
                "configured": True,
            },
            "stock_footage": {
                "configured": bool(env.PEXELS_API_KEY),
            },
        }

        # Add cookie age warnings
        if user_settings:
            for platform, cookie_field, set_at_field in [
                ("tiktok_scout", "tiktok_cookie_encrypted", "tiktok_cookie_set_at"),
                ("douyin_scout", "douyin_cookie_encrypted", "douyin_cookie_set_at"),
            ]:
                cookie = getattr(user_settings, cookie_field, None)
                set_at = getattr(user_settings, set_at_field, None)
                if cookie and set_at:
                    age_days = (datetime.utcnow() - set_at).days
                    status[platform]["age_days"] = age_days

        return status

    async def get_recent_failures(self, user_id: str = "local") -> list[str]:
        """Get human-readable summaries of recent job failures (last 24h).
        These are surfaced in the planner prompt so the AI can mention them proactively."""
        from backend.models.job import Job

        since = datetime.utcnow() - timedelta(hours=24)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Job)
                .where(Job.user_id == user_id)
                .where(Job.status == "failed")
                .where(Job.created_at >= since)
                .order_by(Job.created_at.desc())
                .limit(5)
            )
            failed_jobs = result.scalars().all()

        if not failed_jobs:
            return []

        summaries = []
        for job in failed_jobs:
            error = (job.error_message or "unknown error")[:100]
            title = job.title or job.job_type
            summaries.append(f"{job.job_type} '{title}' failed: {error}")
        return summaries

    # ── Private helpers ──────────────────────────────────────────────────────────

    async def _build_event_summary_for_profile(self, user_id: str) -> str:
        """Build a text summary of recent events for the AI profile generator."""
        async with AsyncSessionLocal() as db:
            # Get last 200 events
            result = await db.execute(
                select(UserBehavior)
                .where(UserBehavior.user_id == user_id)
                .order_by(UserBehavior.created_at.desc())
                .limit(200)
            )
            events = result.scalars().all()

        if not events:
            return ""

        # Group by event type with details
        by_type = {}
        for e in events:
            if e.event_type not in by_type:
                by_type[e.event_type] = []
            data = json.loads(e.data_json or "{}")
            data["_timestamp"] = e.created_at.isoformat() if e.created_at else ""
            by_type[e.event_type].append(data)

        lines = [f"Total events analyzed: {len(events)}"]
        lines.append(f"Time range: {events[-1].created_at.isoformat()} to {events[0].created_at.isoformat()}")
        lines.append("")

        for event_type, items in by_type.items():
            lines.append(f"## {event_type} ({len(items)} events)")
            # Show up to 10 examples per type
            for item in items[:10]:
                ts = item.pop("_timestamp", "")
                lines.append(f"  - [{ts[:16]}] {json.dumps(item, ensure_ascii=False)[:200]}")
            if len(items) > 10:
                lines.append(f"  ... and {len(items) - 10} more")
            lines.append("")

        return "\n".join(lines)

    async def generate_content_calendar(self, user_id: str = "local", days: int = 7) -> list[dict] | None:
        """
        AI generates a day-by-day content plan synthesizing:
        user profile + performance insights + trend data + posting time recs.
        Returns a list of daily content suggestions.
        """
        from backend.core.ai_provider import get_ai_client
        from backend.services.performance_tracker import recommend_posting_time

        profile = await self.get_user_profile(user_id)
        perf = await self.get_performance_insights(user_id)
        posting_rec = await recommend_posting_time(user_id)

        # Build context for AI
        profile_text = json.dumps(profile, indent=2, ensure_ascii=False) if profile else "New user, no profile yet."
        perf_text = json.dumps(perf, indent=2) if perf else "No performance data yet."
        posting_text = json.dumps(posting_rec, indent=2) if posting_rec else "No posting time data."

        from datetime import date, timedelta as td
        start_date = date.today()
        date_range = [(start_date + td(days=i)).isoformat() for i in range(days)]

        prompt = f"""You are a content strategist for a video creator. Generate a {days}-day content calendar.

## Creator Profile
{profile_text}

## Past Performance
{perf_text}

## Optimal Posting Times
{posting_text}

## Date Range
{', '.join(date_range)}

Generate a JSON array with one entry per day:
[
  {{
    "date": "YYYY-MM-DD",
    "topic": "specific video topic idea",
    "platform": "youtube_shorts or tiktok or youtube_long",
    "posting_time": "HH:MM",
    "why": "1 sentence explaining why this topic + timing is strategic"
  }}
]

Rules:
- Use the creator's best-performing niches and topics
- Mix content types (educational, entertaining, trending)
- Avoid repeating the same topic two days in a row
- Use optimal posting times if data is available
- Be specific with topics — not generic like "cooking video" but "5 budget meals under $3"

Return ONLY the JSON array."""

        try:
            ai = get_ai_client()
            response = await ai.chat(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
            )
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            calendar = json.loads(cleaned)
            if isinstance(calendar, list):
                return calendar
        except Exception as e:
            logger.warning(f"Content calendar generation failed: {e}")
        return None

    async def get_performance_insights(self, user_id: str = "local") -> dict | None:
        """
        Analyze uploaded video performance to feed back into AI decisions.
        Returns insights about what content works best for this creator.
        """
        from backend.models.generated_video import GeneratedVideo
        from backend.models.video_metrics import VideoMetrics
        from backend.core.exceptions import safe_json_loads

        async with AsyncSessionLocal() as db:
            # Get uploaded videos with metrics
            result = await db.execute(
                select(GeneratedVideo).where(
                    GeneratedVideo.user_id == user_id,
                    GeneratedVideo.status == "uploaded",
                ).order_by(GeneratedVideo.created_at.desc()).limit(50)
            )
            videos = result.scalars().all()

        if len(videos) < 3:
            return None  # Not enough data

        # Collect performance data per video
        video_data = []
        async with AsyncSessionLocal() as db:
            for v in videos:
                metrics_result = await db.execute(
                    select(VideoMetrics)
                    .where(VideoMetrics.generated_video_id == v.id)
                    .order_by(VideoMetrics.fetched_at.desc())
                    .limit(1)
                )
                latest = metrics_result.scalar_one_or_none()
                if not latest:
                    continue

                engagement = (latest.likes + latest.comments * 2) / max(latest.views, 1) * 100
                video_data.append({
                    "title": v.title,
                    "niche": v.niche or "",
                    "views": latest.views,
                    "likes": latest.likes,
                    "comments": latest.comments,
                    "engagement_rate": round(engagement, 2),
                    "gen_tier": v.gen_tier,
                    "aspect_ratio": v.aspect_ratio,
                })

        if len(video_data) < 3:
            return None

        # Sort by views to find top performers
        video_data.sort(key=lambda x: x["views"], reverse=True)

        # Compute niche performance
        niche_stats = {}
        for vd in video_data:
            n = vd["niche"] or "unknown"
            if n not in niche_stats:
                niche_stats[n] = {"views": [], "engagement": []}
            niche_stats[n]["views"].append(vd["views"])
            niche_stats[n]["engagement"].append(vd["engagement_rate"])

        best_niches = sorted(
            niche_stats.keys(),
            key=lambda n: sum(niche_stats[n]["views"]) / len(niche_stats[n]["views"]),
            reverse=True,
        )[:5]

        engagement_by_niche = {
            n: round(sum(niche_stats[n]["engagement"]) / len(niche_stats[n]["engagement"]), 1)
            for n in best_niches
        }

        return {
            "best_niches": best_niches,
            "top_videos": video_data[:5],
            "engagement_rate_by_niche": engagement_by_niche,
            "total_videos_analyzed": len(video_data),
            "avg_views": round(sum(v["views"] for v in video_data) / len(video_data)),
            "avg_engagement": round(sum(v["engagement_rate"] for v in video_data) / len(video_data), 2),
        }

    async def get_news_context(self, user_id: str = "local") -> dict:
        """Build news-specific context for the planner prompt (Layer 7: News Memory)."""
        async with AsyncSessionLocal() as db:
            # Get all news-related events
            result = await db.execute(
                select(UserBehavior)
                .where(UserBehavior.user_id == user_id)
                .where(UserBehavior.event_type.in_(["news_scouted", "news_saved", "news_generated", "news_dismissed"]))
                .order_by(UserBehavior.created_at.desc())
                .limit(200)
            )
            events = result.scalars().all()

        if not events:
            return {"total_news_scouts": 0}

        # Parse events
        news_queries = []
        saved_topics = []
        dismissed_topics = []
        sources_used = []
        last_query = None
        last_scout_at = None
        total_saved = 0
        total_generated = 0

        for e in events:
            data = json.loads(e.data_json or "{}")
            if e.event_type == "news_scouted":
                q = data.get("query", "")
                if q:
                    news_queries.append(q)
                    if not last_query:
                        last_query = q
                        last_scout_at = e.created_at
                sources_used.extend(data.get("sources") or [])
            elif e.event_type == "news_saved":
                total_saved += data.get("count", 0)
                # Track which topics were saved
                if data.get("query"):
                    saved_topics.append(data["query"])
            elif e.event_type == "news_generated":
                total_generated += 1
            elif e.event_type == "news_dismissed":
                topic = data.get("topic", "")
                if topic:
                    dismissed_topics.append(topic)

        # Most-scouted news queries
        query_counts = {}
        for q in news_queries:
            query_counts[q] = query_counts.get(q, 0) + 1
        top_news_niches = sorted(query_counts, key=query_counts.get, reverse=True)[:5]

        # Most-used sources
        source_counts = {}
        for s in sources_used:
            source_counts[s] = source_counts.get(s, 0) + 1
        preferred_sources = sorted(source_counts, key=source_counts.get, reverse=True)[:4]

        # Days since last scout
        last_scout_days_ago = None
        if last_scout_at:
            last_scout_days_ago = (datetime.utcnow() - last_scout_at).days

        return {
            "total_news_scouts": len([e for e in events if e.event_type == "news_scouted"]),
            "top_news_niches": top_news_niches,
            "last_news_query": last_query,
            "last_news_scout_days_ago": last_scout_days_ago,
            "total_articles_saved": total_saved,
            "total_news_videos_generated": total_generated,
            "preferred_sources": preferred_sources,
            "dismissed_topics": list(set(dismissed_topics))[:5],
            "saved_not_generated": max(total_saved - total_generated, 0),
        }
