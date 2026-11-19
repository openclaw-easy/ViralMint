# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Trend Velocity Alerts — detect when keywords spike above their baseline.

Catches trends 2-3 weeks before they peak by comparing current interest
to the 7-day average. Sends proactive alerts via WebSocket.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select, and_
from backend.database import AsyncSessionLocal
from backend.core.http_utils import get_user_agent

logger = logging.getLogger(__name__)


@dataclass
class TrendAlert:
    keyword: str
    current_interest: float      # today's relative interest (0-100)
    baseline_interest: float     # 7-day average interest
    velocity_multiplier: float   # e.g. 3.5x = 350% above baseline
    alert_level: str             # "spike" | "rising" | "steady" | "declining" | "unknown"
    # Why a reading is "unknown": "lookup_failed" (Google Trends errored or refused) or "no_data" (too little search
    # volume). Before this both came back as a fake "steady", so a broken lookup looked like a quiet week.
    note: str = ""


async def check_keyword_velocity(keyword: str) -> TrendAlert:
    """
    Compare current search interest to 7-day baseline using Google Trends.
    Returns a TrendAlert with velocity classification.
    """
    def _fetch():
        try:
            from pytrends.request import TrendReq
            pytrends = TrendReq(hl="en-US", tz=360)
            pytrends.build_payload([keyword], timeframe="now 7-d")
            df = pytrends.interest_over_time()

            if df.empty or keyword not in df.columns:
                return TrendAlert(
                    keyword=keyword,
                    current_interest=0,
                    baseline_interest=0,
                    velocity_multiplier=1.0,
                    alert_level="unknown",
                    note="no_data",
                )

            values = df[keyword].tolist()
            if len(values) < 2:
                return TrendAlert(
                    keyword=keyword,
                    current_interest=values[0] if values else 0,
                    baseline_interest=values[0] if values else 0,
                    velocity_multiplier=1.0,
                    alert_level="unknown",
                    note="no_data",
                )

            # Split into baseline (first 75%) and current (last 25%)
            split_idx = max(1, int(len(values) * 0.75))
            baseline = sum(values[:split_idx]) / max(split_idx, 1)
            current = sum(values[split_idx:]) / max(len(values) - split_idx, 1)

            velocity = current / max(baseline, 1)

            if velocity >= 3.0:
                alert_level = "spike"
            elif velocity >= 1.5:
                alert_level = "rising"
            elif velocity >= 0.8:
                alert_level = "steady"
            else:
                alert_level = "declining"

            return TrendAlert(
                keyword=keyword,
                current_interest=round(current, 1),
                baseline_interest=round(baseline, 1),
                velocity_multiplier=round(velocity, 2),
                alert_level=alert_level,
            )
        except Exception as e:
            logger.warning(f"Trend velocity check failed for '{keyword}': {e}")
            return TrendAlert(
                keyword=keyword,
                current_interest=0,
                baseline_interest=0,
                velocity_multiplier=1.0,
                alert_level="unknown",
                note="lookup_failed",
            )

    return await asyncio.to_thread(_fetch)


async def check_user_keywords_velocity(user_id: str = "local") -> list[TrendAlert]:
    """A reading for each of the user's five most recently scouted niches (last 30 days), every one with its
    alert_level and note: a lookup that failed says so instead of vanishing from an "alerts only" list. Nothing is
    pushed anywhere (the old WS `trend_alert` reached no screen); callers show the readings.
    """
    from backend.models.user_behavior import UserBehavior

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserBehavior)
            .where(
                and_(
                    UserBehavior.user_id == user_id,
                    UserBehavior.event_type == "niche_searched",
                    UserBehavior.created_at >= datetime.utcnow() - timedelta(days=30),
                )
            )
            .order_by(UserBehavior.created_at.desc())
            .limit(50)
        )
        events = result.scalars().all()

    # Newest first, each niche once (a set lost the order and picked five at random).
    niches: dict[str, None] = {}
    for e in events:
        try:
            data = json.loads(e.data_json or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("niche"):
            niches.setdefault(str(data["niche"]), None)

    readings = []
    for keyword in list(niches)[:5]:          # five, to stay under Google Trends' rate limit
        readings.append(await check_keyword_velocity(keyword))
        await asyncio.sleep(1)
    return readings


async def get_trending_keywords(keywords: list[str]) -> list[dict]:
    """
    Check velocity for a list of keywords. Returns sorted by velocity.
    """
    results = []
    for kw in keywords[:10]:
        alert = await check_keyword_velocity(kw)
        results.append({
            "keyword": alert.keyword,
            "current_interest": alert.current_interest,
            "baseline_interest": alert.baseline_interest,
            "velocity": alert.velocity_multiplier,
            "alert_level": alert.alert_level,
            "note": alert.note,
        })
        await asyncio.sleep(0.5)

    results.sort(key=lambda x: x["velocity"], reverse=True)
    return results


async def cross_platform_correlation(keyword: str) -> dict:
    """
    Check a keyword across multiple platforms to identify cross-platform momentum.
    Combines: Google Trends velocity + YouTube search results + Reddit mentions.

    A keyword trending on all 3 platforms has the strongest signal.
    """
    import httpx

    # Run all checks in parallel
    google_task = asyncio.create_task(check_keyword_velocity(keyword))
    youtube_task = asyncio.create_task(_check_youtube_signal(keyword))
    reddit_task = asyncio.create_task(_check_reddit_signal(keyword))

    google_alert = await google_task
    youtube_signal = await youtube_task
    reddit_signal = await reddit_task

    # Compute cross-platform score
    signals = []
    platforms_trending = []

    if google_alert.alert_level in ("spike", "rising"):
        signals.append(("google_trends", google_alert.velocity_multiplier))
        platforms_trending.append("google_trends")

    if youtube_signal.get("is_trending"):
        signals.append(("youtube", youtube_signal.get("recency_score", 1.0)))
        platforms_trending.append("youtube")

    if reddit_signal.get("is_trending"):
        signals.append(("reddit", reddit_signal.get("engagement_score", 1.0)))
        platforms_trending.append("reddit")

    # Cross-platform momentum: stronger when multiple platforms agree
    momentum = len(platforms_trending)
    if momentum >= 3:
        confidence = "very_high"
    elif momentum == 2:
        confidence = "high"
    elif momentum == 1:
        confidence = "moderate"
    else:
        confidence = "low"

    # Combined score (0-100)
    combined = 0
    if signals:
        avg_signal = sum(s[1] for s in signals) / len(signals)
        combined = min(avg_signal * momentum * 15, 100)

    return {
        "keyword": keyword,
        "cross_platform_score": round(combined, 1),
        "confidence": confidence,
        "platforms_trending": platforms_trending,
        "platform_count": momentum,
        "google_trends": {
            "velocity": google_alert.velocity_multiplier,
            "alert_level": google_alert.alert_level,
            "note": google_alert.note,
            "current_interest": google_alert.current_interest,
        },
        "youtube": youtube_signal,
        "reddit": reddit_signal,
    }


async def _check_youtube_signal(keyword: str) -> dict:
    """Check if keyword has recent high-view videos on YouTube (proxy for trending)."""
    import httpx

    from backend.config import settings as env
    api_key = env.YOUTUBE_API_KEY

    if not api_key:
        return {"is_trending": False, "reason": "no_api_key"}

    def _fetch():
        try:
            resp = httpx.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "part": "snippet",
                    "q": keyword,
                    "type": "video",
                    "order": "date",
                    "publishedAfter": (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "maxResults": 10,
                    "key": api_key,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                return {"is_trending": False, "reason": "api_error"}

            data = resp.json()
            total = data.get("pageInfo", {}).get("totalResults", 0)
            items = data.get("items", [])

            # High signal: many recent videos (>50 in last 7 days)
            is_trending = total > 50
            recency_score = min(total / 100.0, 3.0)

            return {
                "is_trending": is_trending,
                "recent_videos_count": total,
                "recency_score": round(recency_score, 2),
                "sample_titles": [i["snippet"]["title"] for i in items[:3]],
            }
        except Exception as e:
            logger.warning(f"YouTube signal check failed for '{keyword}': {e}")
            return {"is_trending": False, "reason": str(e)}

    return await asyncio.to_thread(_fetch)


async def _check_reddit_signal(keyword: str) -> dict:
    """Check if keyword has recent high-engagement Reddit posts."""
    import httpx

    def _fetch():
        try:
            resp = httpx.get(
                "https://www.reddit.com/search.json",
                params={"q": keyword, "sort": "new", "t": "week", "limit": 10},
                headers={"User-Agent": get_user_agent()},
                timeout=10,
            )
            if resp.status_code != 200:
                return {"is_trending": False, "reason": "api_error"}

            posts = resp.json().get("data", {}).get("children", [])
            if not posts:
                return {"is_trending": False, "post_count": 0, "engagement_score": 0}

            total_score = sum(p["data"].get("score", 0) for p in posts)
            total_comments = sum(p["data"].get("num_comments", 0) for p in posts)
            avg_score = total_score / len(posts)

            # Trending if average score > 100 or many posts
            is_trending = avg_score > 100 or len(posts) >= 8
            engagement_score = min(avg_score / 200.0 + len(posts) / 10.0, 3.0)

            return {
                "is_trending": is_trending,
                "post_count": len(posts),
                "avg_score": round(avg_score, 1),
                "total_comments": total_comments,
                "engagement_score": round(engagement_score, 2),
            }
        except Exception as e:
            logger.warning(f"Reddit signal check failed for '{keyword}': {e}")
            return {"is_trending": False, "reason": str(e)}

    return await asyncio.to_thread(_fetch)
