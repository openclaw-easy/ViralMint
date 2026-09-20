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
    user_images: list[str] = None,
    visual_style: str = None,
    transition_style: str = None,
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
            start_image=start_image, user_images=user_images,
            visual_style=visual_style, transition_style=transition_style,
        )
        logger.info("TASK DONE  generate | job=%s", job_id[:8])
    except Exception as e:
        logger.error(f"TASK FAIL  generate | job={job_id[:8]}: {e}", exc_info=True)
        from backend.agents.job_helper import update_job_status
        await update_job_status(job_id, "failed", error_message=str(e))


async def run_batch_download_urls(job_id: str, urls: list[dict], user_id: str = "local",
                                  options: dict | None = None, analyze: bool = True):
    """Download multiple videos from a list of URLs, then analyze all.
    urls: [{"url": "...", "title": "..."}, ...]

    `options`: shared per-download yt-dlp options (see
    backend/services/download_options.py) applied to EVERY url in the batch.
    None → the pre-existing default path for every caller that predates it.

    `analyze`: run Whisper + insight extraction over everything downloaded.
    True for every pre-existing caller (channel analysis and the chat planner
    both want the transcripts). The Video Download tool page passes False: it
    was asked for the files, and transcribing a 40-minute video the user only
    wanted on disk is minutes of work nobody asked for — Library can analyze
    on demand."""
    logger.info("TASK START batch_download | job=%s count=%d", job_id[:8], len(urls))
    from backend.agents.job_helper import update_job_status
    from backend.core.ws_manager import ws_manager
    from backend.core.exceptions import RateLimitError

    from backend.core.http_utils import jittered_delay

    try:
        total = len(urls)
        downloaded_ids = []
        # Per-video delivery summaries ({id, title, height, requested_quality,
        # ext, subtitle_files, extras_dropped}) — persisted in the job's
        # output_data so the Video Download page can report what was DELIVERED
        # vs what the options asked for.
        video_summaries = []
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
                dl = await _download_single_video_to_db(
                    job_id, video_url, video_title, user_id, options=options)
                if dl:
                    downloaded_ids.append(dl["id"])
                    video_summaries.append(dl)
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

        if analyze:
            # Analyze all downloaded videos
            await ws_manager.send_progress(job_id, 75, f"Analyzing {len(downloaded_ids)} videos...", user_id)
            await update_job_status(job_id, "running", progress_pct=75, current_step=f"Analyzing {len(downloaded_ids)} videos...")

            from backend.agents.analyzer import AnalyzerAgent
            await AnalyzerAgent().run(job_id=job_id, user_id=user_id)

        await update_job_status(
            job_id, "success",
            progress_pct=100,
            current_step=(
                f"Downloaded and analyzed {len(downloaded_ids)}/{total} videos"
                if analyze else
                f"Downloaded {len(downloaded_ids)}/{total} videos — open Library to analyze"
            ),
            # `videos` carries what each download DELIVERED (height/ext/
            # subtitle sidecars) so the Video Download page can show "asked for
            # 4K, this source had 1080p" instead of implying every option was
            # honoured.
            output_data={"downloaded_ids": downloaded_ids, "total": total,
                         "videos": video_summaries},
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
            dl = await _download_single_video_to_db(job_id, video_url, video_title, user_id)
            if dl:
                downloaded_ids.append(dl["id"])
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

    dv_id = (await _download_single_video_to_db(job_id, url, title, user_id))["id"]

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


async def _download_single_video_to_db(job_id: str, url: str, title: str, user_id: str,
                                       options: dict | None = None) -> dict:
    """Download one video, create DB records.

    Returns a delivery summary:
        {"id": DownloadedVideo.id, "title": str,
         "height": int|None, "requested_quality": str|None, "ext": str|None,
         "subtitle_files": [str], "extras_dropped": bool}

    `id` is what chaining callers need; the rest is what the user actually GOT
    vs what the per-download `options` asked for (a quality cap degrades to
    the closest available height, and a container request degrades to mkv when
    codecs demand it; `ext` always comes from the file actually written).
    Callers that surface results to a human or an LLM should pass the summary
    through so "asked for 4K" is never silently presented as "got 4K".

    `options`: see backend/services/download_options.py. None → the
    pre-existing default path, byte-identical for every caller that predates
    this parameter.
    """
    from backend.models.downloaded_video import DownloadedVideo
    from backend.models.scout_result import ScoutResult
    from backend.services.ytdlp_service import download_video
    from backend.database import AsyncSessionLocal
    from backend.config import settings
    from backend.agents.scout import compute_virality_score
    from uuid import uuid4
    from datetime import datetime
    from pathlib import Path

    video_id = str(uuid4())[:12]

    try:
        dl_result = await download_video(
            url=url,
            output_dir=settings.VIDEOS_DIR,
            filename=video_id,
            extract_audio=True,
            # None for every pre-existing caller → byte-identical download.
            options=options,
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
            options=options,
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
        return {
            "id": dv.id,
            "title": dv.title,
            "height": dl_result.get("height"),
            "requested_quality": dl_result.get("requested_quality"),
            "ext": (Path(dl_result["video_path"]).suffix.lstrip(".").lower() or None)
                   if dl_result.get("video_path") else None,
            # Basenames, not full paths — enough for the UI to name the sidecar
            # ("test_vid.en.srt") without spilling filesystem layout into cards.
            "subtitle_files": [Path(p).name for p in dl_result.get("kept_subtitle_paths") or []],
            "extras_dropped": bool(dl_result.get("option_postprocessors_dropped")),
        }


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


async def run_suggest_clips(
    job_id: str,
    downloaded_video_id: str,
    opts: ExtractOptions,
    user_id: str = "local",
):
    """Find viral moments and RETURN them — cut nothing.

    The plan half of AI picks, for Clipper's cutting bench: the same
    transcript load and the same `_select_clip_windows_with_retries` the
    extract job runs, stopping before a single frame is encoded. The windows
    come back in `output_json` so the bench can draw them on the timeline as
    proposals the user nudges, deletes or accepts.

    Whisper may still have to run here (a source with no cached transcript),
    which is why this is a Job and not a request handler — it is local, free
    and can take minutes.
    """
    max_clips = opts.max_clips
    logger.info(
        "TASK START suggest_clips | job=%s video=%s max=%d",
        job_id[:8], downloaded_video_id[:8], max_clips,
    )
    from backend.agents.job_helper import update_job_status
    from backend.database import AsyncSessionLocal
    from backend.models.downloaded_video import DownloadedVideo
    from backend.models.user_settings import UserSettings
    from backend.services.clip_extractor import (
        _load_or_transcribe_segments,
        _raise_if_cancelled,
        _remove_overlapping_clips,
        _select_clip_windows_with_retries,
    )
    from sqlalchemy import select

    try:
        await update_job_status(job_id, "running", progress_pct=0,
                                current_step="Loading video...")

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

        await update_job_status(job_id, "running", progress_pct=10,
                                current_step="Reading the transcript...")
        segments = await _load_or_transcribe_segments(
            video,
            user_settings,
            whisper_quality=opts.whisper_quality,
            force_retranscribe=opts.force_retranscribe,
            job_id=job_id,
            user_id=user_id,
        )
        if not segments:
            raise ValueError(
                "This video has no speech to analyse — drag your own ranges instead"
            )

        # Cancellation gates, same discipline as run_extract_clips: nothing
        # interrupts this coroutine, so without them a cancel during the
        # multi-minute Whisper phase let the runner carry on, make the
        # selection call anyway, and then OVERWRITE the user's "cancelled"
        # with "success" (terminal→terminal writes are deliberately allowed),
        # so the bench adopted proposals the user had cancelled.
        await _raise_if_cancelled(job_id)

        await update_job_status(job_id, "running", progress_pct=55,
                                current_step="Finding the strongest moments...")
        duration = int(video.duration_seconds or 0)
        windows = await _select_clip_windows_with_retries(
            segments,
            video.title or "Untitled",
            duration,
            max_clips,
            user_settings,
            min_duration=opts.min_duration,
            max_duration=opts.max_duration,
            job_id=job_id,
            user_id=user_id,
            user_query=opts.user_query,
            target_platform=opts.target_platform,
            genre=opts.genre,
        )
        # Sanitize BEFORE anything compares these numbers. The windows come
        # straight from an LLM, and a NaN bound would reach the bench as a
        # block of undefined width.
        clean = []
        for w in windows or []:
            if not isinstance(w, dict):
                continue
            try:
                start = float(w.get("start"))
                end = float(w.get("end"))
            except (TypeError, ValueError):
                continue
            if not (start >= 0 and end > start):
                continue
            clean.append({**w, "start": start, "end": end})

        clean = _remove_overlapping_clips(clean)
        if not clean:
            raise ValueError(
                "The AI found no clip-worthy moments in this video — "
                "try a wider length range, or drag your own"
            )

        # Trim to the shape the bench needs. The full window carries the
        # scoring internals; the timeline only draws a span and a reason.
        suggestions = [{
            "start": round(w["start"], 3),
            "end": round(w["end"], 3),
            "title": (w.get("title") or "").strip() or None,
            "score": w.get("virality_score"),
            "hook_score": w.get("hook_score"),
            "hook_type": w.get("hook_type"),
            "reason": (w.get("reason") or w.get("reasoning") or "").strip() or None,
        } for w in clean[:max_clips]]
        suggestions.sort(key=lambda s: s["start"])

        # Second gate: the selection call is the long pole after Whisper.
        await _raise_if_cancelled(job_id)

        await update_job_status(
            job_id, "success", progress_pct=100,
            current_step=f"Found {len(suggestions)} moment(s)",
            output_data={"type": "clip_suggestions",
                         "downloaded_video_id": downloaded_video_id,
                         "suggestions": suggestions},
        )
        logger.info("TASK DONE suggest_clips | job=%s found=%d",
                    job_id[:8], len(suggestions))
    except asyncio.CancelledError:
        logger.info("TASK CANCELLED suggest_clips | job=%s", job_id[:8])
        raise
    except Exception as e:
        from backend.core.exceptions import JobCancelledError
        if isinstance(e, JobCancelledError):
            # Quiet stop, not a failure — mirrors run_extract_clips.
            logger.info("TASK CANCELLED suggest_clips | job=%s (user cancel honoured)", job_id[:8])
            await update_job_status(job_id, "cancelled", current_step="Cancelled")
            return
        logger.error("TASK FAILED suggest_clips | job=%s: %s", job_id[:8], e, exc_info=True)
        await update_job_status(job_id, "failed", error_message=str(e))


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

        # Final cancellation gate — the pipeline polls at its phase
        # boundaries, but a cancel that lands DURING the ffmpeg fan-out is
        # only visible here, after the files exist. Caught before the save
        # loop so a cancelled job leaves no Library rows and the produced
        # files don't orphan in GENERATED_DIR.
        from backend.agents.job_helper import job_cancelled
        from backend.core.exceptions import JobCancelledError
        if await job_cancelled(job_id):
            for clip in clips:
                for key in ("video_path", "thumbnail_path"):
                    p = clip.get(key)
                    if p:
                        try:
                            Path(p).unlink(missing_ok=True)
                        except OSError:
                            pass
            raise JobCancelledError("Job was cancelled by the user")

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

        # A clip whose parallel probe failed reaches here with aspect None
        # (see _probe_one) — re-probe the finished file once rather than
        # guessing 9:16, so the Library tile and the player frame stay honest.
        from backend.services.video_utils import aspect_label

        async def _fallback_aspect(c: dict) -> str:
            path = c.get("video_path")
            if path:
                try:
                    return await asyncio.to_thread(aspect_label, Path(path))
                except Exception:
                    pass
            return "9:16"

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
                    # Probed from the finished file by the extractor, NOT
                    # assumed: extraction only reframes a landscape source, so
                    # a square / 4:5 source keeps its own shape and used to be
                    # persisted as "9:16" — which is what the Library sizes the
                    # tile from and what the aspect filter chips match on.
                    aspect_ratio=clip.get("aspect_ratio")
                    or await _fallback_aspect(clip),
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

        # Tell the USER, not just the log, when a caption burn failed. The job
        # still reports success — the clips exist and play — and the rows still
        # save as "ready", so before this the only trace of a failed burn was a
        # log line: a customer found out by watching the files (rule #14).
        uncaptioned = sum(
            1 for c in clips
            if c.get("caption_status") in ("failed", "extract_failed")
        )
        if uncaptioned > 0:
            await ws_manager.send_constraint_warning(
                "clip_captions_failed",
                f"{uncaptioned} of {len(clips)} clips were saved WITHOUT captions "
                f"— the caption burn failed. The ffmpeg error is in the backend log.",
                severity="warning",
                user_id=user_id,
            )

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
        # User-initiated cancel is a quiet stop, not a failure: the row is
        # already "cancelled" (and must stay that way — a "failed" write here
        # would overwrite it, terminal→terminal is allowed), and the user gets
        # no job_failed toast for something they did on purpose. The import is
        # local because the happy path above already imported it; re-importing
        # is free.
        from backend.core.exceptions import JobCancelledError
        if isinstance(e, JobCancelledError):
            logger.info("TASK CANCELLED extract_clips | job=%s (user cancel honoured)", job_id[:8])
            from backend.agents.job_helper import update_job_status as _update
            await _update(job_id, "cancelled", current_step="Cancelled")
            return
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


async def run_studio_author(job_id: str, brief: dict, user_id: str = "local", **kwargs):
    """Author a composition with the user's own model and set it live.

    The shape that matters is validate → repair → set live. A composition that
    fails the render contract is handed its own errors and re-authored once; a
    composition that fails twice is reported as failed rather than written over
    whatever the user already had. Setting a broken file live would cost them
    working work to gain nothing.
    """
    from backend.agents.job_helper import update_job_status, job_cancelled
    from backend.core.ai_provider import get_ai_client
    from backend.core.exceptions import JobCancelledError
    from backend.core.ws_manager import ws_manager
    from backend.services import composition_author as author
    from backend.services.studio_service import StudioService

    logger.info("TASK START studio-author | job=%s", job_id[:8])
    try:
        await update_job_status(job_id, "running", progress_pct=10,
                                current_step="Designing the composition")
        if await job_cancelled(job_id):
            raise JobCancelledError("Compose cancelled before it started")

        # BYOK: the composition is written by whichever provider and model the
        # user configured, so quality tracks their choice rather than ours.
        from sqlalchemy import select as _select
        from backend.database import AsyncSessionLocal
        from backend.models.user_settings import UserSettings
        async with AsyncSessionLocal() as db:
            row = await db.execute(
                _select(UserSettings).where(UserSettings.user_id == user_id))
            user_settings = row.scalar_one_or_none()
        ai = get_ai_client(user_settings)

        assets_dir = StudioService.project_dir() / "assets"
        html = await author.author_composition(brief, ai)
        issues = await StudioService._all_issues(html, assets_dir)

        for attempt in range(author.MAX_REPAIR_PASSES):
            if not issues:
                break
            if await job_cancelled(job_id):
                raise JobCancelledError("Compose cancelled")
            logger.info("studio-author | job=%s repairing %d issue(s)", job_id[:8], len(issues))
            await update_job_status(job_id, "running", progress_pct=55,
                                    current_step="Fixing what the renderer rejected")
            html = await author.author_composition(
                brief, ai, repair_error="\n".join(f"- {i}" for i in issues),
                previous_html=html)
            issues = await StudioService._all_issues(html, assets_dir)

        if issues:
            # Report the findings rather than a generic failure: they name the
            # element and the fix, which is what makes a second attempt (with a
            # different model, or a clearer brief) worth trying.
            raise ValueError(
                "The composition didn't satisfy the renderer:\n"
                + "\n".join(f"- {i}" for i in issues[:6]))

        await update_job_status(job_id, "running", progress_pct=90,
                                current_step="Opening it in the studio")
        result = await StudioService.import_composition(html)
        await update_job_status(job_id, "success", progress_pct=100, current_step="Done",
                                output_data={"file": result["file"],
                                             "archived": result.get("archived")})
        await ws_manager.send({
            "type": "job_complete", "job_id": job_id, "job_type": "motion_compose",
            "result": result,
        }, user_id)
        logger.info("TASK DONE  studio-author | job=%s", job_id[:8])
    except Exception as e:
        from backend.core.exceptions import JobCancelledError as _JC
        from backend.agents.job_helper import update_job_status as _u
        from backend.core.ws_manager import ws_manager as _ws
        if isinstance(e, _JC):
            logger.info("TASK CANCELLED studio-author | job=%s", job_id[:8])
            await _u(job_id, "cancelled", current_step="Cancelled")
            return
        logger.error("TASK FAIL  studio-author | job=%s: %s", job_id[:8], e, exc_info=True)
        await _u(job_id, "failed", error_message=str(e)[:500])
        await _ws.send({"type": "job_failed", "job_id": job_id,
                        "error": f"Compose failed: {str(e)[:200]}"}, user_id)


async def run_studio_render(job_id: str, aspect_ratio: str = "9:16",
                            title: str | None = None,
                            supersample: bool | None = None, **kwargs):
    """Render the live studio composition to MP4 and land it in the Library.

    Holds the studio's author lock for the render itself: index.html is a
    single mutable file, and a composition swapped in halfway through a render
    produces a video that is half one piece and half another.
    """
    from uuid import uuid4
    from pathlib import Path
    from backend.agents.job_helper import create_job, update_job_status, job_cancelled
    from backend.agents.generator_motion import MotionGeneratorAgent
    from backend.config import settings
    from backend.core.exceptions import JobCancelledError
    from backend.core.ws_manager import ws_manager
    from backend.services.motion_render_service import MotionRenderService
    from backend.services.studio_service import StudioService
    from backend.services.video_utils import probe_duration

    logger.info("TASK START studio-render | job=%s aspect=%s", job_id[:8], aspect_ratio)
    try:
        await update_job_status(job_id, "running", progress_pct=15,
                                current_step="Rendering the composition")
        if await job_cancelled(job_id):
            raise JobCancelledError("Render cancelled before it started")
        out = settings.GENERATED_DIR / f"motion_{uuid4().hex[:12]}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        async with StudioService._author_lock:
            video = await MotionRenderService.render_project(
                StudioService.project_dir(), aspect_ratio, out_path=out, keep=True,
                supersample=supersample)
        # A cancelled render must not land in the Library or overwrite
        # "cancelled" with "success" — check again now that the slow part is
        # done and before anything is persisted.
        if await job_cancelled(job_id):
            await asyncio.to_thread(Path(video).unlink, True)
            raise JobCancelledError("Render cancelled — output discarded")
        await update_job_status(job_id, "running", progress_pct=85,
                                current_step="Saving to library")
        dur = await asyncio.to_thread(probe_duration, video, 0.0)
        thumb = await MotionGeneratorAgent._thumbnail(video)
        gen_id = await MotionGeneratorAgent._save(
            user_id="local", title=(title or "Motion composition")[:120], variables={},
            niche=None, video_path=video, thumb_path=thumb, audio_path=None,
            aspect_ratio=aspect_ratio,
            duration_seconds=int(round(dur)) if dur else None,
            script=None, caption_status=None)
        await update_job_status(job_id, "success", progress_pct=100, current_step="Done",
                                output_data={"generated_video_id": gen_id,
                                             "source_type": "motion_graphics"})
        # create_job announced job_started to every listener. Without a matching
        # terminal broadcast the global active-jobs pill lingers until some
        # unrelated fetch reconciles it.
        await ws_manager.send({
            "type": "job_complete", "job_id": job_id, "job_type": "motion_render",
            "result": {"generated_video_id": gen_id, "source_type": "motion_graphics"},
        }, "local")
        logger.info("TASK DONE  studio-render | job=%s gen=%s", job_id[:8], gen_id[:8])
    except Exception as e:
        from backend.core.exceptions import JobCancelledError as _JC
        from backend.agents.job_helper import update_job_status as _u
        from backend.core.ws_manager import ws_manager as _ws
        if isinstance(e, _JC):
            logger.info("TASK CANCELLED studio-render | job=%s", job_id[:8])
            await _u(job_id, "cancelled", current_step="Cancelled")
            return
        logger.error("TASK FAIL  studio-render | job=%s: %s", job_id[:8], e, exc_info=True)
        await _u(job_id, "failed", error_message=str(e))
        await _ws.send({"type": "job_failed", "job_id": job_id,
                        "error": f"Motion render failed: {e}"}, "local")


def dispatch(coro):
    """Fire-and-forget an async task with concurrency limiting (max 3 concurrent)."""
    logger.debug("Dispatching async task: %s", coro.__qualname__ if hasattr(coro, '__qualname__') else type(coro).__name__)
    asyncio.create_task(_run_with_limit(coro))


# Strong references to long-lived fire-and-forget tasks. asyncio keeps only a
# WEAK reference to tasks (`asyncio.all_tasks` is a WeakSet), so a bare
# `asyncio.create_task(coro)` whose return value is discarded can be
# garbage-collected mid-flight — silently killing the work with no completion
# and no error. Anything that sleeps between passes is especially exposed.
_background_tasks: set = set()


def spawn_background(coro) -> asyncio.Task:
    """Schedule *coro* as a fire-and-forget task, holding a STRONG reference to
    it until it completes.

    Use this instead of ``dispatch()`` for supervisory/long-lived coroutines:
    ``dispatch`` runs its argument inside ``_task_semaphore`` (max 3), so a
    long-lived watcher would permanently occupy a third of the heavy-task
    capacity and starve real jobs. This helper is unlimited but is only for
    cheap, mostly-sleeping work.

    Precondition: a running event loop (same as ``asyncio.create_task``).
    Returns the Task so callers can await/cancel it if they need to.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


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
