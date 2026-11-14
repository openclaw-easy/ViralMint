# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Metadata without a download: one video's info, a channel's listing, and
just the caption track. Same options and cookie jar as a download (`opts`,
`cookies`); a failure that ends one of these is worded by `engine.terminal_exception`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from backend.core.exceptions import (
    DownloadError,
    DownloadTimeoutError,
    NoCaptionsError,
    VideoUnavailableError,
)
from backend.core.executors import PoolExhaustedError, download_pool
from backend.download import cookies
from backend.download import opts as dl_opts
from backend.download.classify import ErrorClass, classify
from backend.download.util import host_of

logger = logging.getLogger(__name__)


def _ydl_opts(url: str | None, **overrides) -> dict:
    """Options for one metadata call, with the cookie jar. Call it on the pool
    thread: getting the jar may read the browser."""
    return dl_opts.build(url, jar=cookies.store().jar_for_attempt(), **overrides)


# ── Subtitles only (no video, no audio) ─────────────────────────────────────

# The fallback chain for "whatever this video has". The caller's language (or
# the video's own spoken language, when it declares one) goes in front.
_DEFAULT_SUBTITLE_LANGS: tuple[str, ...] = (
    "en", "zh", "ja", "ko", "es", "fr", "de", "pt", "ru", "ar",
)

# Subtitle files are tiny; a healthy fetch finishes in seconds. This bounds a
# wedged extraction. A timed-out call cannot be cancelled (it keeps its worker
# until yt-dlp returns), which is why it runs on `download_pool` and never on
# the shared default executor.
SUBTITLE_FETCH_TIMEOUT_S = 300


def _subtitle_lang_chain(language: str | None, video_language: str | None) -> list[str]:
    """Preference order for the caption track.

    A caller-pinned language wins. With none, the video's OWN language comes
    first: YouTube publishes an auto-translated track in ~150 languages next to
    the original, so without this a Spanish talk resolved to the machine
    translation into English simply because "en" heads the default chain.
    """
    lead = (language or "").strip().lower() or (video_language or "").strip().lower()
    lead = lead.split("-")[0] if lead else ""
    chain = [lead] if lead else []
    return chain + [lang for lang in _DEFAULT_SUBTITLE_LANGS if lang != lead]


def _pick_subtitle_track(
    manual: dict, auto: dict, langs: list[str], prefer_manual: bool = True,
) -> tuple[str, str, bool] | None:
    """Choose `(track_key, base_language, is_auto)` from yt-dlp's track maps.

    Per language, in caller order: a creator track first (exact key, then a
    regional variant such as `en-US` / `es-419`), then the auto-generated one
    (exact key, then YouTube's `<lang>-orig` original-language ASR track,
    then a script or regional variant — YouTube keys Chinese auto tracks
    `zh-Hans` / `zh-Hant`, never `zh`, so exact matching skipped them).
    Returns None when nothing in the chain exists in either map.
    """
    def _variant(tracks: dict, lang: str) -> str | None:
        return next((k for k in sorted(tracks)
                     if k.lower().startswith(f"{lang}-") and not k.endswith("-orig")), None)

    def _manual_key(lang: str) -> str | None:
        return lang if lang in manual else _variant(manual, lang)

    def _auto_key(lang: str) -> str | None:
        for key in (lang, f"{lang}-orig"):
            if key in auto:
                return key
        return _variant(auto, lang)

    for lang in langs:
        m, a = _manual_key(lang), _auto_key(lang)
        if prefer_manual and m:
            return m, lang, False
        if a:
            return a, lang, True
        if m:
            return m, lang, False
    return None


async def extract_subtitles_only(
    url: str,
    output_dir: Path,
    language: str | None = None,
    prefer_manual: bool = True,
) -> dict:
    """Download just the caption track for a URL — no video, no audio bytes.

    Two yt-dlp passes: a metadata probe (so the language is picked
    deterministically and a source with no captions fails with a clear
    message instead of writing nothing), then a subtitles-only fetch into
    `output_dir`. Both run on `download_pool`.

    Args:
        url: A single-video URL on any site yt-dlp supports.
        output_dir: Where the subtitle file is written. The caller owns it.
        language: Optional 2-letter ISO code. The caller's code is tried first,
            then the default chain — so the track returned may be in another
            language; compare `language` in the result if that matters.
        prefer_manual: Creator-uploaded captions beat auto-generated ones.

    Returns:
        {"path", "language" (base 2-letter code), "track" (yt-dlp's key),
         "format" ("srt" | "vtt"), "is_auto_generated", "title", "duration"}

    Raises:
        NoCaptionsError: no caption track in any candidate language, or the
            chosen track could not be written as .srt / .vtt.
        VideoUnavailableError: the URL did not resolve to a single video.
        DownloadTimeoutError: the fetch exceeded SUBTITLE_FETCH_TIMEOUT_S.
        DownloadError: any other yt-dlp failure, after one retry.
    """
    if not url or not isinstance(url, str):
        raise ValueError("url is required")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    def _base_opts(**extra) -> dict:
        return _ydl_opts(url, skip_download=True, noplaylist=True, **extra)

    def _do_extract(drop_cookies: bool) -> dict:
        import yt_dlp

        probe_opts = _base_opts(socket_timeout=15)
        if drop_cookies:
            dl_opts.drop_cookies(probe_opts)
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise VideoUnavailableError("yt-dlp could not resolve the URL.")
        if info.get("entries") is not None or info.get("_type") == "playlist":
            raise VideoUnavailableError(
                "This link is a playlist or channel — paste the link to a single video."
            )

        manual = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}
        langs = _subtitle_lang_chain(language, info.get("language"))
        picked = _pick_subtitle_track(manual, auto, langs, prefer_manual=prefer_manual)
        if not picked:
            available = sorted(set(manual) | {k for k in auto if k.endswith("-orig")})
            tried = ", ".join(langs[:3]) + (f" and {len(langs) - 3} more" if len(langs) > 3 else "")
            raise NoCaptionsError(
                f"This video has no caption track (tried {tried}). "
                f"Source offers: {', '.join(available[:12]) if available else 'none'}."
            )
        track, base_lang, is_auto = picked

        # Stem keyed off the video id so two jobs never share a file name; the
        # directory is per-job anyway.
        stem = f"subs_{re.sub(r'[^A-Za-z0-9_-]', '_', str(info.get('id') or 'video'))}"
        fetch_opts = _base_opts(
            socket_timeout=30,
            writesubtitles=not is_auto,
            writeautomaticsub=is_auto,
            subtitleslangs=[track],
            subtitlesformat="srt/vtt/best",
            outtmpl=str(output_dir / stem) + ".%(ext)s",
        )
        if drop_cookies:
            dl_opts.drop_cookies(fetch_opts)
        with yt_dlp.YoutubeDL(fetch_opts) as ydl:
            ydl.extract_info(url, download=True)

        for ext in ("srt", "vtt"):
            candidate = output_dir / f"{stem}.{track}.{ext}"
            if candidate.exists() and candidate.stat().st_size > 0:
                return {
                    "path": str(candidate),
                    "language": base_lang,
                    "track": track,
                    "format": ext,
                    "is_auto_generated": is_auto,
                    "title": info.get("title") or "",
                    "duration": info.get("duration"),
                }
        raise NoCaptionsError(
            f"The '{track}' caption track exists but could not be fetched as .srt or .vtt."
        )

    import yt_dlp as _yt_dlp_mod

    drop_cookies = False
    for attempt in range(2):
        try:
            return await asyncio.wait_for(
                download_pool.run(_do_extract, drop_cookies),
                timeout=SUBTITLE_FETCH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            # Terminal: the worker is still inside yt-dlp, so a retry would
            # race it. Say what happened instead.
            raise DownloadTimeoutError(
                f"Caption fetch timed out after {SUBTITLE_FETCH_TIMEOUT_S // 60} minutes."
            )
        except PoolExhaustedError as e:
            raise DownloadError(str(e)) from e
        except VideoUnavailableError:
            raise  # no captions / not a video — retrying cannot change it
        except _yt_dlp_mod.utils.DownloadError as e:
            error_class = classify(e, url)
            if attempt == 0 and error_class is ErrorClass.HTTP_403:
                # A cookie jar hides yt-dlp's token-free player clients on
                # YouTube (see policy.decide's 403 row) — retry once without it.
                logger.warning("[DOWNLOAD_STEP] subtitles host=%s class=http_403 — retrying once without cookies",
                               host_of(url))
                drop_cookies = True
                continue
            if attempt == 0 and error_class is ErrorClass.TRANSIENT_NETWORK:
                logger.warning("[DOWNLOAD_STEP] subtitles host=%s class=transient_network — retrying once: %s",
                               host_of(url), str(e)[:200])
                await asyncio.sleep(3)
                continue
            from backend.download.engine import terminal_exception
            raise terminal_exception(e, url) from e
    raise DownloadError("Caption fetch failed.")  # unreachable


# Outer bounds for the metadata-only helpers. A healthy call returns in
# seconds; these exist so a wedged extraction fails the request instead of
# holding it (and a download-pool worker) open indefinitely. A timed-out call
# cannot be cancelled — its worker stays held until yt-dlp returns — which is
# why both also bound what they ask yt-dlp for.
INFO_TIMEOUT_S = 120
LISTING_TIMEOUT_S = 180


async def get_video_info(url: str, flat: bool = False, max_entries: int | None = None) -> dict:
    """Get video/channel metadata without downloading.
    Use flat=True for channels/playlists to avoid resolving every video.

    `max_entries` (flat only) stops the listing there. Pass it for any channel:
    a big channel's whole /videos tab doesn't finish inside INFO_TIMEOUT_S, and
    the timed-out thread keeps its download_pool slot until yt-dlp returns.

    `flat=False` means "one video's full metadata": a playlist or channel URL
    resolves to its FIRST video only. Without that cap a channel URL fully
    extracted every entry — minutes to hours of player-JS work for an answer
    that read one title. Returns {} on any failure, as before.
    """
    def _info():
        import yt_dlp
        if not flat:
            extra = dict(dl_opts.ONE_ITEM)
        else:
            extra = {"playlistend": max_entries} if max_entries else {}
        opts = _ydl_opts(url, extract_flat=flat, socket_timeout=15, **extra)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return info if flat else dl_opts.single_video_info(info)

    try:
        # yt-dlp can fail to return, so it never runs on the shared default
        # executor (see backend/core/executors.py).
        return await asyncio.wait_for(download_pool.run(_info), timeout=INFO_TIMEOUT_S) or {}
    except asyncio.TimeoutError:
        logger.error("Failed to get video info: timed out after %ss for %s", INFO_TIMEOUT_S, url[:80])
        return {}
    except Exception as e:
        logger.error(f"Failed to get video info: {e}")
        return {}


_YT_CHANNEL_TABS = {"videos", "shorts", "streams", "live", "playlists", "community", "about", "featured"}


def normalize_youtube_channel_url(url: str) -> str:
    """Append /videos to YouTube channel-root URLs so flat extraction returns
    the channel's videos instead of its TAB list (Videos / Shorts / Live as
    sub-playlists). No-op for non-YouTube URLs, tab-specific URLs, and
    individual videos.

    yt-dlp's flat extraction at a channel root returns `entries` that are the
    channel's tabs as playlist objects — useless for "give me N videos"
    callers, which then read a tab's URL as a video's.
    """
    if "youtube.com" not in url:
        return url
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts or parts[-1].lower() in _YT_CHANNEL_TABS:
        return url
    is_handle = parts[0].startswith("@") and len(parts) == 1
    is_legacy = len(parts) == 2 and parts[0] in ("c", "channel", "user")
    if is_handle or is_legacy:
        new_path = parsed.path.rstrip("/") + "/videos"
        return urlunparse(parsed._replace(path=new_path))
    return url


async def list_channel_videos(url: str, max_videos: int = 5,
                              raise_errors: bool = False) -> list[dict]:
    """
    Extract video list from a channel/playlist URL without downloading.
    Returns list of dicts with video metadata (url, title, duration, views, etc).

    Failures return [] (the long-standing contract for lookups) unless
    `raise_errors=True`, which raises a classified DownloadError instead —
    for callers that turn an empty listing into a user-facing failure and
    must be able to say WHY ("private channel", "rate-limited", a timeout)
    rather than "No videos found".
    """
    # Channel-root URLs return tab playlists (Videos / Shorts) under
    # `entries`, not the actual videos. Hit /videos directly.
    url = normalize_youtube_channel_url(url)

    def _list():
        import yt_dlp
        opts = _ydl_opts(
            url,
            extract_flat=True,           # Don't download, just list
            playlistend=max_videos,      # Limit number of videos
            socket_timeout=30,
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return []

        # Channel/playlist returns entries
        entries = info.get("entries", [])
        if not entries:
            # Single video URL — return as-is
            return [{
                "url": info.get("webpage_url") or info.get("url") or url,
                "video_id": info.get("id", ""),
                "title": info.get("title", ""),
                "duration": info.get("duration"),
                "view_count": info.get("view_count"),
                "upload_date": info.get("upload_date"),
            }]

        results = []
        for entry in entries:
            if not entry:
                continue
            video_url = entry.get("url") or entry.get("webpage_url", "")
            # Flat extraction often gives just video IDs — build full URL
            if video_url and not video_url.startswith("http"):
                video_url = f"https://www.youtube.com/watch?v={video_url}"
            results.append({
                "url": video_url,
                "video_id": entry.get("id", ""),
                "title": entry.get("title", ""),
                "duration": entry.get("duration"),
                "view_count": entry.get("view_count"),
                "upload_date": entry.get("upload_date"),
            })

        return results

    try:
        return await asyncio.wait_for(download_pool.run(_list), timeout=LISTING_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.error("Failed to list channel videos: timed out after %ss for %s",
                     LISTING_TIMEOUT_S, url[:80])
        if raise_errors:
            raise DownloadError(
                f"Listing the channel's videos timed out after {LISTING_TIMEOUT_S // 60} minutes."
            )
        return []
    except Exception as e:
        logger.error(f"Failed to list channel videos: {e}")
        if raise_errors:
            if isinstance(e, DownloadError):
                raise
            from backend.download.engine import terminal_exception
            raise terminal_exception(e, url) from e
        return []


def is_channel_or_playlist_url(url: str) -> bool:
    """Check if a URL points to a channel or playlist rather than a single video."""
    indicators = [
        "/@", "/channel/", "/c/", "/user/",  # YouTube channels
        "/playlist?", "&list=",               # YouTube playlists
        "/videos",                            # Channel videos tab
    ]
    # A watch link is the video, even when it also names a playlist
    # (`&list=`, a Music radio mix): the downloaders pass `noplaylist`, so
    # routing it to the channel path would only relabel a one-video download.
    if "watch?v=" in url or "youtu.be/" in url:
        return False
    return any(ind in url for ind in indicators)
