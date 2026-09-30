# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Channel reader service — fetches YouTube/TikTok channel data using public APIs.
YouTube: Data API v3 with API key (no OAuth needed).
TikTok: yt-dlp extraction (no API key needed).
"""
import asyncio
import json
import logging
import re
import time
from typing import Optional

from backend.config import settings
from backend.core.executors import download_pool

logger = logging.getLogger(__name__)

# ── Simple in-memory cache (5-min TTL) ────────────────────────────────────────

_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL = 300  # seconds


def _cache_get(key: str) -> Optional[dict]:
    if key in _cache:
        data, ts = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
        del _cache[key]
    return None


def _cache_set(key: str, data: dict):
    _cache[key] = (data, time.time())


def cache_clear(prefix: str = ""):
    keys_to_del = [k for k in _cache if k.startswith(prefix)] if prefix else list(_cache.keys())
    for k in keys_to_del:
        del _cache[k]


# ── YouTube (API key — public data, no OAuth) ────────────────────────────────

async def resolve_youtube_channel_id(url: str, api_key: str) -> Optional[str]:
    """Resolve a YouTube channel URL to a channel ID using the Data API."""
    # Try to extract channel ID directly from URL
    # Formats: /channel/UCxxxx, /@handle, /c/name, /user/name
    channel_id_match = re.search(r'/channel/(UC[\w-]+)', url)
    if channel_id_match:
        return channel_id_match.group(1)

    # For @handle, /c/name, /user/name — use search or forHandle
    handle_match = re.search(r'/@([\w.-]+)', url)
    if handle_match:
        handle = handle_match.group(1)
        return await _resolve_handle(handle, api_key)

    # Try /c/ or /user/ patterns
    name_match = re.search(r'/(?:c|user)/([\w.-]+)', url)
    if name_match:
        name = name_match.group(1)
        return await _resolve_handle(name, api_key)

    return None


async def _resolve_handle(handle: str, api_key: str) -> Optional[str]:
    """Resolve a YouTube handle to channel ID via the Data API."""
    def _resolve():
        from googleapiclient.discovery import build
        youtube = build("youtube", "v3", developerKey=api_key)
        # forHandle parameter works for @handles
        resp = youtube.channels().list(part="id", forHandle=handle).execute()
        if resp.get("items"):
            return resp["items"][0]["id"]
        # Fallback: search
        resp = youtube.search().list(part="snippet", q=handle, type="channel", maxResults=1).execute()
        if resp.get("items"):
            return resp["items"][0]["snippet"]["channelId"]
        return None
    try:
        return await asyncio.to_thread(_resolve)
    except Exception as e:
        logger.warning(f"Failed to resolve YouTube handle '{handle}': {e}")
        return None


async def get_youtube_channel_info(channel_id: str, api_key: str) -> Optional[dict]:
    """Fetch basic YouTube channel info (name, thumbnail, stats)."""
    def _fetch():
        from googleapiclient.discovery import build
        youtube = build("youtube", "v3", developerKey=api_key)
        resp = youtube.channels().list(part="snippet,statistics", id=channel_id).execute()
        if not resp.get("items"):
            return None
        ch = resp["items"][0]
        return {
            "title": ch["snippet"]["title"],
            "thumbnail_url": ch["snippet"]["thumbnails"].get("medium", {}).get("url", ""),
            "subscriber_count": int(ch["statistics"].get("subscriberCount", 0)),
            "video_count": int(ch["statistics"].get("videoCount", 0)),
        }
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.warning(f"Failed to fetch YouTube channel info: {e}")
        return None


async def search_youtube_channels(query: str, api_key: str, max_results: int = 5) -> list[dict]:
    """Search YouTube for channels matching the query. Returns list of channel summaries."""
    def _search():
        from googleapiclient.discovery import build
        youtube = build("youtube", "v3", developerKey=api_key)

        # Search for channels
        resp = youtube.search().list(
            part="snippet",
            q=query,
            type="channel",
            maxResults=max_results,
        ).execute()

        if not resp.get("items"):
            return []

        # Batch-fetch channel stats for subscriber counts
        channel_ids = [item["snippet"]["channelId"] for item in resp["items"]]
        stats_resp = youtube.channels().list(
            part="snippet,statistics",
            id=",".join(channel_ids),
        ).execute()

        stats_map = {}
        for ch in stats_resp.get("items", []):
            stats_map[ch["id"]] = ch

        results = []
        for item in resp["items"]:
            cid = item["snippet"]["channelId"]
            ch = stats_map.get(cid, {})
            stats = ch.get("statistics", {})
            snippet = ch.get("snippet", item["snippet"])
            results.append({
                "channel_id": cid,
                "title": snippet.get("title", ""),
                "description": snippet.get("description", "")[:150],
                "thumbnail_url": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                "custom_url": snippet.get("customUrl", ""),
                "subscriber_count": int(stats.get("subscriberCount", 0)),
                "video_count": int(stats.get("videoCount", 0)),
                "url": f"https://www.youtube.com/channel/{cid}",
            })

        return results

    return await asyncio.to_thread(_search)


async def search_tiktok_profiles(query: str, max_results: int = 5) -> list[dict]:
    """Search TikTok for profiles. Uses yt-dlp profile lookup."""
    def _search():
        import yt_dlp
        import re

        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "playlistend": 1,
        }

        # Extract handle from full URL or clean up query
        handle = query.strip()
        url_match = re.search(r'tiktok\.com/@([^/?]+)', handle)
        if url_match:
            handle = url_match.group(1)
        else:
            handle = handle.lstrip("@").strip()

        if not handle:
            return []

        results = []
        # Direct profile lookup (most reliable)
        try:
            url = f"https://www.tiktok.com/@{handle}"
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                # Extract thumbnail from list format
                thumb = ""
                if info.get("thumbnails"):
                    thumb = info["thumbnails"][-1].get("url", "")
                elif info.get("thumbnail"):
                    thumb = info["thumbnail"]
                results.append({
                    "username": handle,
                    "display_name": info.get("channel") or info.get("uploader") or info.get("title", handle),
                    "thumbnail_url": thumb,
                    "follower_count": info.get("channel_follower_count") or 0,
                    "video_count": info.get("playlist_count") or len(info.get("entries", [])),
                    "url": url,
                })
        except Exception as e:
            logger.debug(f"TikTok handle lookup failed for '{handle}': {e}")

        return results

    # yt-dlp can fail to return — its own pool, never the shared default
    # executor (backend/core/executors.py).
    results = await download_pool.run(_search)

    # Enrich with scraped profile data (follower count, avatar)
    for r in results:
        scraped = await _scrape_tiktok_profile(r["url"])
        if scraped:
            if scraped.get("follower_count"):
                r["follower_count"] = scraped["follower_count"]
            if scraped.get("video_count"):
                r["video_count"] = scraped["video_count"]
            if scraped.get("avatar_url"):
                r["thumbnail_url"] = scraped["avatar_url"]
            if scraped.get("display_name"):
                r["display_name"] = scraped["display_name"]

    return results


async def get_youtube_channel(
    channel_id: str,
    api_key: str,
    page_token: Optional[str] = None,
    max_results: int = 200,
) -> dict:
    """
    Fetch YouTube channel stats + video list using API key (public data).
    No OAuth required. Fetches up to max_results videos across multiple API pages.

    Resilience layers:
      1. Try playlist API with multiple playlist ID prefixes (UU, UULF, UUSH, UULV)
      2. Retry transient 5xx errors with backoff
      3. Fall back to YouTube Search API (no playlists, uses search quota)
      4. Fall back to yt-dlp flat extraction (no API quota at all)
    """
    cache_key = f"yt_{channel_id}_{page_token or 'first'}_{max_results}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    def _fetch():
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        youtube = build("youtube", "v3", developerKey=api_key)

        # 1. Channel stats
        ch_resp = youtube.channels().list(part="snippet,statistics,contentDetails", id=channel_id).execute()
        if not ch_resp.get("items"):
            return {"connected": True, "channel": None, "videos": [], "next_page_token": None, "total": 0}

        ch = ch_resp["items"][0]
        channel_info = {
            "channel_id": ch["id"],
            "title": ch["snippet"]["title"],
            "description": ch["snippet"].get("description", ""),
            "thumbnail_url": ch["snippet"]["thumbnails"].get("medium", {}).get("url", ""),
            "custom_url": ch["snippet"].get("customUrl", ""),
            "subscriber_count": int(ch["statistics"].get("subscriberCount", 0)),
            "total_views": int(ch["statistics"].get("viewCount", 0)),
            "video_count": int(ch["statistics"].get("videoCount", 0)),
        }

        # 2. Fetch videos — try playlist API first, then search API, then yt-dlp
        all_items, next_token = _fetch_via_playlist_api(
            youtube, ch, page_token, max_results, channel_id
        )

        if not all_items and not page_token:
            # Playlist API failed entirely — try search API fallback
            logger.info(f"Playlist API failed for {channel_id}, falling back to search API")
            all_items = _fetch_via_search_api(youtube, channel_id, max_results)
            next_token = None

        if not all_items and not page_token:
            # Search API also failed — last resort: yt-dlp
            logger.info(f"Search API failed for {channel_id}, falling back to yt-dlp")
            all_items = _fetch_via_ytdlp(channel_id, max_results)
            next_token = None

        if not all_items:
            logger.warning(f"All video fetch methods failed for channel {channel_id}")
            result = {"connected": True, "channel": channel_info, "videos": [], "next_page_token": None, "total": 0}
            return result

        # 3. Batch-fetch video stats for items that don't already have them
        video_ids = [item.get("video_id") or item.get("contentDetails", {}).get("videoId", "") for item in all_items]
        video_ids = [vid for vid in video_ids if vid]

        # Check if items already have stats (from search/yt-dlp fallbacks)
        already_has_stats = all_items[0].get("_has_stats", False) if all_items else False

        stats_map = {}
        if not already_has_stats:
            for i in range(0, len(video_ids), 50):
                batch = video_ids[i:i + 50]
                try:
                    stats_resp = youtube.videos().list(
                        part="statistics,contentDetails",
                        id=",".join(batch),
                    ).execute()
                    for v in stats_resp.get("items", []):
                        stats_map[v["id"]] = v
                except HttpError as e:
                    logger.warning(f"Batch stats fetch failed: {e}")

        # 4. Build video list
        videos = []
        view_counts = []
        for item in all_items:
            vid = item.get("video_id") or item.get("contentDetails", {}).get("videoId", "")
            if not vid:
                continue

            if already_has_stats:
                # Item already has stats from search/yt-dlp fallback
                vc = item.get("view_count", 0)
                view_counts.append(vc)
                videos.append({
                    "video_id": vid,
                    "title": item.get("title", ""),
                    "description": item.get("description", "")[:200],
                    "thumbnail_url": item.get("thumbnail_url", ""),
                    "published_at": item.get("published_at", ""),
                    "view_count": vc,
                    "like_count": item.get("like_count", 0),
                    "comment_count": item.get("comment_count", 0),
                    "duration": item.get("duration", ""),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                })
            else:
                # Standard playlist item — merge with stats
                snippet = item.get("snippet", {})
                stats = stats_map.get(vid, {})
                s = stats.get("statistics", {})
                cd = stats.get("contentDetails", {})
                vc = int(s.get("viewCount", 0))
                view_counts.append(vc)
                videos.append({
                    "video_id": vid,
                    "title": snippet.get("title", ""),
                    "description": snippet.get("description", "")[:200],
                    "thumbnail_url": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "view_count": vc,
                    "like_count": int(s.get("likeCount", 0)),
                    "comment_count": int(s.get("commentCount", 0)),
                    "duration": cd.get("duration", ""),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                })

        # 5. Compute outlier scores using channel median
        from backend.services.outlier_detection_service import (
            compute_channel_stats, compute_outlier_scores,
        )
        ch_stats = compute_channel_stats(view_counts)
        compute_outlier_scores(
            videos,
            channel_median=ch_stats["median_views"],
            channel_avg=ch_stats["avg_views"],
            subscriber_count=channel_info.get("subscriber_count", 0),
        )
        channel_info["median_views"] = ch_stats["median_views"]
        channel_info["avg_views"] = ch_stats["avg_views"]

        return {
            "connected": True,
            "channel": channel_info,
            "videos": videos,
            "next_page_token": next_token,
            "total": len(videos),
        }

    # May fall back to yt-dlp (_fetch_via_ytdlp), which can fail to return —
    # so this runs on the download pool, not the shared default executor.
    result = await download_pool.run(_fetch)
    _cache_set(cache_key, result)
    return result


def _fetch_via_playlist_api(youtube, ch, page_token, max_results, channel_id) -> tuple[list, Optional[str]]:
    """
    Layer 1: Fetch videos via playlistItems API.
    Tries multiple playlist ID prefixes with retry on transient 5xx.
    Returns (items, next_page_token).
    """
    import time as _time
    from googleapiclient.errors import HttpError

    uploads_id = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    channel_suffix = uploads_id[2:] if uploads_id.startswith("UU") else uploads_id

    # YouTube has multiple upload playlist prefixes; UU can 500, others may work
    playlist_ids_to_try = [uploads_id]
    if uploads_id.startswith("UU"):
        for prefix in ("UULF", "UUSH", "UULV"):
            pid = prefix + channel_suffix
            if pid != uploads_id:
                playlist_ids_to_try.append(pid)

    per_page = min(max_results, 50)

    for pid in playlist_ids_to_try:
        for attempt in range(3):  # retry transient errors up to 3 times
            all_items = []
            current_token = page_token
            try:
                while len(all_items) < max_results:
                    pl_kwargs = {"part": "snippet,contentDetails", "playlistId": pid, "maxResults": per_page}
                    if current_token:
                        pl_kwargs["pageToken"] = current_token
                    pl_resp = youtube.playlistItems().list(**pl_kwargs).execute()
                    all_items.extend(pl_resp.get("items", []))
                    current_token = pl_resp.get("nextPageToken")
                    if not current_token:
                        break
                if all_items:
                    logger.info(f"playlistItems succeeded with {pid} ({len(all_items)} items)")
                    return all_items, current_token
                break  # empty but no error — skip retries, try next prefix
            except HttpError as e:
                status = e.resp.status if hasattr(e, 'resp') else 0
                if status in (500, 502, 503) and attempt < 2:
                    delay = (attempt + 1) * 1.5
                    logger.info(f"playlistItems {pid} returned {status}, retry {attempt+1}/2 in {delay}s")
                    _time.sleep(delay)
                    continue
                logger.warning(f"playlistItems failed for {pid} (HTTP {status}), trying next prefix")
                break  # non-retryable or exhausted retries — try next prefix

    logger.warning(f"All playlist prefixes failed for channel {channel_id}")
    return [], None


def _fetch_via_search_api(youtube, channel_id: str, max_results: int) -> list:
    """
    Layer 2: Fall back to YouTube Search API when playlist API fails.
    Costs more quota (100 units/call vs 1) but doesn't use playlists.
    Returns items with _has_stats=True (stats already included).
    """
    from googleapiclient.errors import HttpError

    try:
        # search().list costs 100 quota units, but is more reliable
        search_resp = youtube.search().list(
            part="snippet",
            channelId=channel_id,
            type="video",
            order="date",
            maxResults=min(max_results, 50),
        ).execute()

        if not search_resp.get("items"):
            return []

        # Batch-fetch stats for the found videos
        video_ids = [item["id"]["videoId"] for item in search_resp["items"]]
        stats_map = {}
        try:
            stats_resp = youtube.videos().list(
                part="snippet,statistics,contentDetails",
                id=",".join(video_ids),
            ).execute()
            for v in stats_resp.get("items", []):
                stats_map[v["id"]] = v
        except HttpError:
            pass  # continue without stats

        items = []
        for item in search_resp["items"]:
            vid = item["id"]["videoId"]
            stats = stats_map.get(vid, {})
            s = stats.get("statistics", {})
            cd = stats.get("contentDetails", {})
            snippet = stats.get("snippet", item.get("snippet", {}))
            items.append({
                "video_id": vid,
                "title": snippet.get("title", ""),
                "description": snippet.get("description", "")[:200],
                "thumbnail_url": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                "published_at": snippet.get("publishedAt", ""),
                "view_count": int(s.get("viewCount", 0)),
                "like_count": int(s.get("likeCount", 0)),
                "comment_count": int(s.get("commentCount", 0)),
                "duration": cd.get("duration", ""),
                "_has_stats": True,
            })

        logger.info(f"Search API fallback returned {len(items)} videos for {channel_id}")
        return items

    except HttpError as e:
        logger.warning(f"Search API fallback failed for {channel_id}: {e}")
        return []


def _fetch_via_ytdlp(channel_id: str, max_results: int) -> list:
    """
    Layer 3: Last resort — use yt-dlp to extract channel videos.
    Zero API quota cost. Works even when YouTube API is completely broken.
    Returns items with _has_stats=True.
    """
    try:
        import yt_dlp

        url = f"https://www.youtube.com/channel/{channel_id}/videos"
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "playlistend": min(max_results, 100),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info or not info.get("entries"):
            return []

        items = []
        for entry in info["entries"]:
            if not entry:
                continue
            thumb = ""
            if entry.get("thumbnails"):
                # Pick a medium-quality thumbnail
                for t in entry["thumbnails"]:
                    if t.get("url"):
                        thumb = t["url"]
                        break
            items.append({
                "video_id": entry.get("id", ""),
                "title": entry.get("title", ""),
                "description": (entry.get("description") or "")[:200],
                "thumbnail_url": thumb,
                "published_at": "",
                "view_count": entry.get("view_count") or 0,
                "like_count": entry.get("like_count") or 0,
                "comment_count": entry.get("comment_count") or 0,
                "duration": entry.get("duration_string") or "",
                "_has_stats": True,
            })

        logger.info(f"yt-dlp fallback returned {len(items)} videos for {channel_id}")
        return items

    except Exception as e:
        logger.warning(f"yt-dlp fallback failed for {channel_id}: {e}")
        return []


# ── TikTok (yt-dlp — public data, no API key needed) ─────────────────────────

async def get_tiktok_channel(profile_url: str, max_videos: int = 200) -> dict:
    """
    Fetch TikTok profile + video list using yt-dlp.
    No API key or OAuth needed — reads public data.

    Default cap matches get_youtube_channel's 200. It used to be 20, which
    silently truncated the My Channels grid (the one caller that takes the
    default) to a fifth of what the YouTube side shows. The TikTok yt-dlp scrape
    pulls the profile list in ONE request with no native pagination, so a higher
    cap is not meaningfully slower for the same channel.
    """
    cache_key = f"tt_{profile_url}_{max_videos}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    def _fetch():
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "playlistend": max_videos,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(profile_url, download=False)

        if not info:
            return {"connected": True, "user": None, "videos": []}

        # Extract profile info from channel metadata
        # yt-dlp returns 'thumbnails' (list) not 'thumbnail' (string)
        profile_thumb = ""
        if info.get("thumbnails"):
            profile_thumb = info["thumbnails"][-1].get("url", "")
        elif info.get("thumbnail"):
            profile_thumb = info["thumbnail"]

        user_info = {
            "display_name": info.get("channel") or info.get("uploader") or info.get("title", ""),
            "avatar_url": profile_thumb,
            "follower_count": info.get("channel_follower_count") or 0,
            "video_count": info.get("playlist_count") or len(info.get("entries", [])),
        }

        # Extract username for building TikTok video URLs
        # yt-dlp provides uploader_id (@username) or we parse from the profile URL
        tiktok_username = info.get("uploader_id") or info.get("channel_id") or ""
        if not tiktok_username and "/@" in profile_url:
            tiktok_username = profile_url.split("/@")[-1].split("/")[0].split("?")[0]
        if tiktok_username and not tiktok_username.startswith("@"):
            tiktok_username = f"@{tiktok_username}"

        entries = info.get("entries", [])
        videos = []
        for entry in entries:
            if not entry:
                continue
            video_url = entry.get("url") or entry.get("webpage_url", "")
            if video_url and not video_url.startswith("http"):
                # TikTok URLs require @username: tiktok.com/@user/video/ID
                if tiktok_username:
                    video_url = f"https://www.tiktok.com/{tiktok_username}/video/{video_url}"
                else:
                    video_url = f"https://www.tiktok.com/video/{video_url}"
            # Extract thumbnail from 'thumbnails' list (yt-dlp format)
            cover = ""
            if entry.get("thumbnails"):
                cover = entry["thumbnails"][-1].get("url", "")
            elif entry.get("thumbnail"):
                cover = entry["thumbnail"]
            videos.append({
                "video_id": entry.get("id", ""),
                "title": entry.get("title", ""),
                "cover_url": cover,
                "created_at": entry.get("timestamp") or 0,
                "view_count": entry.get("view_count") or 0,
                "like_count": entry.get("like_count") or 0,
                "comment_count": entry.get("comment_count") or 0,
                "share_count": entry.get("repost_count") or 0,
                "duration": entry.get("duration") or 0,
                "url": video_url,
            })

        return {
            "connected": True,
            "user": user_info,
            "videos": videos,
            "has_more": False,
        }

    try:
        result = await download_pool.run(_fetch)   # yt-dlp — see search_tiktok_profiles
        # Enrich user info with scraped profile data (follower count, avatar)
        scraped = await _scrape_tiktok_profile(profile_url)
        if scraped and result.get("user"):
            user = result["user"]
            if not user.get("follower_count") and scraped.get("follower_count"):
                user["follower_count"] = scraped["follower_count"]
            if not user.get("avatar_url") and scraped.get("avatar_url"):
                user["avatar_url"] = scraped["avatar_url"]
            if scraped.get("display_name"):
                user["display_name"] = scraped["display_name"]
            if not user.get("video_count") and scraped.get("video_count"):
                user["video_count"] = scraped["video_count"]
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        logger.error(f"TikTok channel fetch failed: {e}", exc_info=True)
        return {"connected": True, "error": str(e), "user": None, "videos": []}


async def _scrape_tiktok_profile(profile_url: str) -> Optional[dict]:
    """
    Scrape TikTok profile page for follower count, avatar, etc.
    yt-dlp doesn't provide follower counts for TikTok, so we parse the
    __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON embedded in the page HTML.
    """
    import re

    def _fetch():
        import httpx
        from backend.core.http_utils import get_default_headers
        resp = httpx.get(profile_url, headers=get_default_headers(),
                         follow_redirects=True, timeout=10)
        m = re.search(
            r'<script\s+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            resp.text, re.DOTALL,
        )
        if not m:
            return None
        import json
        data = json.loads(m.group(1))
        user_detail = data.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {})
        user_info = user_detail.get("userInfo", {})
        stats = user_info.get("stats", {})
        user = user_info.get("user", {})
        if not user:
            return None
        return {
            "display_name": user.get("nickname") or user.get("uniqueId", ""),
            "avatar_url": user.get("avatarLarger") or user.get("avatarMedium", ""),
            "follower_count": stats.get("followerCount", 0),
            "video_count": stats.get("videoCount", 0),
        }

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.debug(f"TikTok profile scrape failed for {profile_url}: {e}")
        return None


async def get_tiktok_channel_info(profile_url: str) -> Optional[dict]:
    """
    Quick TikTok profile info fetch (name, avatar, follower count).
    Tries HTML scrape first (gives real follower count), falls back to yt-dlp.
    """
    # Try scraping first — gives accurate follower count
    scraped = await _scrape_tiktok_profile(profile_url)
    if scraped and scraped.get("display_name"):
        return scraped

    # Fallback to yt-dlp (no follower count, but at least gets the name)
    try:
        result = await get_tiktok_channel(profile_url, max_videos=1)
        user = result.get("user")
        if not user or not user.get("display_name"):
            return None
        return {
            "display_name": user.get("display_name", ""),
            "avatar_url": user.get("avatar_url", ""),
            "follower_count": user.get("follower_count", 0),
            "video_count": user.get("video_count", 0),
        }
    except Exception as e:
        logger.debug(f"TikTok profile parse failed: {e}")
        return None
