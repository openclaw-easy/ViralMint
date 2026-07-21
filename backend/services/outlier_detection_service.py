# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Outlier Video Detection — identifies videos that massively outperform their channel's baseline.

vidIQ calls this "Outlier Score". A video with 10x the channel's median views is a
content goldmine — the topic/format resonated far beyond the creator's usual audience.

Classification:
  >= 3x  channel median → OUTLIER
  >= 5x  → STRONG OUTLIER
  >= 10x → BREAKOUT
  >= 20x → MONSTER (extremely rare, usually viral)

Usage:
  # Compute outlier scores for a list of videos from the same channel
  enriched = compute_outlier_scores(videos, channel_median=50000)

  # Fetch channel stats + compute median from YouTube API
  stats = await get_channel_baseline(channel_id, api_key)
  # Returns: {"channel_id": "UC...", "median_views": 50000, "avg_views": 75000, ...}
"""
import asyncio
import logging
import statistics
from typing import Optional

logger = logging.getLogger(__name__)


# ── Outlier classification thresholds ────────────────────────────────────────

OUTLIER_THRESHOLDS = [
    (20.0, "MONSTER"),     # 20x+ median: extremely viral
    (10.0, "BREAKOUT"),    # 10x+ median: breakout hit
    (5.0,  "STRONG"),      # 5x+ median: strong outlier
    (3.0,  "OUTLIER"),     # 3x+ median: above average
]


def classify_outlier(outlier_score: float) -> Optional[str]:
    """Classify an outlier score into a human-readable tier."""
    if not outlier_score or outlier_score < 3.0:
        return None
    for threshold, label in OUTLIER_THRESHOLDS:
        if outlier_score >= threshold:
            return label
    return None


# ── Core computation ─────────────────────────────────────────────────────────

def compute_outlier_scores(
    videos: list[dict],
    channel_median: float = None,
    channel_avg: float = None,
    subscriber_count: int = 0,
) -> list[dict]:
    """
    Enrich a list of videos with outlier_score and outlier_label.

    Each video dict should have at least: views (int), and optionally view_count.
    Uses channel_median as the baseline. Falls back to channel_avg, then subscriber heuristic.

    Returns the same list with outlier_score and outlier_label added to each dict.
    """
    baseline = channel_median
    if not baseline or baseline < 1:
        baseline = channel_avg
    if not baseline or baseline < 1:
        if subscriber_count and subscriber_count > 0:
            baseline = subscriber_count * 0.03  # ~3% of subs typically view a video
        else:
            baseline = None

    for video in videos:
        views = video.get("views") or video.get("view_count", 0)
        if baseline and baseline > 0 and views > 0:
            score = round(views / baseline, 1)
            video["outlier_score"] = score
            video["outlier_label"] = classify_outlier(score)
            video["channel_avg_views"] = round(baseline, 0)
        else:
            video["outlier_score"] = None
            video["outlier_label"] = None

    return videos


def compute_channel_stats(view_counts: list[int]) -> dict:
    """
    Compute median and average views from a list of view counts.

    Returns:
        {
            "median_views": 50000,
            "avg_views": 75000,
            "total_videos": 42,
            "p25_views": 20000,    # 25th percentile
            "p75_views": 120000,   # 75th percentile
            "max_views": 5000000,
        }
    """
    if not view_counts:
        return {"median_views": 0, "avg_views": 0, "total_videos": 0,
                "p25_views": 0, "p75_views": 0, "max_views": 0}

    # Filter out zero-view videos (likely unlisted/private)
    valid = [v for v in view_counts if v > 0]
    if not valid:
        return {"median_views": 0, "avg_views": 0, "total_videos": len(view_counts),
                "p25_views": 0, "p75_views": 0, "max_views": 0}

    sorted_views = sorted(valid)
    n = len(sorted_views)

    return {
        "median_views": int(statistics.median(sorted_views)),
        "avg_views": int(statistics.mean(sorted_views)),
        "total_videos": n,
        "p25_views": int(sorted_views[n // 4]) if n >= 4 else int(sorted_views[0]),
        "p75_views": int(sorted_views[3 * n // 4]) if n >= 4 else int(sorted_views[-1]),
        "max_views": int(sorted_views[-1]),
    }


# ── YouTube API integration ─────────────────────────────────────────────────

async def get_channel_baseline(channel_id: str, api_key: str) -> Optional[dict]:
    """
    Fetch a YouTube channel's recent videos and compute baseline view stats.
    Uses the last 50 videos to compute median/average (captures recent performance).

    Returns:
        {
            "channel_id": "UCxxxx",
            "channel_title": "Creator Name",
            "subscriber_count": 100000,
            "median_views": 50000,
            "avg_views": 75000,
            "total_videos_sampled": 50,
            "p25_views": 20000,
            "p75_views": 120000,
        }
    """
    def _fetch():
        from googleapiclient.discovery import build
        youtube = build("youtube", "v3", developerKey=api_key)

        # Get channel info + uploads playlist
        ch_resp = youtube.channels().list(
            part="snippet,statistics,contentDetails",
            id=channel_id,
        ).execute()

        if not ch_resp.get("items"):
            return None

        ch = ch_resp["items"][0]
        uploads_id = ch["contentDetails"]["relatedPlaylists"]["uploads"]
        subscriber_count = int(ch["statistics"].get("subscriberCount", 0))
        channel_title = ch["snippet"].get("title", "")

        # Fetch last 50 videos from uploads playlist
        # YouTube API sometimes returns 500 for UU-prefix playlists.
        # Try multiple playlist prefixes with retry on transient 5xx.
        import time as _time
        from googleapiclient.errors import HttpError

        channel_suffix = uploads_id[2:] if uploads_id.startswith("UU") else uploads_id
        playlist_ids_to_try = [uploads_id]
        if uploads_id.startswith("UU"):
            for prefix in ("UULF", "UUSH", "UULV"):
                pid = prefix + channel_suffix
                if pid != uploads_id:
                    playlist_ids_to_try.append(pid)

        pl_items = []
        for pid in playlist_ids_to_try:
            for attempt in range(2):
                try:
                    pl_resp = youtube.playlistItems().list(
                        part="contentDetails",
                        playlistId=pid,
                        maxResults=50,
                    ).execute()
                    pl_items = pl_resp.get("items", [])
                    break
                except HttpError as e:
                    status = e.resp.status if hasattr(e, 'resp') else 0
                    if status in (500, 502, 503) and attempt == 0:
                        _time.sleep(1.5)
                        continue
                    logger.warning(f"playlistItems failed for {pid} (HTTP {status})")
                    break
            if pl_items:
                break

        video_ids = [item["contentDetails"]["videoId"] for item in pl_items]
        if not video_ids:
            return {
                "channel_id": channel_id,
                "channel_title": channel_title,
                "subscriber_count": subscriber_count,
                "median_views": 0,
                "avg_views": 0,
                "total_videos_sampled": 0,
            }

        # Batch-fetch video stats
        stats_resp = youtube.videos().list(
            part="statistics",
            id=",".join(video_ids),
        ).execute()

        view_counts = []
        for v in stats_resp.get("items", []):
            views = int(v["statistics"].get("viewCount", 0))
            view_counts.append(views)

        stats = compute_channel_stats(view_counts)
        return {
            "channel_id": channel_id,
            "channel_title": channel_title,
            "subscriber_count": subscriber_count,
            "median_views": stats["median_views"],
            "avg_views": stats["avg_views"],
            "total_videos_sampled": stats["total_videos"],
            "p25_views": stats["p25_views"],
            "p75_views": stats["p75_views"],
        }

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.warning(f"Failed to get channel baseline for {channel_id}: {e}")
        return None


async def batch_get_channel_baselines(
    channel_ids: list[str],
    api_key: str,
) -> dict[str, dict]:
    """
    Fetch baselines for multiple channels in parallel.
    Returns: { channel_id: baseline_dict }

    Deduplicates channel_ids and caches results.
    """
    unique_ids = list(set(channel_ids))
    if not unique_ids:
        return {}

    # Limit concurrency to avoid API rate limits
    semaphore = asyncio.Semaphore(3)

    async def _fetch_one(cid: str):
        async with semaphore:
            return cid, await get_channel_baseline(cid, api_key)

    results = await asyncio.gather(
        *[_fetch_one(cid) for cid in unique_ids],
        return_exceptions=True,
    )

    baselines = {}
    for result in results:
        if isinstance(result, Exception):
            continue
        cid, baseline = result
        if baseline:
            baselines[cid] = baseline

    return baselines


def enrich_scout_results_with_outliers(
    results: list[dict],
    channel_baselines: dict[str, dict],
) -> list[dict]:
    """
    Enrich scout results with outlier scores using pre-fetched channel baselines.

    Each result must have: author_url (containing channel ID) and views.
    """
    import re

    for result in results:
        # `.get(key, "")` doesn't guard an EXPLICIT None value — ytdlp-fallback
        # rows carry author_url=None, which reaches re.search and crashes.
        author_url = result.get("author_url") or ""
        channel_id = None

        # Extract channel ID from URL
        match = re.search(r'/channel/(UC[\w-]+)', author_url)
        if match:
            channel_id = match.group(1)

        if not channel_id:
            continue

        baseline = channel_baselines.get(channel_id)
        if not baseline:
            continue

        views = result.get("views", 0)
        median = baseline.get("median_views", 0)
        avg = baseline.get("avg_views", 0)
        subs = baseline.get("subscriber_count", 0)

        # Store channel stats on the result
        result["channel_avg_views"] = avg or median
        result["subscriber_count"] = subs

        # Compute outlier score using median (more robust than average)
        effective_baseline = median if median > 0 else avg
        if effective_baseline and effective_baseline > 0 and views > 0:
            result["outlier_score"] = round(views / effective_baseline, 1)
        elif subs and subs > 0 and views > 0:
            # Fallback: use subscriber-based estimate
            estimated_avg = subs * 0.03
            result["outlier_score"] = round(views / estimated_avg, 1) if estimated_avg > 0 else None

    return results
