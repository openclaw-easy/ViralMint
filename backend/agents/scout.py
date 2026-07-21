# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Agent 2: Multi-platform scout + virality scoring.
Runs all platforms in parallel via asyncio.gather.
If one platform fails, logs warning + continues with others.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from backend.database import AsyncSessionLocal
from backend.models.scout_result import ScoutResult
from backend.models.user_settings import UserSettings
from backend.core.ws_manager import ws_manager
from backend.agents.job_helper import update_job_status
from backend.config import settings

logger = logging.getLogger(__name__)


PLATFORM_LIMITS = {
    "youtube": 50,
    "tiktok": 30,
    "douyin": 30,
}

# Default limit for any platform not listed above
DEFAULT_SEARCH_LIMIT = 15

# Platforms that can be scouted via yt-dlp search (no API key needed)
# Maps platform name → yt-dlp search prefix
# Note: bilisearch requires cookies (412), so Bilibili falls back to ytsearch
YTDLP_SEARCH_PLATFORMS = {
    "soundcloud": "scsearch",
    "niconico": "nicosearch",
}

# Universal fallback: search YouTube for "{platform} {niche}" content
# This catches Bilibili, Instagram, Vimeo, etc. — surprisingly effective
# because creators often cross-post or discuss other platforms' content
FALLBACK_SEARCH_PREFIX = "ytsearch"

# Hard cap on platforms per scout run — every entry costs a search (+ possible
# AI retry). Six covers every legitimate combination in the UI.
MAX_PLATFORMS_PER_SCOUT = 6


def compute_virality_score(video: dict) -> float:
    """
    Virality score 0-100.
    Weights: engagement rate 30%, view velocity (VPH) 25%, recency 20%,
             raw views 15%, raw likes 10%.
    Also computes views_per_hour and outlier_score as side data on the dict.
    """
    likes = max(video.get("likes", 0), 0)
    views = max(video.get("views", 1), 1)
    comments = max(video.get("comments", 0), 0)

    upload_date = video.get("upload_date")
    if upload_date and isinstance(upload_date, datetime):
        # Normalise: many feeds pass naïve datetimes (assumed UTC).
        # Using datetime.now(timezone.utc) on a naïve upload_date would
        # raise TypeError — convert here so virality scoring tolerates
        # either tz-aware or tz-naïve inputs.
        if upload_date.tzinfo is None:
            upload_date = upload_date.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        hours_old = max((now_utc - upload_date).total_seconds() / 3600, 1)
        days_old = max(hours_old / 24, 1)
    else:
        hours_old = 720  # assume 30 days
        days_old = 30

    engagement_rate = (likes + comments * 2) / views
    recency_bonus = 1.0 / (1 + days_old / 30)
    views_score = min(views / 1_000_000, 1.0)
    likes_score = min(likes / 100_000, 1.0)

    # View velocity — views per hour, normalized (10K VPH = max score)
    vph = views / hours_old
    vph_score = min(vph / 10_000, 1.0)

    # Store VPH on the dict for DB storage
    video["views_per_hour"] = round(vph, 1)

    # Outlier score — how many x above channel average
    channel_avg = video.get("channel_avg_views")
    if not channel_avg or channel_avg < 1:
        subs = video.get("subscriber_count", 0) or 0
        channel_avg = max(subs * 0.03, 100) if subs > 0 else None
    if channel_avg and channel_avg > 0:
        video["outlier_score"] = round(views / channel_avg, 1)

    raw = (
        engagement_rate * 0.30
        + vph_score * 0.25
        + recency_bonus * 0.20
        + views_score * 0.15
        + likes_score * 0.10
    )
    return round(min(raw * 100, 100.0), 2)


class ScoutAgent:
    # Set by run() when it has already emitted ONE aggregated fallback notice —
    # the per-platform warning in _scout_via_ytdlp_search then stays quiet.
    _suppress_fallback_warning = False

    async def run(
        self,
        job_id: str,
        niche: str,
        platforms: list[str],
        user_id: str = "local",
    ):
        """Run scout across all specified platforms in parallel."""
        logger.info("SCOUT START | job=%s niche=%r platforms=%s", job_id[:8], niche, platforms)

        # Hygiene on the platform list — it can arrive from the chat agent, MCP,
        # or the UI. Dedupe (preserving order) and cap: an LLM once passed 34
        # invented "platforms" in one call, which meant 34 searches + AI retries
        # (a ~70s grind) and a warning toast per fallback platform.
        seen: set[str] = set()
        cleaned: list[str] = []
        for p in platforms:
            p = str(p).strip()
            if p and p.lower() not in seen:
                seen.add(p.lower())
                cleaned.append(p)
        if len(cleaned) > MAX_PLATFORMS_PER_SCOUT:
            logger.warning("SCOUT platform list capped %d → %d (dropped: %s)",
                           len(cleaned), MAX_PLATFORMS_PER_SCOUT,
                           cleaned[MAX_PLATFORMS_PER_SCOUT:])
            cleaned = cleaned[:MAX_PLATFORMS_PER_SCOUT]
        platforms = cleaned or ["youtube"]

        # One AGGREGATED fallback notice for all platforms without a native
        # scout — the per-platform warning inside _scout_via_ytdlp_search
        # became a toast storm when several were requested at once.
        fallback_platforms = [
            p for p in platforms
            if p not in ("youtube", "tiktok", "douyin")
            and p not in YTDLP_SEARCH_PLATFORMS
        ]
        if len(fallback_platforms) > 1:
            try:
                await ws_manager.send_constraint_warning(
                    constraint="multi_platform_fallback",
                    message=(f"No direct search for {', '.join(fallback_platforms[:6])}"
                             f"{'…' if len(fallback_platforms) > 6 else ''} — "
                             f"showing YouTube cross-posts about \"{niche}\" instead."),
                    severity="warning",
                    user_id=user_id,
                )
            except Exception:  # noqa: BLE001
                pass
            self._suppress_fallback_warning = True

        await update_job_status(job_id, "running", progress_pct=0, current_step="Starting scout...")
        await ws_manager.send_progress(job_id, 0, "Starting scout...", user_id)

        # Load user settings for credentials
        async with AsyncSessionLocal() as db:
            from sqlalchemy import select
            result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            user_settings = result.scalar_one_or_none()

        # Build platform tasks
        tasks = []
        for platform in platforms:
            tasks.append(self._scout_platform(platform, niche, user_settings, user_id=user_id))

        # Run all in parallel
        platform_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect all results, with AI-assisted retry for failures/empty results
        all_results = []
        for i, result in enumerate(platform_results):
            platform = platforms[i]
            if isinstance(result, Exception):
                logger.warning(f"Scout failed for {platform}: {result}")
                await ws_manager.send_constraint_warning(
                    constraint=f"{platform}_scout",
                    message=f"Scout failed for {platform}: {result}",
                    severity="warning",
                    user_id=user_id,
                )
                # AI fallback: try raw HTTP search when library-based approach throws
                try:
                    fallback = await self._ai_raw_search_fallback(platform, niche, user_settings)
                    if fallback:
                        logger.info(f"AI raw search fallback recovered {len(fallback)} results for {platform}")
                        result = fallback
                    else:
                        continue
                except Exception as fb_err:
                    logger.debug(f"AI raw search fallback failed for {platform}: {fb_err}")
                    continue

            # AI-assisted retry: if platform returned 0 results, refine search terms
            if result is not None and len(result) == 0:
                try:
                    from backend.core.ai_retry import ai_refine_search
                    refined_niche = await ai_refine_search(platform, niche, user_settings)
                    if refined_niche:
                        logger.info(f"Retrying {platform} scout with refined niche: '{refined_niche}'")
                        await ws_manager.send_progress(job_id, 0, f"Refining search for {platform}...", user_id)
                        retry_result = await self._scout_platform(platform, refined_niche, user_settings, user_id=user_id)
                        if retry_result and not isinstance(retry_result, Exception):
                            result = retry_result
                            logger.info(f"AI-refined search found {len(result)} results on {platform}")
                except Exception as retry_err:
                    logger.debug(f"AI search refinement retry failed for {platform}: {retry_err}")

            if result:
                all_results.extend(result)

            pct = ((i + 1) / len(platforms)) * 80
            await ws_manager.send_progress(job_id, pct, f"Scouted {platform}", user_id)

        logger.info("SCOUT collected %d raw results from %d platforms", len(all_results), len(platforms))

        # Enrich YouTube results with real outlier detection (batch channel
        # baselines). Enrichment is a bonus, never a dependency — a bug here
        # must not fail a scout that already collected results (rule #10; a
        # None author_url did exactly that on 2026-07-19).
        try:
            await self._enrich_with_outlier_scores(all_results, platforms, user_settings)
        except Exception as enrich_err:  # noqa: BLE001
            logger.warning(f"Outlier enrichment skipped (non-fatal): {enrich_err}")

        # Score all results (now with real channel_avg_views populated)
        for r in all_results:
            r["virality_score"] = compute_virality_score(r)

        # Sort by virality
        all_results.sort(key=lambda r: r["virality_score"], reverse=True)

        # Save to DB. Returns (display_rows, new_count) — display_rows
        # includes EVERY result the user just scouted (new + previously-
        # scouted duplicates with fresh stats). new_count is only the
        # newly-inserted DB rows, used below for the status message.
        await ws_manager.send_progress(job_id, 90, "Saving results...", user_id)
        results_to_send, new_count_from_save = await self._save_results(
            all_results, job_id, niche, user_id,
        )

        # Send results over WS grouped by platform — group by `requested_platform`
        # so a "scout bilibili" request shows results under the bilibili tab even
        # when the underlying URLs are YouTube cross-posts (fallback mode).
        # `requested_platform` defaults to `platform` for native-search results
        # where they're the same value, so this stays correct for soundcloud /
        # niconico / youtube / tiktok / douyin paths.
        for platform in platforms:
            platform_items = [
                r for r in results_to_send
                if r.get("requested_platform", r["platform"]) == platform
            ]
            if platform_items:
                await ws_manager.send({
                    "type": "scout_results",
                    "job_id": job_id,
                    "platform": platform,
                    "total": len(platform_items),
                    "results": platform_items,
                }, user_id)

        # Complete — report both total found and new (non-duplicate) count
        new_count = new_count_from_save
        total_count = len(all_results)
        if new_count == total_count:
            step_msg = f"Found {total_count} results"
        elif new_count == 0:
            step_msg = f"Found {total_count} results (all previously scouted)"
        else:
            step_msg = f"Found {total_count} results ({new_count} new)"

        await update_job_status(
            job_id, "success",
            progress_pct=100,
            current_step=step_msg,
            output_data={"total_results": total_count, "new_results": new_count},
        )
        await ws_manager.send({
            "type": "job_complete",
            "job_id": job_id,
            "result": {"total_results": total_count, "new_results": new_count},
        }, user_id)

    async def _scout_platform(self, platform: str, niche: str, user_settings, user_id: str = "local") -> list[dict]:
        """Scout a single platform. Keys are BYOK (per-user → .env fallback).
        `user_id` is threaded through so the ytdlp-fallback path can emit
        constraint warnings on the user's WS channel.
        """
        from backend.core.api_keys import get_youtube_api_key
        logger.info("SCOUT platform=%s | niche=%r", platform, niche)
        if platform == "youtube":
            youtube_key = get_youtube_api_key(user_settings)
            if not youtube_key:
                logger.warning("YouTube API key not configured — skipping YouTube scout")
                return []
            from backend.services.youtube_scout import search_youtube
            return await search_youtube(niche, youtube_key, PLATFORM_LIMITS["youtube"])

        elif platform == "tiktok":
            # Try TikHub API first (env key)
            if settings.TIKHUB_API_KEY:
                from backend.services.tikhub_client import search_tiktok
                return await search_tiktok(niche, settings.TIKHUB_API_KEY, PLATFORM_LIMITS["tiktok"])
            # Fall back to user's session cookie
            from backend.core.crypto import decrypt_safe
            cookie = ""
            if user_settings and user_settings.tiktok_cookie_encrypted:
                cookie = decrypt_safe(user_settings.tiktok_cookie_encrypted)
            if cookie:
                from backend.services.tiktok_downloader_svc import scout_tiktok_trending
                return await scout_tiktok_trending(cookie, niche, PLATFORM_LIMITS["tiktok"])
            logger.warning("TikTok: no API key or cookie configured")
            return []

        elif platform == "douyin":
            if settings.TIKHUB_API_KEY:
                from backend.services.tikhub_client import search_douyin
                return await search_douyin(niche, settings.TIKHUB_API_KEY, PLATFORM_LIMITS["douyin"])
            from backend.core.crypto import decrypt_safe
            cookie = ""
            if user_settings and user_settings.douyin_cookie_encrypted:
                cookie = decrypt_safe(user_settings.douyin_cookie_encrypted)
            if cookie:
                from backend.services.tiktok_downloader_svc import scout_douyin_trending
                return await scout_douyin_trending(cookie, niche, PLATFORM_LIMITS["douyin"])
            logger.warning("Douyin: no API key or cookie configured")
            return []

        else:
            # Generic fallback: use yt-dlp search if the platform supports it.
            # Pass user_id so the fallback can emit a constraint_warning when
            # there's no native search for the requested platform.
            return await self._scout_via_ytdlp_search(platform, niche, user_id=user_id)

    async def _ai_raw_search_fallback(self, platform: str, niche: str, user_settings) -> list[dict]:
        """
        AI-powered fallback: when the normal scout path fails (library error, API change),
        do a raw HTTP search to the platform's public search endpoint and let AI parse
        the response. Works for any platform without needing API keys.
        """
        import httpx
        from backend.core.ai_retry import ai_parse_api_response

        # Platform-specific public search URLs (no auth needed, HTML/JSON responses)
        search_urls = {
            "youtube": f"https://www.youtube.com/results?search_query={niche}&sp=CAMSAhAB",
            "tiktok": f"https://www.tiktok.com/api/search/general/full/?keyword={niche}&search_source=normal_search",
        }

        url = search_urls.get(platform)
        if not url:
            return []

        try:
            from backend.core.http_utils import get_default_headers
            headers = get_default_headers()
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    logger.debug(f"Raw search fallback got {resp.status_code} for {platform}")
                    return []
                raw_text = resp.text[:6000]

            logger.info(f"AI raw search fallback: fetched {len(raw_text)} chars from {platform}")
            ai_results = await ai_parse_api_response(raw_text, platform, niche, user_settings)

            if not ai_results:
                return []

            # Convert AI output to our standard format
            results = []
            for item in ai_results:
                video_id = item.get("aweme_id") or item.get("video_id", "")
                if not video_id:
                    continue
                results.append({
                    "platform": platform,
                    "video_id": video_id,
                    "video_url": item.get("video_url") or self._build_video_url(platform, video_id, item),
                    "embed_url": None,
                    "title": (item.get("desc") or item.get("title") or "")[:200],
                    "description": item.get("desc") or item.get("description") or "",
                    "author": item.get("author", {}).get("nickname") or item.get("author", {}).get("unique_id") or "Unknown",
                    "author_url": "",
                    "thumbnail_url": "",
                    "views": item.get("statistics", {}).get("play_count", 0) or item.get("views", 0),
                    "likes": item.get("statistics", {}).get("digg_count", 0) or item.get("likes", 0),
                    "comments": item.get("statistics", {}).get("comment_count", 0) or item.get("comments", 0),
                    "shares": item.get("statistics", {}).get("share_count", 0) or item.get("shares", 0),
                    "duration_seconds": item.get("video", {}).get("duration") or item.get("duration_seconds"),
                    "upload_date": None,
                })

            return results

        except Exception as e:
            logger.debug(f"AI raw search fallback error for {platform}: {e}")
            return []

    @staticmethod
    def _build_video_url(platform: str, video_id: str, item: dict) -> str:
        """Build a video URL from platform + video_id."""
        if platform == "youtube":
            return f"https://youtube.com/watch?v={video_id}"
        elif platform == "tiktok":
            author = item.get("author", {}).get("unique_id", "")
            if author:
                return f"https://www.tiktok.com/@{author}/video/{video_id}"
            return f"https://www.tiktok.com/video/{video_id}"
        elif platform == "douyin":
            return f"https://www.douyin.com/video/{video_id}"
        return ""

    async def _enrich_with_outlier_scores(self, results: list[dict], platforms: list[str], user_settings) -> None:
        """
        Batch-fetch channel baselines for YouTube results and compute real outlier scores.
        This replaces the naive subscriber-based heuristic with actual median view data.
        """
        if "youtube" not in platforms:
            return

        youtube_results = [r for r in results if r.get("platform") == "youtube"]
        if not youtube_results:
            return

        from backend.core.api_keys import get_youtube_api_key
        api_key = get_youtube_api_key(user_settings)
        if not api_key:
            return

        # Extract unique channel IDs from author_url
        import re
        channel_ids = set()
        for r in youtube_results:
            # `.get(key, "")` doesn't protect against an EXPLICIT None value —
            # ytdlp-fallback rows carry author_url=None and crashed the whole
            # scout here (2026-07-19), violating rule #10.
            match = re.search(r'/channel/(UC[\w-]+)', r.get("author_url") or "")
            if match:
                channel_ids.add(match.group(1))

        if not channel_ids:
            return

        # Limit to 15 unique channels to avoid excessive API calls
        channel_ids = list(channel_ids)[:15]

        try:
            from backend.services.outlier_detection_service import (
                batch_get_channel_baselines,
                enrich_scout_results_with_outliers,
            )
            baselines = await batch_get_channel_baselines(channel_ids, api_key)
            if baselines:
                enrich_scout_results_with_outliers(youtube_results, baselines)
                logger.info("SCOUT enriched %d YouTube results with outlier scores from %d channels",
                           len(youtube_results), len(baselines))
        except Exception as e:
            logger.warning(f"Outlier enrichment failed (non-fatal): {e}")

    async def _scout_via_ytdlp_search(self, platform: str, niche: str, user_id: str = "local") -> list[dict]:
        """
        Generic scout using yt-dlp's built-in search extractors.
        Works for SoundCloud, Niconico natively.
        For all other platforms, searches YouTube for "{platform} {niche}" content
        which is surprisingly effective since creators cross-post and discuss content.

        When in fallback mode, each result dict carries:
          - `platform = "youtube"` (truth: the URL is YouTube — drives downloader,
            outlier enrichment, dedup keys)
          - `requested_platform = <original>` (what the user asked for — drives
            UI grouping in the WS dispatch)
        Plus a one-time constraint_warning so the user knows the request fell
        back to a cross-post search rather than a native scout.
        """
        import asyncio

        search_prefix = YTDLP_SEARCH_PLATFORMS.get(platform)
        is_fallback = search_prefix is None
        if not is_fallback:
            search_niche = niche
        else:
            # Fallback: search YouTube for content about/from this platform
            search_prefix = FALLBACK_SEARCH_PREFIX
            search_niche = f"{platform} {niche}"
            logger.info(f"No native search for '{platform}' — searching YouTube for '{search_niche}'")
            # Surface the fallback to the user — silent fallback was misleading
            # them into thinking they were scouting Bilibili (etc.) when they
            # were actually getting YouTube cross-posts. Per CLAUDE.md rule #14.
            # Quiet when run() already sent ONE aggregated notice for several
            # fallback platforms (a 34-platform LLM call once toasted 30+
            # of these individually).
            if not self._suppress_fallback_warning:
                try:
                    await ws_manager.send_constraint_warning(
                        constraint=f"{platform}_no_native_search",
                        message=f"No direct {platform} search available — showing YouTube cross-posts about \"{niche}\" instead.",
                        severity="warning",
                        user_id=user_id,
                    )
                except Exception as ws_err:
                    logger.debug(f"constraint_warning emit failed (non-fatal): {ws_err}")

        limit = PLATFORM_LIMITS.get(platform, DEFAULT_SEARCH_LIMIT)
        search_query = f"{search_prefix}{limit}:{search_niche}"

        def _search():
            import yt_dlp
            base_opts = {
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 30,
            }
            # Apply the user's browser cookie jar (BYOK) — yt-dlp's HTTP layer
            # scopes cookies by domain, so this is safe on every search backend
            # (ytsearch, scsearch, bilisearch, nicosearch, etc.). For sites that
            # need auth (Bilibili 412 without session cookies) the jar carries
            # them automatically.
            from backend.services.ytdlp_service import _get_cookie_file
            cookie_file = _get_cookie_file()
            if cookie_file:
                base_opts["cookiefile"] = str(cookie_file)

            # YouTube search (ytsearch) returns rich entries — title, views,
            # thumbnail, uploader — even in flat mode, because YouTube exposes
            # them on the search results page. Other search backends
            # (bilisearch, scsearch, nicosearch) return *only* URL+id in flat
            # mode → empty cards. So: flat for ytsearch fallback (fast, rich
            # data); full extract for native non-YouTube searches.
            if is_fallback:
                with yt_dlp.YoutubeDL({**base_opts, "extract_flat": True}) as ydl:
                    return ydl.extract_info(search_query, download=False)

            # Native search path. Defenses against per-entry failures:
            # - `ignoreerrors=True` — skip 412'd entries instead of aborting
            # - `extractor_retries: 1` — fail fast (default would burn many
            #   retries × yt-dlp HTTP backoff per failed video, turning a
            #   bilibili WBI failure into a multi-minute hang)
            # - `retries: 0` — same, suppress HTTP-layer retries during
            #   search (we want to bail fast and fall back to flat mode)
            full_opts = {
                **base_opts,
                "extract_flat": False,
                "ignoreerrors": True,
                "extractor_retries": 1,
                "retries": 0,
            }
            with yt_dlp.YoutubeDL(full_opts) as ydl:
                info = ydl.extract_info(search_query, download=False)

            # If full extract gave us zero usable entries (every video errored,
            # or the search-level auth itself failed), fall back to flat mode.
            # Cards will be sparse (URL+id only) but the user gets *something*
            # to click through rather than an empty result set + a warning.
            usable_count = sum(1 for e in (info.get("entries") if info else []) or [] if e)
            if usable_count == 0:
                logger.info(
                    f"{platform} full extract returned 0 usable entries "
                    f"(likely auth/WBI failures) — retrying in flat mode"
                )
                with yt_dlp.YoutubeDL({**base_opts, "extract_flat": True}) as ydl:
                    info = ydl.extract_info(search_query, download=False)
            return info

        # Hard ceiling on the whole search — yt-dlp's per-extractor retry
        # logic can ignore our `retries`/`extractor_retries` settings on
        # some platforms (Bilibili's WBI loop will keep hammering on 412
        # for minutes if cookies are auth-incomplete). 60s is a generous
        # cap for a healthy native search (~30s for full extract of 15
        # entries) but lets us bail out fast on auth storms.
        #
        # On timeout / failure: log + return []. We DO NOT emit a WS
        # constraint warning here — `run` will retry with an AI-refined
        # niche, and the refined attempt often succeeds even when the
        # original failed. Firing a warning here was producing
        # contradictory UX: user gets the warning toast AND the result
        # cards from the refined retry, both at once. If everything
        # truly fails (refined retry also empty), the existing
        # "0 results" UI is sufficient feedback.
        SEARCH_HARD_TIMEOUT_S = 60
        try:
            info = await asyncio.wait_for(asyncio.to_thread(_search), timeout=SEARCH_HARD_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                f"yt-dlp search for {platform} ({search_prefix}) hit "
                f"{SEARCH_HARD_TIMEOUT_S}s hard timeout — likely an auth-loop "
                f"storm. Returning empty so caller can AI-refine."
            )
            return []
        except Exception as e:
            logger.warning(f"yt-dlp search failed for {platform} ({search_prefix}): {e}")
            return []

        if not info:
            return []

        entries = info.get("entries") or []
        results = []
        for entry in entries:
            if not entry:
                continue

            video_url = entry.get("url") or entry.get("webpage_url", "")
            video_id = entry.get("id", "")

            # Build full URL if needed
            if video_url and not video_url.startswith("http"):
                video_url = entry.get("webpage_url", video_url)

            # Parse upload_date
            upload_date = None
            raw_date = entry.get("upload_date")
            if raw_date and len(str(raw_date)) == 8:
                try:
                    upload_date = datetime.strptime(str(raw_date), "%Y%m%d")
                except ValueError:
                    pass

            # In fallback mode the URL is actually YouTube (we used ytsearch:),
            # so persist `platform="youtube"` — this drives the DB row, dedup
            # key, outlier enrichment, and the downloader's extractor selection.
            # `requested_platform` carries the user's original ask so the WS
            # dispatch can still group these results under the requested tab.
            actual_platform = "youtube" if is_fallback else platform
            results.append({
                "platform": actual_platform,
                "requested_platform": platform,
                "video_id": video_id,
                "video_url": video_url,
                "title": entry.get("title", ""),
                "description": (entry.get("description") or "")[:500],
                "author": entry.get("uploader") or entry.get("channel", ""),
                "author_url": entry.get("uploader_url") or entry.get("channel_url", ""),
                "thumbnail_url": entry.get("thumbnail") or (entry.get("thumbnails", [{}])[0].get("url") if entry.get("thumbnails") else None),
                "views": entry.get("view_count", 0) or 0,
                "likes": entry.get("like_count", 0) or 0,
                "comments": entry.get("comment_count", 0) or 0,
                "shares": 0,
                "duration_seconds": entry.get("duration"),
                "upload_date": upload_date,
            })

        logger.info(f"yt-dlp search for '{niche}' on {platform}: found {len(results)} results")
        return results

    async def _save_results(
        self, results: list[dict], job_id: str, niche: str, user_id: str
    ) -> tuple[list[dict], int]:
        """Persist new scout results AND return the full display payload.

        Returns (display_rows, new_count):

          - display_rows: one entry per input result. New rows get the
            freshly-assigned DB id; previously-scouted duplicates carry
            the EXISTING row's id but the FRESH stats from this run
            (views/likes/virality drift over time, and the user just
            paid for the fresh fetch — showing stale snapshot data
            would feel broken).
          - new_count: how many of those were net-new inserts.

        Why we don't just dedupe out duplicates — that was the bug.
        Display dedupe (drop duplicates from the WS payload) made
        scout feel weak on a second-run of the same niche: 50 fresh
        results came back, 45 were dupes, only 5 hit the UI. The DB
        dedupe is a STORAGE optimization (don't write the same row
        twice); the DISPLAY should still show every result the user
        actually paid to scout. (Bug fix 2026-05-12.)

        DB row stays as the original scout's snapshot — we do NOT
        update existing rows with the fresh stats. Treating each
        scout as immutable evidence keeps the data analytic-friendly;
        a future explicit "refresh stats" action can update in-place.
        """
        logger.debug("SCOUT saving %d results to DB (niche=%r)", len(results), niche)
        display_rows: list[dict] = []
        new_count = 0
        async with AsyncSessionLocal() as db:
            # Load existing rows as a key → id map. We need the id (not
            # just the existence) so duplicates can still show up in the
            # UI with a stable, clickable id (download, inspect, etc.).
            existing_result = await db.execute(
                select(ScoutResult.id, ScoutResult.video_id, ScoutResult.platform)
                .where(ScoutResult.user_id == user_id)
            )
            existing_id_by_key: dict[tuple[str, str], str] = {
                (row[1], row[2]): row[0]
                for row in existing_result.fetchall()
            }

            for r in results:
                key = (r["video_id"], r["platform"])
                if key in existing_id_by_key:
                    # Duplicate — reuse the existing DB row's id. Don't
                    # write the DB row again. UI still gets a full card
                    # with the FRESH stats from this scout.
                    existing_id = existing_id_by_key[key]
                else:
                    # Net-new — insert and remember the new id.
                    sr = ScoutResult(
                        user_id=user_id,
                        job_id=job_id,
                        platform=r["platform"],
                        video_id=r["video_id"],
                        video_url=r["video_url"],
                        embed_url=r.get("embed_url"),
                        title=r.get("title"),
                        description=r.get("description"),
                        author=r.get("author"),
                        author_url=r.get("author_url"),
                        thumbnail_url=r.get("thumbnail_url"),
                        views=r.get("views", 0),
                        likes=r.get("likes", 0),
                        comments=r.get("comments", 0),
                        shares=r.get("shares", 0),
                        duration_seconds=r.get("duration_seconds"),
                        upload_date=r.get("upload_date"),
                        virality_score=r.get("virality_score", 0),
                        views_per_hour=r.get("views_per_hour"),
                        outlier_score=r.get("outlier_score"),
                        subscriber_count=r.get("subscriber_count"),
                        channel_avg_views=r.get("channel_avg_views"),
                        niche=niche,
                    )
                    db.add(sr)
                    await db.flush()
                    existing_id_by_key[key] = sr.id
                    existing_id = sr.id
                    new_count += 1

                # Build the display payload from `r` (FRESH data) plus
                # the resolved id. Identical shape regardless of whether
                # the row is new or a duplicate.
                upload_date = r.get("upload_date")
                if hasattr(upload_date, "isoformat"):
                    upload_date_str = upload_date.isoformat()
                else:
                    upload_date_str = upload_date
                display_rows.append({
                    "id": existing_id,
                    "platform": r["platform"],
                    # Carry the user's originally-requested platform alongside
                    # the URL-truthful `platform`. Used by run to group results
                    # in the WS dispatch under the requested tab even when the
                    # URL itself is a YouTube cross-post.
                    "requested_platform": r.get("requested_platform", r["platform"]),
                    "video_id": r["video_id"],
                    "video_url": r["video_url"],
                    "embed_url": r.get("embed_url"),
                    "title": r.get("title"),
                    "author": r.get("author"),
                    "author_url": r.get("author_url"),
                    "thumbnail_url": r.get("thumbnail_url"),
                    "views": r.get("views", 0),
                    "likes": r.get("likes", 0),
                    "comments": r.get("comments", 0),
                    "shares": r.get("shares", 0),
                    "duration_seconds": r.get("duration_seconds"),
                    "upload_date": upload_date_str,
                    "virality_score": r.get("virality_score", 0),
                    "views_per_hour": r.get("views_per_hour"),
                    "outlier_score": r.get("outlier_score"),
                })
            await db.commit()
        logger.info(
            "SCOUT processed %d results (%d new, %d previously-scouted)",
            len(display_rows), new_count, len(display_rows) - new_count,
        )
        return display_rows, new_count
