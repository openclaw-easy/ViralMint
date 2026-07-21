# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Lightweight in-process task runner.
All tasks run as asyncio background tasks in the same event loop.
No external dependencies required.
"""
import asyncio
import json
import logging

from backend.services.clip_options import ExtractOptions

logger = logging.getLogger(__name__)


def _detect_platform(url: str) -> str:
    """Extract platform name from any URL domain — fully generic, no hardcoded list."""
    from urllib.parse import urlparse
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return "unknown"
    # Strip www. and take the domain name (e.g. "bilibili.com" → "bilibili")
    host = host.lstrip("www.")
    parts = host.rsplit(".", 1)  # ["bilibili", "com"] or ["youtube", "co.uk"] etc
    if len(parts) >= 1:
        # Handle two-part TLDs like "co.uk", "com.br"
        domain = host.split(".")[0]
        # Map common shorteners/variants to canonical names
        aliases = {"youtu": "youtube", "m": "youtube"}  # youtu.be, m.youtube.com
        return aliases.get(domain, domain)
    return "unknown"


async def run_scout(job_id: str, niche: str, platforms: list[str], user_id: str = "local"):
    logger.info("TASK START scout | job=%s niche=%r platforms=%s", job_id[:8], niche, platforms)
    from backend.agents.scout import ScoutAgent
    try:
        await ScoutAgent().run(job_id=job_id, niche=niche, platforms=platforms, user_id=user_id)
        logger.info("TASK DONE  scout | job=%s", job_id[:8])
    except Exception as e:
        logger.error(f"TASK FAIL  scout | job={job_id[:8]}: {e}", exc_info=True)
        from backend.agents.job_helper import update_job_status
        await update_job_status(job_id, "failed", error_message=str(e))


async def run_download(job_id: str, scout_result_ids: list[str], user_id: str = "local"):
    logger.info("TASK START download | job=%s count=%d ids=%s", job_id[:8], len(scout_result_ids), [i[:8] for i in scout_result_ids])
    from backend.agents.downloader import DownloadAgent
    from backend.agents.analyzer import AnalyzerAgent
    try:
        await DownloadAgent().run(job_id=job_id, scout_result_ids=scout_result_ids, user_id=user_id)
        await AnalyzerAgent().run(job_id=job_id, user_id=user_id)
        logger.info("TASK DONE  download | job=%s", job_id[:8])
    except Exception as e:
        logger.error(f"TASK FAIL  download | job={job_id[:8]}: {e}", exc_info=True)
        from backend.agents.job_helper import update_job_status
        await update_job_status(job_id, "failed", error_message=str(e))


async def run_generate(
    job_id: str,
    downloaded_video_id: str,
    aspect_ratio: str = "9:16",
    user_id: str = "local",
    tts_provider: str = None,
    tts_voice: str = None,
    caption_style: str = None,
    caption_enabled: bool = None,
    music_enabled: bool = None,
    music_genre: str = None,
    custom_script: str = None,
    start_image: str = None,
    **_ignored,  # absorb deprecated params (gen_tier, video_model, etc.)
):
    logger.info("TASK START generate | job=%s video=%s tts=%s", job_id[:8], (downloaded_video_id or "none")[:8], tts_provider)
    from backend.agents.generator import GeneratorAgent
    try:
        await GeneratorAgent().run(
            job_id=job_id, downloaded_video_id=downloaded_video_id,
            aspect_ratio=aspect_ratio, user_id=user_id,
            tts_provider=tts_provider, tts_voice=tts_voice,
            caption_style=caption_style,
            caption_enabled=caption_enabled, music_enabled=music_enabled,
            music_genre=music_genre, custom_script=custom_script,
            start_image=start_image,
        )
        logger.info("TASK DONE  generate | job=%s", job_id[:8])
    except Exception as e:
        logger.error(f"TASK FAIL  generate | job={job_id[:8]}: {e}", exc_info=True)
        from backend.agents.job_helper import update_job_status
        await update_job_status(job_id, "failed", error_message=str(e))


async def run_batch_download_urls(job_id: str, urls: list[dict], user_id: str = "local"):
    """Download multiple videos from a list of URLs, then analyze all.
    urls: [{"url": "...", "title": "..."}, ...]"""
    logger.info("TASK START batch_download | job=%s count=%d", job_id[:8], len(urls))
    from backend.agents.job_helper import update_job_status
    from backend.core.ws_manager import ws_manager
    from backend.core.exceptions import RateLimitError

    from backend.core.http_utils import jittered_delay

    try:
        total = len(urls)
        downloaded_ids = []
        errors = []
        rate_limited = False

        for i, item in enumerate(urls):
            video_url = item.get("url", "")
            video_title = item.get("title", "")
            if not video_url:
                continue

            if rate_limited:
                errors.append(f"Video {i + 1}/{total} '{video_title[:40]}': Skipped (rate-limited)")
                continue

            step = f"Downloading video {i + 1}/{total}: {video_title[:50] or video_url[:50]}"
            base_pct = (i / total) * 70  # 0% to 70% for downloads
            await ws_manager.send_progress(job_id, base_pct, step, user_id)
            await update_job_status(job_id, "running", progress_pct=base_pct, current_step=step)

            # Inter-download delay with jitter (skip first)
            if i > 0:
                await asyncio.sleep(jittered_delay())

            try:
                dv_id = await _download_single_video_to_db(job_id, video_url, video_title, user_id)
                if dv_id:
                    downloaded_ids.append(dv_id)
            except RateLimitError as e:
                rate_limited = True
                errors.append(f"Video {i + 1}/{total} '{video_title[:40]}': {e}")
                logger.warning(f"Rate limited on video {i + 1}/{total}, skipping remaining: {e}")
                await ws_manager.send_constraint_warning(
                    constraint="rate_limit",
                    message=f"YouTube is rate-limiting downloads. {len(downloaded_ids)}/{total} downloaded so far. Try again later.",
                    severity="warning",
                    user_id=user_id,
                )
            except Exception as e:
                errors.append(f"Video {i + 1}/{total} '{video_title[:40]}': {e}")
                logger.error(f"Failed to download {video_url}: {e}", exc_info=True)

        error_summary = None
        if errors:
            error_summary = f"Failed {len(errors)}/{total} download(s):\n" + "\n".join(errors)

        if not downloaded_ids:
            user_msg = (
                "YouTube is rate-limiting downloads from this IP. Try again in 10-15 minutes."
                if rate_limited else
                error_summary or "All video downloads failed. They may be unavailable or region-blocked."
            )
            raise Exception(user_msg)

        # Analyze all downloaded videos
        await ws_manager.send_progress(job_id, 75, f"Analyzing {len(downloaded_ids)} videos...", user_id)
        await update_job_status(job_id, "running", progress_pct=75, current_step=f"Analyzing {len(downloaded_ids)} videos...")

        from backend.agents.analyzer import AnalyzerAgent
        await AnalyzerAgent().run(job_id=job_id, user_id=user_id)

        await update_job_status(
            job_id, "success",
            progress_pct=100,
            current_step=f"Downloaded and analyzed {len(downloaded_ids)}/{total} videos",
            output_data={"downloaded_ids": downloaded_ids, "total": total},
            error_message=error_summary if errors else None,
        )
        await ws_manager.send({
            "type": "job_complete",
            "job_id": job_id,
            "result": {"downloaded": len(downloaded_ids), "total": total},
        }, user_id)

    except Exception as e:
        logger.error(f"Batch download task failed: {e}", exc_info=True)
        from backend.agents.job_helper import update_job_status as _update
        await _update(job_id, "failed", error_message=str(e))
        from backend.core.ws_manager import ws_manager as _ws
        await _ws.send({"type": "job_failed", "job_id": job_id, "error": str(e)}, user_id)


async def run_download_url(job_id: str, url: str, title: str = "", user_id: str = "local"):
    """Download a video directly from a user-provided URL, then analyze it.
    Handles both single video URLs and channel/playlist URLs."""
    logger.info("TASK START download_url | job=%s url=%s", job_id[:8], url[:80])
    from backend.agents.job_helper import update_job_status
    from backend.services.ytdlp_service import is_channel_or_playlist_url, list_channel_videos
    from backend.core.ws_manager import ws_manager

    try:
        await update_job_status(job_id, "running", progress_pct=0, current_step="Checking URL...")
        await ws_manager.send_progress(job_id, 5, "Checking URL...", user_id)

        if is_channel_or_playlist_url(url):
            await _download_channel(job_id, url, user_id)
        else:
            await _download_single_url(job_id, url, title, user_id)

    except Exception as e:
        logger.error(f"URL download task failed: {e}", exc_info=True)
        from backend.agents.job_helper import update_job_status as _update
        await _update(job_id, "failed", error_message=str(e))
        from backend.core.ws_manager import ws_manager as _ws
        await _ws.send({
            "type": "job_failed",
            "job_id": job_id,
            "error": str(e),
        }, user_id)


async def _download_channel(job_id: str, url: str, user_id: str, max_videos: int = 5):
    """Download top N videos from a channel/playlist, then analyze each."""
    from backend.agents.job_helper import update_job_status
    from backend.services.ytdlp_service import list_channel_videos
    from backend.core.ws_manager import ws_manager
    from backend.core.exceptions import RateLimitError

    from backend.core.http_utils import jittered_delay

    await ws_manager.send_progress(job_id, 5, "Listing channel videos...", user_id)

    videos = await list_channel_videos(url, max_videos=max_videos)
    if not videos:
        raise Exception(f"No videos found at {url}")

    total = len(videos)
    await ws_manager.send_progress(job_id, 10, f"Found {total} videos — downloading...", user_id)

    downloaded_ids = []
    rate_limited = False
    for i, video in enumerate(videos):
        video_url = video.get("url", "")
        video_title = video.get("title", "")
        if not video_url:
            continue

        if rate_limited:
            continue

        step = f"Downloading video {i + 1}/{total}: {video_title[:50]}"
        base_pct = 10 + (i / total) * 60  # 10% to 70% for downloads
        await ws_manager.send_progress(job_id, base_pct, step, user_id)
        await update_job_status(job_id, "running", progress_pct=base_pct, current_step=step)

        # Inter-download delay with jitter (skip first)
        if i > 0:
            await asyncio.sleep(jittered_delay())

        try:
            dv_id = await _download_single_video_to_db(job_id, video_url, video_title, user_id)
            if dv_id:
                downloaded_ids.append(dv_id)
        except RateLimitError as e:
            rate_limited = True
            logger.warning(f"Rate limited on channel video {i + 1}/{total}, skipping remaining: {e}")
            await ws_manager.send_constraint_warning(
                constraint="rate_limit",
                message=f"YouTube is rate-limiting downloads. {len(downloaded_ids)}/{total} downloaded. Try again later.",
                severity="warning",
                user_id=user_id,
            )
        except Exception as e:
            logger.warning(f"Failed to download {video_url}: {e}")
            continue

    if not downloaded_ids:
        raise Exception(
            "YouTube is rate-limiting downloads from this IP. Try again in 10-15 minutes."
            if rate_limited else
            "All video downloads failed"
        )

    # Analyze all downloaded videos
    await ws_manager.send_progress(job_id, 75, f"Analyzing {len(downloaded_ids)} videos...", user_id)
    await update_job_status(job_id, "running", progress_pct=75, current_step=f"Analyzing {len(downloaded_ids)} videos...")

    from backend.agents.analyzer import AnalyzerAgent
    await AnalyzerAgent().run(job_id=job_id, user_id=user_id)

    await update_job_status(
        job_id, "success",
        progress_pct=100,
        current_step=f"Downloaded and analyzed {len(downloaded_ids)} videos",
        output_data={"downloaded_ids": downloaded_ids, "url": url, "total": total},
    )
    await ws_manager.send({
        "type": "job_complete",
        "job_id": job_id,
        "result": {"downloaded": len(downloaded_ids), "total": total, "url": url},
    }, user_id)


async def _download_single_url(job_id: str, url: str, title: str, user_id: str):
    """Download a single video URL, save to DB, and analyze."""
    from backend.agents.job_helper import update_job_status
    from backend.core.ws_manager import ws_manager

    await ws_manager.send_progress(job_id, 10, "Downloading video...", user_id)

    dv_id = await _download_single_video_to_db(job_id, url, title, user_id)

    await ws_manager.send_progress(job_id, 70, "Analyzing video...", user_id)

    from backend.agents.analyzer import AnalyzerAgent
    await AnalyzerAgent().run(job_id=job_id, user_id=user_id)

    await update_job_status(
        job_id, "success",
        progress_pct=100,
        current_step="Download and analysis complete",
        output_data={"downloaded_ids": [dv_id], "url": url},
    )
    await ws_manager.send({
        "type": "job_complete",
        "job_id": job_id,
        "result": {"downloaded": 1, "total": 1, "url": url},
    }, user_id)


async def _download_single_video_to_db(job_id: str, url: str, title: str, user_id: str) -> str:
    """Download one video, create DB records. Returns DownloadedVideo.id."""
    from backend.models.downloaded_video import DownloadedVideo
    from backend.models.scout_result import ScoutResult
    from backend.services.ytdlp_service import download_video
    from backend.database import AsyncSessionLocal
    from backend.config import settings
    from backend.agents.scout import compute_virality_score
    from uuid import uuid4
    from datetime import datetime

    video_id = str(uuid4())[:12]

    try:
        dl_result = await download_video(
            url=url,
            output_dir=settings.VIDEOS_DIR,
            filename=video_id,
            extract_audio=True,
        )
    except Exception as first_error:
        # AI-assisted retry: ask AI to fix the URL and try once more
        from backend.core.ai_retry import ai_fix_url
        from backend.models.user_settings import UserSettings
        from sqlalchemy import select

        logger.info(f"Download failed for {url}, attempting AI-assisted URL fix...")

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            user_settings = result.scalar_one_or_none()

        corrected_url = await ai_fix_url(url, str(first_error), user_settings)
        if not corrected_url:
            raise first_error  # AI couldn't help — raise original error

        logger.info(f"Retrying download with AI-corrected URL: {corrected_url}")
        dl_result = await download_video(
            url=corrected_url,
            output_dir=settings.VIDEOS_DIR,
            filename=video_id,
            extract_audio=True,
        )
        # If this also fails, the exception propagates naturally
        url = corrected_url  # Use corrected URL for DB records

    # Detect platform from URL domain — generic, no hardcoded list
    platform = _detect_platform(url)

    # Parse upload_date from yt-dlp "YYYYMMDD" format
    upload_date = None
    raw_date = dl_result.get("upload_date")
    if raw_date and len(raw_date) == 8:
        try:
            upload_date = datetime.strptime(raw_date, "%Y%m%d")
        except ValueError:
            pass

    views = dl_result.get("views", 0) or 0
    likes = dl_result.get("likes", 0) or 0
    comments = dl_result.get("comments", 0) or 0

    virality = compute_virality_score({
        "views": views, "likes": likes, "comments": comments,
        "upload_date": upload_date,
    })

    async with AsyncSessionLocal() as db:
        sr = ScoutResult(
            user_id=user_id,
            job_id=job_id,
            platform=platform,
            video_id=video_id,
            video_url=url,
            title=title or dl_result.get("title", "Direct download"),
            description=dl_result.get("description", "")[:500] if dl_result.get("description") else None,
            author=dl_result.get("uploader"),
            author_url=dl_result.get("uploader_url"),
            thumbnail_url=dl_result.get("thumbnail"),
            views=views,
            likes=likes,
            comments=comments,
            upload_date=upload_date,
            duration_seconds=dl_result.get("duration"),
            is_downloaded=True,
            virality_score=virality,
        )
        db.add(sr)
        await db.flush()

        # Store subtitle text as initial transcript if available
        subtitles = dl_result.get("subtitles")
        transcript = None
        transcript_language = None
        transcript_source = None
        if subtitles and isinstance(subtitles, dict) and subtitles.get("text"):
            transcript = subtitles["text"]
            transcript_language = subtitles.get("language")
            transcript_source = subtitles.get("source", "auto_subtitles")
            logger.info(f"Stored {transcript_source} transcript for {video_id} ({transcript_language})")

        # Store chapters and tags as JSON
        chapters = dl_result.get("chapters")
        tags = dl_result.get("tags")
        category = dl_result.get("category")

        dv = DownloadedVideo(
            user_id=user_id,
            scout_result_id=sr.id,
            title=sr.title,
            platform=sr.platform,
            video_path=dl_result.get("video_path"),
            audio_path=dl_result.get("audio_path"),
            duration_seconds=dl_result.get("duration"),
            file_size_mb=dl_result.get("file_size_mb"),
            transcript=transcript,
            transcript_language=transcript_language,
            transcript_source=transcript_source,
            chapters_json=json.dumps(chapters) if chapters else None,
            tags_json=json.dumps(tags) if tags else None,
            category=category,
        )
        db.add(dv)
        await db.commit()
        await db.refresh(dv)
        return dv.id


async def run_analyze_imported(job_id: str, downloaded_video_id: str, user_id: str = "local"):
    """Analyze a user-imported local video file (transcribe + extract insights)."""
    from backend.agents.job_helper import update_job_status
    from backend.agents.analyzer import AnalyzerAgent
    from backend.core.ws_manager import ws_manager
    from backend.database import AsyncSessionLocal
    from backend.models.downloaded_video import DownloadedVideo
    from backend.config import settings
    from sqlalchemy import select
    from pathlib import Path
    import subprocess

    try:
        await update_job_status(job_id, "running", progress_pct=0, current_step="Preparing imported video...")
        await ws_manager.send_progress(job_id, 10, "Preparing imported video...", user_id)

        # If we have video but no audio, extract audio for better transcription
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DownloadedVideo).where(DownloadedVideo.id == downloaded_video_id)
            )
            dv = result.scalar_one_or_none()

        if not dv:
            raise Exception(f"Downloaded video {downloaded_video_id} not found")

        if dv.video_path and not dv.audio_path:
            video_path = Path(dv.video_path)
            if video_path.exists():
                audio_dir = settings.AUDIO_DIR
                audio_dir.mkdir(parents=True, exist_ok=True)
                audio_path = audio_dir / f"{video_path.stem}_audio.mp3"

                await ws_manager.send_progress(job_id, 20, "Extracting audio...", user_id)
                proc = await asyncio.to_thread(
                    subprocess.run,
                    ["ffmpeg", "-i", str(video_path), "-vn", "-acodec", "libmp3lame",
                     "-q:a", "2", "-y", str(audio_path)],
                    capture_output=True, timeout=300,
                )
                if proc.returncode == 0 and audio_path.exists():
                    async with AsyncSessionLocal() as db:
                        result = await db.execute(
                            select(DownloadedVideo).where(DownloadedVideo.id == downloaded_video_id)
                        )
                        row = result.scalar_one_or_none()
                        if row:
                            row.audio_path = str(audio_path)
                            await db.commit()

        # Get video duration via ffprobe if not set
        if dv.video_path and not dv.duration_seconds:
            try:
                from backend.services.video_utils import probe_duration
                probe_path = dv.video_path if dv.video_path else dv.audio_path
                if probe_path:
                    dur = await asyncio.to_thread(probe_duration, probe_path, 0)
                    if dur > 0:
                        duration = int(dur)
                        async with AsyncSessionLocal() as db:
                            result = await db.execute(
                                select(DownloadedVideo).where(DownloadedVideo.id == downloaded_video_id)
                            )
                            row = result.scalar_one_or_none()
                            if row:
                                row.duration_seconds = duration
                                await db.commit()
            except Exception as e:
                logger.warning(f"Could not get duration: {e}")

        await ws_manager.send_progress(job_id, 40, "Transcribing...", user_id)

        # Run analyzer (transcription + AI insights)
        await AnalyzerAgent().run(job_id=job_id, user_id=user_id)

        await update_job_status(
            job_id, "success",
            progress_pct=100,
            current_step="Import and analysis complete",
            output_data={"downloaded_video_id": downloaded_video_id},
        )
        await ws_manager.send({
            "type": "job_complete",
            "job_id": job_id,
            "result": {"downloaded_video_id": downloaded_video_id},
        }, user_id)

    except Exception as e:
        logger.error(f"Import analysis failed: {e}", exc_info=True)
        from backend.agents.job_helper import update_job_status as _update
        await _update(job_id, "failed", error_message=str(e))
        await ws_manager.send({
            "type": "job_failed",
            "job_id": job_id,
            "error": str(e),
        }, user_id)


async def run_analyze_channel(job_id: str, url: str, user_id: str = "local"):
    """Channel analysis — fetch metadata + video list + AI strategic analysis."""
    from backend.agents.job_helper import update_job_status
    from backend.services.ytdlp_service import get_video_info
    from backend.core.ws_manager import ws_manager

    try:
        await update_job_status(job_id, "running", progress_pct=0, current_step="Fetching channel info...")
        await ws_manager.send_progress(job_id, 10, "Fetching channel info...", user_id)

        is_tiktok = "tiktok.com" in url

        if is_tiktok:
            summary = await _analyze_tiktok_channel(url)
        else:
            summary = await _analyze_youtube_channel(url)

        if not summary:
            raise Exception(f"Could not fetch channel info from {url}")

        await ws_manager.send_progress(job_id, 60, "Generating AI analysis...", user_id)

        # Generate AI strategic analysis
        ai_analysis = await _generate_channel_ai_analysis(summary, user_id)
        summary["ai_analysis"] = ai_analysis

        await update_job_status(
            job_id, "success",
            progress_pct=100,
            current_step="Channel analysis ready",
            output_data=summary,
        )

        # Send rich channel summary to chat
        await ws_manager.send({
            "type": "channel_analysis",
            "job_id": job_id,
            "summary": summary,
        }, user_id)

        await ws_manager.send({
            "type": "job_complete",
            "job_id": job_id,
            "result": {"channel_title": summary.get("channel_title", ""), "video_count": len(summary.get("videos", []))},
        }, user_id)

    except Exception as e:
        logger.error(f"Channel analysis failed: {e}", exc_info=True)
        from backend.agents.job_helper import update_job_status as _update
        await _update(job_id, "failed", error_message=str(e))
        await ws_manager.send({
            "type": "job_failed",
            "job_id": job_id,
            "error": str(e),
        }, user_id)


async def _analyze_tiktok_channel(url: str) -> dict:
    """Fetch TikTok channel data with rich engagement metrics."""
    from backend.services.channel_reader import get_tiktok_channel

    result = await get_tiktok_channel(url, max_videos=30)
    user_info = result.get("user") or {}
    videos = result.get("videos") or []

    video_list = []
    for i, v in enumerate(videos, 1):
        # TikTok titles are often just hashtags — use "Video #N" as prefix
        title = v.get("title", "").strip()
        if not title or title.startswith("#"):
            title = f"Video #{i}" + (f" — {title[:60]}" if title else "")
        # Format Unix timestamp to readable date
        upload_date = v.get("created_at")
        if upload_date and isinstance(upload_date, (int, float)) and upload_date > 1_000_000_000:
            from datetime import datetime
            try:
                upload_date = datetime.utcfromtimestamp(upload_date).strftime("%Y-%m-%d")
            except Exception:
                upload_date = None
        video_list.append({
            "url": v.get("url", ""),
            "video_id": v.get("video_id", ""),
            "title": title,
            "duration": v.get("duration"),
            "view_count": v.get("view_count", 0),
            "like_count": v.get("like_count", 0),
            "comment_count": v.get("comment_count", 0),
            "share_count": v.get("share_count", 0),
            "upload_date": upload_date,
        })

    return {
        "platform": "tiktok",
        "channel_title": user_info.get("display_name", "Unknown"),
        "channel_description": "",
        "channel_url": url,
        "subscriber_count": user_info.get("follower_count", 0),
        "thumbnail": user_info.get("avatar_url", ""),
        "total_video_count": user_info.get("video_count", len(videos)),
        "total_videos_listed": len(video_list),
        "videos": video_list,
    }


async def _analyze_youtube_channel(url: str) -> dict:
    """Fetch YouTube channel data via yt-dlp flat extraction."""
    from backend.services.ytdlp_service import get_video_info

    channel_info = await get_video_info(url, flat=True)
    if not channel_info:
        return None

    channel_title = channel_info.get("channel", "") or channel_info.get("uploader", "") or channel_info.get("title", "Unknown")
    channel_desc = channel_info.get("description", "")
    subscriber_count = channel_info.get("channel_follower_count") or channel_info.get("subscriber_count")
    channel_url = channel_info.get("channel_url") or channel_info.get("uploader_url") or url
    thumbnails = channel_info.get("thumbnails") or []
    thumbnail = thumbnails[0].get("url") if thumbnails else None

    entries = channel_info.get("entries") or []
    video_list = []
    for entry in entries[:30]:
        if not entry:
            continue
        vid_url = entry.get("url") or entry.get("webpage_url", "")
        if vid_url and not vid_url.startswith("http"):
            vid_url = f"https://www.youtube.com/watch?v={vid_url}"
        video_list.append({
            "url": vid_url,
            "video_id": entry.get("id", ""),
            "title": entry.get("title", ""),
            "duration": entry.get("duration"),
            "view_count": entry.get("view_count"),
            "like_count": entry.get("like_count"),
            "comment_count": entry.get("comment_count"),
            "upload_date": entry.get("upload_date"),
        })

    return {
        "platform": "youtube",
        "channel_title": channel_title,
        "channel_description": channel_desc[:500] if channel_desc else "",
        "channel_url": channel_url,
        "subscriber_count": subscriber_count,
        "thumbnail": thumbnail,
        "total_videos_listed": len(video_list),
        "videos": video_list,
    }


CHANNEL_ANALYSIS_PROMPT = """You are a professional social media strategist and content analyst. Analyze this channel's data and provide a strategic breakdown.

Channel: {channel_title}
Platform: {platform}
Followers/Subscribers: {subscriber_count}
Total videos listed: {total_videos_listed}
Description: {channel_description}

Video data (most recent first):
{video_data}

Provide your analysis in this exact markdown format:

## Channel Overview
A 2-3 sentence summary of who this channel is and what they do.

## Key Metrics
| Metric | Value |
|--------|-------|
(Include: total views, avg views/video, engagement rate, top-performing video, posting frequency. Calculate from the data.)

## What's Working
2-3 bullet points about what this channel does well, based on their top-performing content.

## Warning Signs
2-3 bullet points about issues or risks you see in the data (e.g. declining views, low engagement, inconsistent posting, over-reliance on one format).

## Actionable Recommendations
3 specific, tactical recommendations for someone who wants to compete with or learn from this channel. Be concrete — mention specific content ideas or strategies.

Keep it concise and data-driven. Reference specific numbers from the video data. Do not add disclaimers or filler."""


async def _generate_channel_ai_analysis(summary: dict, user_id: str) -> str:
    """Call AI to generate strategic analysis of the channel data."""
    try:
        from backend.core.ai_provider import get_ai_client
        from backend.database import AsyncSessionLocal
        from backend.models.user_settings import UserSettings
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            user_settings = result.scalar_one_or_none()

        ai = get_ai_client(user_settings)

        # Build video data summary for the prompt
        videos = summary.get("videos", [])
        video_lines = []
        for i, v in enumerate(videos[:20], 1):
            parts = [f"{i}. \"{v.get('title', 'Untitled')[:80]}\""]
            if v.get("view_count") is not None:
                parts.append(f"views={v['view_count']:,}")
            if v.get("like_count"):
                parts.append(f"likes={v['like_count']:,}")
            if v.get("comment_count"):
                parts.append(f"comments={v['comment_count']:,}")
            if v.get("share_count"):
                parts.append(f"shares={v['share_count']:,}")
            if v.get("duration"):
                parts.append(f"duration={v['duration']}s")
            if v.get("upload_date"):
                parts.append(f"date={v['upload_date']}")
            video_lines.append(" | ".join(parts))

        prompt = CHANNEL_ANALYSIS_PROMPT.format(
            channel_title=summary.get("channel_title", "Unknown"),
            platform=summary.get("platform", "unknown"),
            subscriber_count=f"{summary.get('subscriber_count', 0):,}" if summary.get("subscriber_count") else "Unknown",
            total_videos_listed=summary.get("total_videos_listed", 0),
            channel_description=summary.get("channel_description", "None provided"),
            video_data="\n".join(video_lines) or "No video data available",
        )

        analysis = await ai.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
        )
        return analysis.strip()

    except Exception as e:
        logger.warning(f"AI channel analysis failed (non-fatal): {e}")
        return ""


async def run_extract_clips(
    job_id: str,
    downloaded_video_id: str,
    opts: ExtractOptions,
    user_id: str = "local",
):
    """Extract clips from a downloaded video — AI viral picker or manual ranges.

    All run knobs come via `opts` (ExtractOptions). `opts.mode` is "ai"
    (default — AI viral-clip picker) or "manual" (cut user-supplied
    `opts.time_ranges` verbatim). The API endpoint validates `time_ranges`
    before dispatching so the runner just forwards `opts` to
    `extract_viral_clips`.
    """
    max_clips = opts.max_clips
    mode = opts.mode
    logger.info(
        "TASK START extract_clips | job=%s video=%s mode=%s max=%d",
        job_id[:8], downloaded_video_id[:8], mode, max_clips,
    )
    from backend.agents.job_helper import update_job_status
    from backend.core.ws_manager import ws_manager
    from backend.database import AsyncSessionLocal
    from backend.models.downloaded_video import DownloadedVideo
    from backend.models.generated_video import GeneratedVideo
    from backend.models.user_settings import UserSettings
    from backend.services.clip_extractor import extract_viral_clips
    from sqlalchemy import select
    from pathlib import Path

    try:
        await update_job_status(job_id, "running", progress_pct=0, current_step="Loading video...")

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DownloadedVideo).where(DownloadedVideo.id == downloaded_video_id)
            )
            video = result.scalar_one_or_none()
            if not video:
                raise ValueError(f"Downloaded video {downloaded_video_id} not found")

            result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            user_settings = result.scalar_one_or_none()

        # Run the clip extraction pipeline. `opts.mode` + `opts.time_ranges`
        # branch the service-layer logic: "ai" runs the viral picker,
        # "manual" cuts the user-supplied ranges verbatim.
        clips = await extract_viral_clips(
            video=video,
            user_settings=user_settings,
            opts=opts,
            job_id=job_id,
            user_id=user_id,
        )

        if not clips:
            raise ValueError("No clips were extracted")

        # Source video title — used as the prefix for per-clip names so users
        # (and LLMs) can tell at a glance which source a clip came from.
        # Truncated to ~30 chars to keep the per-clip title short.
        source_title = (video.title if video and video.title else "Clip").strip()
        if len(source_title) > 30:
            source_title = source_title[:28].rstrip() + "…"

        def _clip_title(c: dict, idx: int) -> str:
            """Build a per-clip descriptive title.

            All clips from the same source used to inherit the source's title
            verbatim — five clips of "Tesla Q4 earnings call" all named the
            same, useless when browsing or when an LLM is picking one via the
            /mcp list_clips query.

            Format priorities:
              1. youtube_title  — when AI metadata succeeded, this is already
                 a per-clip catchy title; prefer it.
              2. {source} — {hook_type}: {reason} — derived from the
                 viral-clip picker's structured fields.
              3. {source} — clip {n} — fallback when neither AI metadata nor
                 a reason landed.
            """
            yt = (c.get("youtube_title") or "").strip()
            if yt:
                return yt[:100]
            hook = (c.get("hook_type") or "").strip()
            reason = (c.get("reason") or "").strip()
            if reason:
                if len(reason) > 50:
                    cut = reason[:48].rsplit(" ", 1)[0]
                    reason = cut.rstrip(",.;:") + "…"
                if hook:
                    return f"{source_title} — {hook}: {reason}"
                return f"{source_title} — {reason}"
            return f"{source_title} — clip {idx + 1}"

        # Save each clip as a GeneratedVideo record with all new fields
        clip_ids = []
        async with AsyncSessionLocal() as db:
            for idx, clip in enumerate(clips):
                gv = GeneratedVideo(
                    user_id=user_id,
                    source_downloaded_video_id=downloaded_video_id,
                    title=_clip_title(clip, idx),
                    video_path=str(clip["video_path"]) if clip.get("video_path") else None,
                    thumbnail_path=str(clip["thumbnail_path"]) if clip.get("thumbnail_path") else None,
                    aspect_ratio="9:16",
                    duration_seconds=clip.get("duration_seconds"),
                    gen_tier="clip_extraction",
                    source_type="clip_extraction",
                    youtube_title=clip.get("youtube_title"),
                    youtube_description=clip.get("youtube_description"),
                    youtube_tags_json=json.dumps(clip.get("youtube_tags", [])),
                    tiktok_title=clip.get("tiktok_title"),
                    status="ready",
                    # New clip-specific fields
                    clip_start_seconds=clip.get("start"),
                    clip_end_seconds=clip.get("end"),
                    clip_virality_score=clip.get("virality_score"),
                    clip_hook_score=clip.get("hook_score"),
                    clip_hook_type=clip.get("hook_type"),
                    clip_virality_reason=clip.get("reason"),
                    # score_breakdown is the dict produced by _select_clip_windows
                    # with the 4 sub-scores (flow / value / trend / shareability).
                    # Serialize only when non-empty so legacy rows / fallback paths
                    # that don't compute it stay NULL.
                    clip_score_breakdown_json=(
                        json.dumps(clip["score_breakdown"])
                        if clip.get("score_breakdown") else None
                    ),
                    caption_status=clip.get("caption_status"),
                    metadata_status=clip.get("metadata_status"),
                    script=clip.get("transcript_text"),  # Store clip transcript as script
                )
                db.add(gv)
                await db.flush()
                clip_ids.append(gv.id)
            await db.commit()

        await update_job_status(
            job_id, "success",
            progress_pct=100,
            current_step=f"Extracted {len(clips)} clips",
            output_data={"clip_ids": clip_ids, "count": len(clips)},
        )
        await ws_manager.send({
            "type": "job_complete",
            "job_id": job_id,
            "result": {"clip_ids": clip_ids, "count": len(clips)},
        }, user_id)
        logger.info("TASK DONE  extract_clips | job=%s clips=%d", job_id[:8], len(clips))

    except Exception as e:
        logger.error(f"TASK FAIL  extract_clips | job={job_id[:8]}: {e}", exc_info=True)
        from backend.agents.job_helper import update_job_status as _update
        await _update(job_id, "failed", error_message=str(e))
        await ws_manager.send({
            "type": "job_failed",
            "job_id": job_id,
            "error": str(e),
        }, user_id)


async def run_reanalyze(job_id: str, video_id: str, whisper_quality: str = "balanced", user_id: str = "local"):
    logger.info("TASK START reanalyze | job=%s video=%s quality=%s", job_id[:8], video_id[:8], whisper_quality)
    from backend.agents.analyzer import AnalyzerAgent
    try:
        await AnalyzerAgent().reanalyze_single(
            job_id=job_id, video_id=video_id,
            whisper_quality=whisper_quality, user_id=user_id,
        )
        logger.info("TASK DONE  reanalyze | job=%s", job_id[:8])
    except Exception as e:
        logger.error(f"TASK FAIL  reanalyze | job={job_id[:8]}: {e}", exc_info=True)
        from backend.agents.job_helper import update_job_status
        await update_job_status(job_id, "failed", error_message=str(e))


async def run_upload(
    job_id: str,
    generated_video_id: str,
    platforms: list[str],
    user_id: str = "local",
):
    logger.info("TASK START upload | job=%s video=%s platforms=%s", job_id[:8], generated_video_id[:8], platforms)
    from backend.agents.uploader import UploadAgent
    try:
        await UploadAgent().run(
            job_id=job_id, generated_video_id=generated_video_id,
            platforms=platforms, user_id=user_id,
        )
        logger.info("TASK DONE  upload | job=%s", job_id[:8])
    except Exception as e:
        logger.error(f"TASK FAIL  upload | job={job_id[:8]}: {e}", exc_info=True)
        from backend.agents.job_helper import update_job_status
        await update_job_status(job_id, "failed", error_message=str(e))


_task_semaphore = asyncio.Semaphore(3)  # max 3 concurrent heavy tasks


async def _run_with_limit(coro):
    """Run a coroutine with concurrency limiting."""
    try:
        async with _task_semaphore:
            await coro
    except Exception as e:
        # Last-resort catch — individual task runners should handle their own errors,
        # but if something leaks through, log it instead of crashing silently.
        logger.error("Unhandled exception in background task: %s", e, exc_info=True)


async def run_news_scout(
    job_id: str,
    query: str,
    expanded_queries: list[str] = None,
    sources: list[str] = None,
    direct_url: str = None,
    user_id: str = "local",
):
    logger.info("TASK START news_scout | job=%s query=%r", job_id[:8], query)
    from backend.agents.news_scout import NewsScoutAgent
    try:
        await NewsScoutAgent().run(
            job_id=job_id, query=query, expanded_queries=expanded_queries,
            sources=sources, direct_url=direct_url, user_id=user_id,
        )
        logger.info("TASK DONE  news_scout | job=%s", job_id[:8])
    except Exception as e:
        logger.error("TASK FAIL  news_scout | job=%s: %s", job_id[:8], e, exc_info=True)
        from backend.agents.job_helper import update_job_status
        await update_job_status(job_id, "failed", error_message=str(e))


async def run_news_save(
    job_id: str,
    article_ids: list[str],
    user_id: str = "local",
):
    """Save selected news scout results to downloaded_videos (Library)."""
    logger.info("TASK START news_save | job=%s count=%d", job_id[:8], len(article_ids))
    from backend.agents.job_helper import update_job_status
    from backend.database import AsyncSessionLocal
    from backend.models.scout_result import ScoutResult
    from backend.models.downloaded_video import DownloadedVideo
    from backend.core.ws_manager import ws_manager
    from sqlalchemy import select

    try:
        await update_job_status(job_id, "running", progress_pct=0, current_step="Saving articles to Library...")

        saved_ids = []
        async with AsyncSessionLocal() as db:
            for i, article_id in enumerate(article_ids):
                result = await db.execute(
                    select(ScoutResult).where(ScoutResult.id == article_id)
                )
                sr = result.scalar_one_or_none()
                if not sr:
                    continue

                # Parse the analysis from description
                analysis = {}
                try:
                    analysis = json.loads(sr.description or "{}")
                except json.JSONDecodeError:
                    pass

                full_text = analysis.pop("full_text_preview", "")

                dv = DownloadedVideo(
                    user_id=user_id,
                    scout_result_id=sr.id,
                    title=sr.title or "Untitled Article",
                    platform="news",
                    transcript=full_text or sr.title,
                    insights_json=json.dumps({
                        "source_url": sr.video_url,
                        "source_domain": sr.author,
                        "published_at": sr.upload_date.isoformat() if sr.upload_date else None,
                        **analysis,
                    }, ensure_ascii=False),
                    video_path=None,
                    audio_path=None,
                    thumbnail_path=sr.thumbnail_url,
                )
                db.add(dv)
                await db.flush()
                saved_ids.append(dv.id)

                pct = ((i + 1) / len(article_ids)) * 100
                await update_job_status(job_id, "running", progress_pct=pct,
                                        current_step=f"Saved {i + 1}/{len(article_ids)}")

            await db.commit()

        await update_job_status(job_id, "success", progress_pct=100,
                                current_step=f"Saved {len(saved_ids)} articles",
                                output_data={"downloaded_ids": saved_ids})

        await ws_manager.send({
            "type": "news_saved",
            "count": len(saved_ids),
            "downloaded_ids": saved_ids,
            "message": f"{len(saved_ids)} article{'s' if len(saved_ids) != 1 else ''} saved to Library — ready for video generation",
        }, user_id)

        logger.info("TASK DONE  news_save | job=%s saved=%d", job_id[:8], len(saved_ids))
    except Exception as e:
        logger.error("TASK FAIL  news_save | job=%s: %s", job_id[:8], e, exc_info=True)
        from backend.agents.job_helper import update_job_status
        await update_job_status(job_id, "failed", error_message=str(e))


def dispatch(coro):
    """Fire-and-forget an async task with concurrency limiting (max 3 concurrent)."""
    logger.debug("Dispatching async task: %s", coro.__qualname__ if hasattr(coro, '__qualname__') else type(coro).__name__)
    asyncio.create_task(_run_with_limit(coro))


async def run_batch_generate(
    job_id: str,
    items: list[dict],
    shared_settings: dict,
    user_id: str = "local",
):
    """
    Orchestrated batch video generation — runs items SEQUENTIALLY within a
    single parent job.  Each item gets its own child job for individual tracking.

    Advantages over firing N parallel dispatch() calls:
    - Sequential execution avoids resource thrashing (Whisper, FFmpeg)
    - Single parent job with aggregated progress (0-100% across all items)
    - Partial failure: if item 2/5 fails, items 3-5 still run
    - WS messages report per-item + overall progress
    """
    from backend.agents.job_helper import create_job, update_job_status
    from backend.agents.generator import GeneratorAgent
    from backend.core.ws_manager import ws_manager

    total = len(items)
    logger.info("TASK START batch_generate | job=%s count=%d", job_id[:8], total)

    await update_job_status(
        job_id, "running", progress_pct=0,
        current_step=f"Starting batch generation (0/{total})...",
    )

    succeeded = []
    failed = []

    for idx, item in enumerate(items):
        vid_id = item["downloaded_video_id"]
        merged = {**shared_settings, **{k: v for k, v in item.items() if k != "downloaded_video_id" and v is not None}}

        # Create child job for individual tracking
        child_job = await create_job("generate", user_id, {"downloaded_video_id": vid_id, "batch_parent": job_id, **merged})

        # Report per-item start
        base_pct = (idx / total) * 100
        step = f"Generating video {idx + 1}/{total}..."
        await update_job_status(job_id, "running", progress_pct=base_pct, current_step=step)
        await ws_manager.send({
            "type": "batch_item_start",
            "parent_job_id": job_id,
            "child_job_id": child_job.id,
            "index": idx,
            "total": total,
            "downloaded_video_id": vid_id,
        }, user_id)

        try:
            await GeneratorAgent().run(
                job_id=child_job.id,
                downloaded_video_id=vid_id,
                user_id=user_id,
                aspect_ratio=merged.get("aspect_ratio", "9:16"),
                tts_provider=merged.get("tts_provider"),
                tts_voice=merged.get("tts_voice"),
                caption_style=merged.get("caption_style"),
                caption_enabled=merged.get("caption_enabled"),
                music_enabled=merged.get("music_enabled"),
                music_genre=merged.get("music_genre"),
                custom_script=merged.get("custom_script"),
                start_image=merged.get("start_image"),
            )
            succeeded.append({"index": idx, "child_job_id": child_job.id, "video_id": vid_id})
            logger.info("BATCH item %d/%d succeeded | child=%s", idx + 1, total, child_job.id[:8])
        except Exception as e:
            failed.append({"index": idx, "child_job_id": child_job.id, "video_id": vid_id, "error": str(e)})
            logger.error("BATCH item %d/%d failed | child=%s: %s", idx + 1, total, child_job.id[:8], e)
            await update_job_status(child_job.id, "failed", error_message=str(e))

        # Report per-item completion
        done_pct = ((idx + 1) / total) * 100
        await ws_manager.send({
            "type": "batch_item_done",
            "parent_job_id": job_id,
            "child_job_id": child_job.id,
            "index": idx,
            "total": total,
            "success": idx not in [f["index"] for f in failed],
        }, user_id)
        await update_job_status(job_id, "running", progress_pct=done_pct,
                                current_step=f"Completed {idx + 1}/{total} ({len(succeeded)} ok, {len(failed)} failed)")

    # Final status
    if failed and not succeeded:
        error_msgs = "; ".join(f"Item {f['index']+1}: {f['error'][:100]}" for f in failed)
        await update_job_status(job_id, "failed", progress_pct=100,
                                current_step=f"All {total} videos failed",
                                error_message=error_msgs,
                                output_data={"succeeded": succeeded, "failed": failed})
        await ws_manager.send({"type": "job_failed", "job_id": job_id, "error": error_msgs}, user_id)
    else:
        step = f"Generated {len(succeeded)}/{total} videos" + (f" ({len(failed)} failed)" if failed else "")
        error_msg = "; ".join(f"Item {f['index']+1}: {f['error'][:100]}" for f in failed) if failed else None
        await update_job_status(job_id, "success", progress_pct=100, current_step=step,
                                error_message=error_msg,
                                output_data={"succeeded": succeeded, "failed": failed})
        await ws_manager.send({
            "type": "job_complete", "job_id": job_id,
            "result": {"succeeded": len(succeeded), "failed": len(failed), "total": total},
        }, user_id)

    logger.info("TASK DONE  batch_generate | job=%s succeeded=%d failed=%d", job_id[:8], len(succeeded), len(failed))
