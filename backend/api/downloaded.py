# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""REST /api/downloaded — downloaded & analyzed competitor videos."""
import asyncio
import json
import logging
import math
import shutil
from pathlib import Path
from typing import Optional
from uuid import uuid4
from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models.downloaded_video import DownloadedVideo
from backend.models.scout_result import ScoutResult
from backend.core.exceptions import safe_json_loads as _safe_json

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/downloaded")
async def list_downloaded(
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    """List downloaded + analyzed videos."""
    async with AsyncSessionLocal() as db:
        # Count total
        from sqlalchemy import func
        total = (await db.execute(
            select(func.count(DownloadedVideo.id))
        )).scalar()

        result = await db.execute(
            select(DownloadedVideo)
            .order_by(DownloadedVideo.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        videos = result.scalars().all()

        # Batch-fetch all scout results in one query (fixes N+1)
        sr_ids = [v.scout_result_id for v in videos if v.scout_result_id]
        sr_map = {}
        if sr_ids:
            sr_result = await db.execute(
                select(ScoutResult).where(ScoutResult.id.in_(sr_ids))
            )
            sr_map = {sr.id: sr for sr in sr_result.scalars().all()}

        items = []
        for v in videos:
            title = v.title
            platform = v.platform
            thumbnail_url = None
            sr = sr_map.get(v.scout_result_id) if v.scout_result_id else None
            if sr:
                if not title:
                    title = sr.title
                    platform = sr.platform
                    v.title = title
                    v.platform = platform
                thumbnail_url = sr.thumbnail_url

            source_url = sr.video_url if sr else None

            insights = None
            if v.insights_json:
                try:
                    insights = json.loads(v.insights_json)
                except json.JSONDecodeError:
                    pass

            # For news articles, source_url may be stored in insights
            if not source_url and insights and isinstance(insights, dict):
                source_url = insights.get("source_url")

            # Check if files still exist on disk
            video_exists = bool(v.video_path and Path(v.video_path).exists())
            audio_exists = bool(v.audio_path and Path(v.audio_path).exists())

            items.append({
                "id": v.id,
                "scout_result_id": v.scout_result_id,
                "title": title,
                "platform": platform,
                "video_path": v.video_path,
                "audio_path": v.audio_path,
                "thumbnail_path": v.thumbnail_path,
                "thumbnail_url": thumbnail_url,
                "source_url": source_url,
                "file_exists": video_exists or audio_exists,
                "transcript": v.transcript[:200] if v.transcript else None,
                "transcript_language": v.transcript_language,
                "transcript_source": v.transcript_source,
                "insights": insights,
                "has_chapters": bool(v.chapters_json),
                "has_segment_analysis": bool(v.segment_analysis_json),
                "has_improvements": bool(v.improvement_suggestions_json),
                "has_comments": bool(v.comment_insights_json),
                "has_transcript_segments": bool(v.transcript_segments_json),
                "category": v.category,
                "duration_seconds": v.duration_seconds,
                "file_size_mb": v.file_size_mb,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            })

        # Persist any backfilled titles
        await db.commit()

    return {"videos": items, "total": total}


@router.get("/downloaded/{video_id}")
async def get_downloaded(video_id: str):
    """Get a single downloaded video with full transcript and insights."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
        if not video:
            raise HTTPException(status_code=404, detail="Downloaded video not found")

        # Use denormalized title/platform, fall back to scout result
        title = video.title
        platform = video.platform
        source_url = None
        sr = None
        if video.scout_result_id:
            sr_result = await db.execute(
                select(ScoutResult).where(ScoutResult.id == video.scout_result_id)
            )
            sr = sr_result.scalar_one_or_none()
            if sr:
                if not title:
                    title = sr.title
                    platform = sr.platform
                source_url = sr.video_url

        insights = None
        if video.insights_json:
            try:
                insights = json.loads(video.insights_json)
            except json.JSONDecodeError:
                pass

        # For news articles, source_url may be stored in insights
        if not source_url and insights and isinstance(insights, dict):
            source_url = insights.get("source_url")

        # Parse chapters and tags
        chapters = None
        if video.chapters_json:
            try:
                chapters = json.loads(video.chapters_json)
            except json.JSONDecodeError:
                pass

        tags = None
        if video.tags_json:
            try:
                tags = json.loads(video.tags_json)
            except json.JSONDecodeError:
                pass

        return {
            "id": video.id,
            "scout_result_id": video.scout_result_id,
            "title": title,
            "platform": platform,
            "source_url": source_url,
            "video_path": video.video_path,
            "audio_path": video.audio_path,
            "transcript": video.transcript,
            "transcript_language": video.transcript_language,
            "transcript_source": video.transcript_source,
            "insights": insights,
            "insights_json": video.insights_json,
            "segment_analysis": _safe_json(video.segment_analysis_json),
            "improvement_suggestions": _safe_json(video.improvement_suggestions_json),
            "comments": _safe_json(video.comments_json),
            "comment_insights": _safe_json(video.comment_insights_json),
            "chapters": chapters,
            "tags": tags,
            "category": video.category,
            "duration_seconds": video.duration_seconds,
            "file_size_mb": video.file_size_mb,
            "created_at": video.created_at.isoformat() if video.created_at else None,
        }


def drop_bench_caches(video_id: str) -> int:
    """Remove the cutting bench's cached images for one source.

    Two directories under THUMBNAILS_DIR, both keyed `<video_id>_<mtime>_…`:
    `strips/` (the contact sheets behind the timeline) and `frames/` (the
    IN/OUT previews, one per 0.1s bucket per width). Pure caches — deleting
    them costs a rebuild; keeping them after the source is gone costs forever,
    and nothing else knows they exist, because no DB row references them.

    Returns the number of files removed. Never raises: failing to tidy a cache
    must not fail the delete the user asked for.
    """
    removed = 0
    for name in ("strips", "frames"):
        d = settings.THUMBNAILS_DIR / name
        if not d.is_dir():
            continue
        try:
            for f in d.glob(f"{video_id}_*"):
                f.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


@router.delete("/downloaded/{video_id}")
async def delete_downloaded(video_id: str):
    """Delete a downloaded video — removes DB record and files from disk."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
        if not video:
            raise HTTPException(status_code=404, detail="Downloaded video not found")

        # Delete files from disk
        for path_str in [video.video_path, video.audio_path]:
            if path_str:
                p = Path(path_str)
                if p.exists():
                    p.unlink()

        # Sidecar subtitles kept by the `subtitles: "file"` download option
        # live next to the video (`<stem>.<lang>.srt/.vtt`) with no DB column
        # of their own — sweep them with the row or they orphan forever: a
        # delete must cover every file the row owns.
        if video.video_path:
            vp = Path(video.video_path)
            for ext in ("srt", "vtt"):
                for f in vp.parent.glob(f"{vp.stem}.*.{ext}"):
                    try:
                        f.unlink()
                    except OSError:
                        pass

        # The bench's caches are keyed by video id, so they are this row's
        # files too — the same rule as the sidecar subtitles above. An editing
        # session writes hundreds of `frames/` entries and nothing ever asked
        # for them again once the source was gone. (The boot sweep is the
        # backstop for what a crash leaves behind, not a licence to leak here.)
        drop_bench_caches(video_id)

        # Delete associated scout result
        if video.scout_result_id:
            sr_result = await db.execute(
                select(ScoutResult).where(ScoutResult.id == video.scout_result_id)
            )
            sr = sr_result.scalar_one_or_none()
            if sr:
                await db.delete(sr)

        await db.delete(video)
        await db.commit()

    return {"ok": True, "message": "Deleted"}


@router.post("/downloaded/cleanup")
async def cleanup_stale():
    """Remove DB records whose main video file no longer exists on disk.
    Also cleans up orphaned audio files.

    A missing FILE is not a missing VOLUME. With DATA_DIR on an external or
    network drive that is asleep, unplugged or failing, EVERY row's `exists()`
    is False and this would delete the user's entire Clipper library — rows,
    transcripts, analyses and clip lineage — while the mp4s sit intact but
    unreferenced on a drive that comes back a minute later. `list_videos` and
    `job_retention.is_library_item` both take this guard; this endpoint was the
    one destructive surface missing it.
    """
    from backend.config import settings as _settings
    if not _settings.STORAGE_ROOT.exists():
        logger.warning(
            "cleanup_stale refused: the storage root is unreachable, so every "
            "file looks missing. Nothing deleted."
        )
        return {"ok": True, "removed": 0, "skipped": "storage_unavailable"}

    removed = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(DownloadedVideo))
        videos = result.scalars().all()

        for v in videos:
            video_exists = bool(v.video_path and Path(v.video_path).exists())
            # If the video file is gone, the record is stale
            # (audio-only imports check audio_path instead)
            is_audio_only = not v.video_path and v.audio_path
            audio_exists = bool(v.audio_path and Path(v.audio_path).exists())

            if is_audio_only and not audio_exists:
                stale = True
            elif not is_audio_only and not video_exists:
                stale = True
            else:
                stale = False

            if stale:
                # Delete orphaned audio file if video was deleted
                if v.audio_path:
                    p = Path(v.audio_path)
                    if p.exists():
                        p.unlink()
                # And any sidecar subtitles the missing video left behind
                # (`subtitles: "file"` download option — no DB column).
                if v.video_path:
                    vp = Path(v.video_path)
                    for ext in ("srt", "vtt"):
                        for f in vp.parent.glob(f"{vp.stem}.*.{ext}"):
                            try:
                                f.unlink()
                            except OSError:
                                pass
                # Remove scout result
                if v.scout_result_id:
                    sr_result = await db.execute(
                        select(ScoutResult).where(ScoutResult.id == v.scout_result_id)
                    )
                    sr = sr_result.scalar_one_or_none()
                    if sr:
                        await db.delete(sr)
                await db.delete(v)
                removed += 1

        await db.commit()

    logger.info(f"Cleanup: removed {removed} stale downloaded video records")
    return {"ok": True, "removed": removed}


@router.post("/downloaded/batch-download")
async def batch_download_from_urls(body: dict = None):
    """
    Batch download videos from a list of URLs (e.g. from channel analysis).
    Body: { "urls": [{"url": "...", "title": "..."}, ...],
            "options": {...}, "analyze": true }
    Returns: { "job_id": "uuid", "count": N, "options": {...} }

    `options` (all optional — see backend/services/download_options.py) apply
    to every URL in the batch: quality / subtitles / sub_langs / container /
    thumbnail / metadata. Omitted or unrecognized values are dropped and the
    download takes the default path, so an older client or a confused LLM can
    never break a download by sending a bad option.

    `analyze` (default true) runs Whisper + insight extraction afterwards.
    Pass false when the caller just wants the files on disk.
    """
    body = body or {}
    urls = body.get("urls", [])
    if not urls:
        raise HTTPException(status_code=400, detail="No URLs provided")
    if len(urls) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 videos per batch")

    # Accept a bare list of URL strings as well as the documented list of
    # objects. Callers reach for `["https://…"]` constantly (it's the obvious
    # shape), and it used to hit `u.get()` on a str — a 500 with an
    # AttributeError instead of anything the caller could act on. Normalise
    # here so the rest of the handler sees one shape.
    normalized = []
    for u in urls:
        if isinstance(u, str):
            normalized.append({"url": u.strip(), "title": ""})
        elif isinstance(u, dict):
            normalized.append(u)
        else:
            raise HTTPException(
                status_code=400,
                detail=(f"Each entry must be a URL string or an object with a "
                        f"`url` field, got {type(u).__name__}"),
            )
    urls = [u for u in normalized if (u.get("url") or "").strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="No usable URLs provided")

    # URL trust boundary. This endpoint is reachable by the chat planner (i.e.
    # by LLM-relayed values) as well as by our own UI, and every entry here is
    # handed to yt-dlp — which happily accepts `file:///etc/passwd`. Guard at
    # the endpoint so the check can't be bypassed by a different caller.
    for u in urls:
        link = (u.get("url") or "").strip()
        if not link.lower().startswith(("http://", "https://")):
            raise HTTPException(
                status_code=400,
                detail="Every url must be a full http(s) link",
            )

    from backend.services.download_options import normalize as _normalize_opts
    options = _normalize_opts(body.get("options"))
    analyze = body.get("analyze", True) is not False

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import run_batch_download_urls, dispatch

    job = await create_job("download", "local", {
        "batch_urls": [u.get("url", "") for u in urls],
        "count": len(urls),
        # Persisted so a UI re-attaching to a running job can still show what
        # was requested, and so a bug report can tell "asked for 4K" from
        # "got 4K".
        "options": options,
    })
    dispatch(run_batch_download_urls(
        job_id=job.id,
        urls=urls,
        user_id="local",
        options=options or None,
        analyze=analyze,
    ))
    # Echo the NORMALIZED options, not the raw body — the caller sees exactly
    # what will be applied rather than what it hoped for.
    return {"job_id": job.id, "count": len(urls), "options": options}


@router.post("/downloaded/import")
async def import_local_video(
    file: UploadFile = File(...),
    title: str = Form(""),
):
    """
    Import a user's own video file for transcription + AI analysis.
    Accepts video uploads (mp4, mov, avi, mkv, webm) or audio files (mp3, wav, m4a).
    """
    # Validate file type
    allowed_extensions = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".flac"}
    suffix = Path(file.filename).suffix.lower() if file.filename else ""
    if suffix not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. Allowed: {', '.join(sorted(allowed_extensions))}",
        )

    is_audio_only = suffix in {".mp3", ".wav", ".m4a", ".aac", ".flac"}
    file_id = str(uuid4())[:12]

    # Save to appropriate storage directory
    if is_audio_only:
        dest_dir = settings.AUDIO_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"{file_id}{suffix}"
    else:
        dest_dir = settings.VIDEOS_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"{file_id}{suffix}"

    # Stream upload to disk (max 2GB)
    MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024
    file_size = 0
    with open(dest_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):  # 1MB chunks
            file_size += len(chunk)
            if file_size > MAX_UPLOAD_SIZE:
                f.close()
                dest_path.unlink(missing_ok=True)
                raise HTTPException(413, "File too large. Maximum upload size is 2GB.")
            f.write(chunk)

    # An empty upload is not an import. The size was capped above but never
    # floored, so a zero-byte file became a Library row pointing at zero bytes
    # — it would fail later in whatever tool the user pointed at it, with no
    # hint that the import itself was the problem.
    if file_size == 0:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(400, "The uploaded file is empty.")

    file_size_mb = round(file_size / (1024 * 1024), 2)
    display_title = title or Path(file.filename).stem if file.filename else "Imported video"

    # Create DB records
    async with AsyncSessionLocal() as db:
        sr = ScoutResult(
            user_id="local",
            platform="import",
            video_id=file_id,
            video_url="",
            title=display_title,
            is_downloaded=True,
            virality_score=0,
        )
        db.add(sr)
        await db.flush()

        dv = DownloadedVideo(
            user_id="local",
            scout_result_id=sr.id,
            title=display_title,
            platform="import",
            video_path=str(dest_path) if not is_audio_only else None,
            audio_path=str(dest_path) if is_audio_only else None,
            file_size_mb=file_size_mb,
        )
        db.add(dv)
        await db.commit()
        await db.refresh(dv)

    # Kick off analysis (transcription + AI insights) in background
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import run_analyze_imported, dispatch

    job = await create_job("analyze", "local", {"downloaded_video_id": dv.id, "title": display_title})
    dispatch(run_analyze_imported(job_id=job.id, downloaded_video_id=dv.id, user_id="local"))

    return {
        "id": dv.id,
        "job_id": job.id,
        "title": display_title,
        "file_size_mb": file_size_mb,
        "message": "Video imported. Transcription and analysis starting...",
    }


@router.post("/downloaded/polish-script")
async def polish_script(body: dict = None):
    """Use AI to improve/polish an existing script."""
    body = body or {}
    script_text = body.get("script", "").strip()
    if not script_text:
        raise HTTPException(status_code=400, detail="No script text provided")

    from backend.models.user_settings import UserSettings
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == "local")
        )
        user_settings = result.scalar_one_or_none()

    from backend.core.ai_provider import get_ai_client
    from backend.core.exceptions import AIKeyMissingError

    prompt = f"""Improve and polish this video narration script.

Fix grammar, improve flow, sharpen the hook, and make it more engaging.
Keep the same topic, structure, and approximate length.
Do NOT add stage directions, timestamps, or scene descriptions.
Return ONLY the improved script text.

Original script:
{script_text[:8000]}"""

    try:
        ai = get_ai_client(user_settings)
        polished = await ai.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        return {"script": polished.strip()}
    except AIKeyMissingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Script polishing failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Script polishing failed: {e}")


@router.post("/downloaded/generate-script-from-topic")
async def generate_script_from_topic(body: dict = None):
    """Generate a script from AI using only a topic/instructions — no source video needed."""
    body = body or {}
    topic = body.get("topic", "").strip()
    user_instructions = body.get("user_instructions", "").strip()
    aspect_ratio = body.get("aspect_ratio", "9:16")

    if not topic and not user_instructions:
        raise HTTPException(status_code=400, detail="Provide a topic or instructions for the script")

    from backend.models.user_settings import UserSettings
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == "local")
        )
        user_settings = result.scalar_one_or_none()

    from backend.core.ai_provider import get_ai_client
    from backend.core.exceptions import AIKeyMissingError

    platform_format = "vertical short-form" if aspect_ratio == "9:16" else "horizontal long-form"
    combined = f"{topic}\n{user_instructions}".strip() if topic else user_instructions

    prompt = f"""Write an original, engaging video script for a {platform_format} video.

Topic/instructions: {combined}

Requirements:
- Write a complete narration script (NOT a shot list)
- Start with a strong hook in the first 5 seconds
- Keep it concise — aim for 60-90 seconds spoken
- Use conversational, engaging tone
- Do NOT include stage directions, timestamps, or scene descriptions
- Return ONLY the script text, nothing else"""

    try:
        ai = get_ai_client(user_settings)
        script = await ai.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        return {"script": script.strip()}
    except AIKeyMissingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Script generation from topic failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Script generation failed: {e}")


@router.post("/downloaded/{video_id}/generate-script")
async def generate_script_only(video_id: str, body: dict = None):
    """Generate a script from AI using the video's insights, without starting full pipeline."""
    body = body or {}
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Downloaded video not found")

    from backend.models.user_settings import UserSettings
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == "local")
        )
        user_settings = result.scalar_one_or_none()

    from backend.agents.generator import GeneratorAgent
    from backend.core.exceptions import AIKeyMissingError, AIProviderError
    agent = GeneratorAgent()
    aspect_ratio = body.get("aspect_ratio", "9:16")
    user_instructions = body.get("user_instructions")
    try:
        script = await agent._generate_script(video, aspect_ratio, user_settings, user_instructions=user_instructions)
    except AIKeyMissingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Script generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Script generation failed: {e}")
    return {"script": script}


@router.post("/downloaded/{video_id}/generate")
async def generate_from_downloaded(video_id: str, body: dict = None):
    """Trigger video generation from a downloaded + analyzed video with optional config."""
    body = body or {}
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Downloaded video not found")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import run_generate, dispatch

    job = await create_job("generate", "local", {"downloaded_video_id": video_id, **body})
    dispatch(run_generate(
        job_id=job.id,
        downloaded_video_id=video_id,
        user_id="local",
        aspect_ratio=body.get("aspect_ratio", "9:16"),
        tts_provider=body.get("tts_provider"),
        tts_voice=body.get("tts_voice"),
        start_image=body.get("start_image"),
        caption_style=body.get("caption_style"),
        caption_enabled=body.get("caption_enabled"),
        music_enabled=body.get("music_enabled"),
        music_genre=body.get("music_genre"),
        custom_script=body.get("custom_script"),
    ))
    return {"job_id": job.id}


@router.post("/downloaded/batch-generate")
async def batch_generate(body: dict = None):
    """
    Orchestrated batch video generation — runs items sequentially within a
    single parent job for coordinated progress tracking and partial failure handling.

    Body: {
        "items": [
            {"downloaded_video_id": "id1"},
            {"downloaded_video_id": "id2", "gen_tier": "standard"},
        ],
        "shared_settings": {
            "aspect_ratio": "9:16",
            "tts_provider": "edge_tts",
            "caption_style": "viral",
            "music_genre": "lofi",
            ...
        }
    }
    Returns: { "job_id": "parent-uuid", "count": N }
    """
    body = body or {}
    items = body.get("items", [])
    shared = body.get("shared_settings", {})

    if not items:
        raise HTTPException(status_code=400, detail="No items provided")
    if len(items) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 videos per batch")

    # Validate all video IDs exist and filter out missing
    video_ids = [item.get("downloaded_video_id") for item in items if item.get("downloaded_video_id")]
    if not video_ids:
        raise HTTPException(status_code=400, detail="No valid video IDs provided")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo.id).where(DownloadedVideo.id.in_(video_ids))
        )
        found_ids = {row[0] for row in result.fetchall()}

    missing = [vid for vid in video_ids if vid not in found_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"Videos not found: {missing}")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import run_batch_generate, dispatch

    # Single parent job tracks the entire batch
    parent_job = await create_job("batch_generate", "local", {
        "items": items,
        "shared_settings": shared,
        "count": len(items),
    })
    dispatch(run_batch_generate(
        job_id=parent_job.id,
        items=items,
        shared_settings=shared,
        user_id="local",
    ))

    return {"job_id": parent_job.id, "count": len(items)}


@router.post("/downloaded/{video_id}/reanalyze")
async def reanalyze_video(video_id: str, body: dict = None):
    """Re-transcribe and re-analyze a video with a different Whisper model quality."""
    body = body or {}
    whisper_quality = body.get("whisper_quality", "balanced")
    if whisper_quality not in ("fast", "balanced", "accurate", "best"):
        raise HTTPException(status_code=400, detail=f"Invalid whisper_quality: {whisper_quality}")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Downloaded video not found")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import run_reanalyze, dispatch
    from backend.core.ws_manager import ws_manager

    job = await create_job("analyze", "local", {"video_id": video_id, "whisper_quality": whisper_quality})
    dispatch(run_reanalyze(job_id=job.id, video_id=video_id, whisper_quality=whisper_quality, user_id="local"))
    await ws_manager.send({
        "type": "job_started",
        "job_id": job.id,
        "job_type": "analyze",
        "message": f"Re-analyzing with {whisper_quality} Whisper model...",
    }, "local")
    return {"job_id": job.id}


# ── Auto-cut sizing ───────────────────────────────────────────────────
# How many clips the AI picker aims for when the caller names no number.
# Leaving "Clips (max)" blank on a 22-minute source means 43 clips, and the
# dialog that submits it used to show no figure at all — it said "Extract
# now" and quietly authorised 43 renders. The rule lives HERE, once, and the
# dialog reads the same number from
# frontend/src/components/clip/autoClipCount.js, pinned across the two by
# tests/test_auto_clip_count_parity.py. A surface that promises a count has
# to resolve it the way the runner will.
AUTO_CLIP_SECONDS_PER_CLIP = 30
AUTO_CLIP_MIN = 3
AUTO_CLIP_MAX = 99
AUTO_CLIP_UNKNOWN_DURATION = 5


def auto_clip_count(duration_seconds: float | int | None) -> int:
    """How many clips Auto-cut will aim for when the user names no number."""
    if not duration_seconds or duration_seconds <= 0:
        return AUTO_CLIP_UNKNOWN_DURATION
    return max(AUTO_CLIP_MIN,
               min(AUTO_CLIP_MAX, int(duration_seconds) // AUTO_CLIP_SECONDS_PER_CLIP))


# ── Manual-mode constants ─────────────────────────────────────────────
# Surface here so tests and docs can introspect; tweaking these touches
# both validation and the user-facing 400 message.
_MANUAL_MAX_RANGES = 10      # cap per submit; mirrored on the frontend as
                             # MAX_RANGES (bench/useBenchRanges.js).
                             # Deliberately lowered 20 → 10 on 2026-05-27
                             # (UX: 20 was too many for a hand-curated
                             # workflow) — pinned by
                             # tests/test_clip_extractor_manual_mode.py.
                             # It also ceilings AI suggestions, since every
                             # proposal is cut through manual mode.
_MANUAL_MIN_CLIP_SEC = 1.0   # sub-second clips have no visual value
_MANUAL_OVERLAP_EPS = 0.01   # float-noise tolerance when rejecting overlaps; the UI
                             # rounds bounds to 3dp and a real user overlap is seconds,
                             # so 10ms cleanly separates noise from intent.


def _validate_manual_time_ranges(raw_ranges, video_duration: float) -> list[dict]:
    """Parse + validate user-supplied time ranges for manual-mode extraction.

    `raw_ranges` is the unsafe payload from the API client: a list of
    dicts where `start` / `end` are strings ("0:18") OR numbers (18).
    Returns a normalized list of `{"start": float, "end": float}` in
    seconds, sorted by start time.

    Raises HTTPException(400) with a per-range message on any failure
    so the user knows exactly which row in the dialog needs fixing.
    """
    from backend.services.clip_extractor import _parse_timestamp

    if not isinstance(raw_ranges, list) or not raw_ranges:
        raise HTTPException(
            status_code=400,
            detail="time_ranges required in manual mode (list of {start, end} entries)",
        )
    if len(raw_ranges) > _MANUAL_MAX_RANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many ranges: {len(raw_ranges)} (max {_MANUAL_MAX_RANGES} per submit)",
        )

    parsed: list[dict] = []
    for i, r in enumerate(raw_ranges):
        label = f"Range {i + 1}"
        if not isinstance(r, dict):
            raise HTTPException(
                status_code=400,
                detail=f"{label}: expected an object with `start` and `end`",
            )
        try:
            start = _parse_timestamp(r.get("start"))
            end = _parse_timestamp(r.get("end"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"{label}: {e}")

        # NaN specifically: `json.loads` accepts a bare `NaN` literal and EVERY
        # comparison against NaN is False, so `end <= start` below passes it
        # straight through to ffmpeg as `-ss nan`. The free /suggest-clips path
        # already rejected this shape; the cutting path did not. Guarded with
        # isfinite rather than a range check so a slightly-negative start is
        # still clamped into a usable clip instead of newly refused.
        if not (math.isfinite(start) and math.isfinite(end)):
            raise HTTPException(
                status_code=400,
                detail=f"{label}: start and end must be finite numbers",
            )
        if end <= start:
            raise HTTPException(
                status_code=400,
                detail=f"{label}: end ({end:g}s) must be after start ({start:g}s)",
            )
        if end - start < _MANUAL_MIN_CLIP_SEC:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{label}: clip too short "
                    f"({end - start:.2f}s; min {_MANUAL_MIN_CLIP_SEC:g}s)"
                ),
            )
        # Allow a tiny epsilon (0.5s) past the cached duration — yt-dlp
        # sometimes records a duration off by half a second vs the actual
        # frame count.
        if video_duration and end > video_duration + 0.5:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{label}: end ({end:g}s) exceeds video duration "
                    f"({video_duration:g}s)"
                ),
            )
        parsed.append({"start": start, "end": end, "_label": label})

    # Sort chronologically for stable clip numbering downstream.
    parsed.sort(key=lambda r: r["start"])

    # Reject OVERLAPS. Manual mode cuts verbatim — `_build_manual_clip_windows`
    # deliberately skips the AI path's `_remove_overlapping_clips` — so two
    # ranges covering the same seconds produce two near-identical clips, and
    # `max_clips = len(time_ranges)` pre-authorises exactly that.
    #
    # "Nothing may stack" was enforced only in the bench's
    # `useBenchRanges.add/addAt/addMany`. Two doors went around it: dragging a
    # block over its neighbour or typing a timecode (both `update`, which
    # clamped bounds but never looked at neighbours), and any API client posting
    # `time_ranges` directly. The invariant belongs on the server, where every
    # client meets it.
    #
    # Touching ranges (a.end == b.start) are legitimate back-to-back cuts and
    # stay allowed; the epsilon only absorbs float noise from the UI's rounding.
    for a, b in zip(parsed, parsed[1:]):
        if b["start"] < a["end"] - _MANUAL_OVERLAP_EPS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{a['_label']} ({a['start']:g}-{a['end']:g}s) and "
                    f"{b['_label']} ({b['start']:g}-{b['end']:g}s) overlap. "
                    f"Manual mode cuts ranges exactly as given, so overlapping "
                    f"ranges would produce duplicate clips. Adjust them so they "
                    f"don't cover the same seconds."
                ),
            )

    return [{"start": r["start"], "end": r["end"]} for r in parsed]


class ExtractClipsRequest(BaseModel):
    """Wire model for POST /downloaded/{id}/extract-clips.

    Field names + defaults stay stable — the MCP `extract_viral_clips`
    tool and older scripts post this exact shape. Validation/normalization
    (mode/emoji/platform/genre vocab, duration clamps, manual time_ranges)
    happens in the handler; this just types the wire + centralizes defaults.
    """
    mode: str = "ai"
    max_clips: Optional[int] = None
    caption_style: str = "viral"
    whisper_quality: str = "balanced"
    force_retranscribe: bool = False
    min_duration: Optional[int] = None
    max_duration: Optional[int] = None
    remove_silence: bool = False
    force_vertical: bool = False
    user_query: Optional[str] = None
    target_platform: Optional[str] = None
    emoji_style: str = "moderate"
    genre: Optional[str] = None
    time_ranges: Optional[list[dict]] = None
    # AI mode only. When the picker returns nothing, the run cuts evenly-spaced
    # windows by duration so you still get deliverables — those are flagged
    # `picker_fallback` in the job output and `clip_selection="duration_fallback"`
    # per clip. Pass false to get an error instead of time slices.
    allow_duration_fallback: bool = True


@router.post("/downloaded/{video_id}/extract-clips")
async def extract_clips(video_id: str, body: ExtractClipsRequest | None = None):
    """Extract clips from a downloaded long-form video.

    Two modes:
      - "ai" (default) — AI-driven viral-clip picker. Returns N clips
        based on virality scoring of the Whisper transcript.
      - "manual"       — cut user-supplied `time_ranges` verbatim.

    Body params:
        mode: "ai" | "manual" (default "ai")
        time_ranges: list[{start, end}] — REQUIRED when mode="manual".
            Each start/end is either a number (seconds) or a string in
            "SS" / "MM:SS" / "HH:MM:SS" form (with optional ".fff").

      AI mode only (ignored when mode="manual"):
        max_clips: int (1-99, default auto)
        min_duration / max_duration: int (clip seconds, optional)
        user_query: str (free-form filter; ranks above pure virality)
        target_platform: str (hook-type bias — tiktok | youtube_shorts |
            reels | linkedin | twitter; aliases accepted)
        genre: str (genre bias — podcast | interview | qa | vlog |
            tutorial | gaming | reaction | lecture; aliases accepted)

      Both modes (post-processing):
        caption_style: str (default viral)
        emoji_style: str (none|minimal|moderate|heavy, default moderate)
        whisper_quality: str (default balanced)
        force_retranscribe: bool (default false)
        remove_silence: bool (remove silent gaps & filler words, default false)
        force_vertical: bool (default false)
    """
    req = body or ExtractClipsRequest()
    # Mode is the discriminator — branches API-side validation. Default "ai"
    # preserves the legacy behaviour for callers that don't know about manual
    # mode (older MCP configs, scripts written before today).
    mode = (req.mode or "ai").strip().lower()
    if mode not in ("ai", "manual"):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown mode '{mode}' (expected 'ai' or 'manual')",
        )

    caption_style = req.caption_style or "viral"
    whisper_quality = req.whisper_quality or "balanced"
    force_retranscribe = bool(req.force_retranscribe)
    min_duration = req.min_duration
    max_duration = req.max_duration
    remove_silence = bool(req.remove_silence)
    force_vertical = bool(req.force_vertical)
    user_query = (req.user_query or "").strip() or None
    # Platform hint biases the AI hook-type ranker. Unknown values quietly
    # degrade to vanilla virality ranking — _build_platform_bias_block handles
    # the unknown-key case.
    target_platform = (req.target_platform or "").strip().lower() or None
    # AutoEmoji density. Unknown values fall back to "moderate" rather than
    # raising — caller doesn't have to know the exact vocabulary.
    _EMOJI_STYLES = {"none", "minimal", "moderate", "heavy"}
    emoji_style_raw = (req.emoji_style or "moderate").strip().lower()
    emoji_style = emoji_style_raw if emoji_style_raw in _EMOJI_STYLES else "moderate"
    # Genre hint biases the AI's clip-selection criteria. Unknown values
    # quietly degrade to no genre bias.
    genre = (req.genre or "").strip().lower() or None
    max_clips_input = req.max_clips
    max_clips = None  # will be computed after loading video

    # Validate custom duration range (AI mode only — manual mode uses
    # user-supplied ranges directly).
    if mode == "ai":
        if min_duration is not None:
            min_duration = max(10, int(min_duration))
        if max_duration is not None:
            max_duration = max(15, int(max_duration))
        if min_duration and max_duration and min_duration >= max_duration:
            raise HTTPException(status_code=400, detail="min_duration must be less than max_duration")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Downloaded video not found")
    if not video.video_path or not Path(video.video_path).exists():
        raise HTTPException(status_code=400, detail="Video file not found on disk")

    # Short videos are handled by the extractor's short-video fast-path
    # (SHORT_VIDEO_THRESHOLD): a source under ~20s is emitted as a single
    # captioned clip rather than rejected. No blanket "too short" floor here.
    duration = video.duration_seconds or 0

    # Manual mode: validate ranges against the loaded video BEFORE creating the
    # job so a typo'd range fails cleanly at submit time.
    time_ranges: list[dict] | None = None
    if mode == "manual":
        time_ranges = _validate_manual_time_ranges(
            req.time_ranges,
            video_duration=duration,
        )
        max_clips = len(time_ranges)
    else:
        # Auto-calculate max_clips from duration if not specified
        # (~1 clip per 30s, min 3, max 99). AI-mode only.
        if max_clips_input is not None:
            max_clips = max(1, min(int(max_clips_input), 99))
        else:
            # Same rule the dialog quotes — see auto_clip_count.
            max_clips = auto_clip_count(duration)

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import run_extract_clips, dispatch
    from backend.core.ws_manager import ws_manager
    from backend.services.clip_options import ExtractOptions

    job = await create_job("generate", "local", {
        "downloaded_video_id": video_id,
        "type": "clip_extraction",
        "mode": mode,
        "max_clips": max_clips,
    })
    opts = ExtractOptions(
        mode=mode,
        max_clips=max_clips,
        caption_style=caption_style,
        whisper_quality=whisper_quality,
        force_retranscribe=force_retranscribe,
        min_duration=min_duration,
        max_duration=max_duration,
        remove_silence=remove_silence,
        force_vertical=force_vertical,
        user_query=user_query,
        target_platform=target_platform,
        emoji_style=emoji_style,
        genre=genre,
        time_ranges=time_ranges,
        allow_duration_fallback=req.allow_duration_fallback,
    )
    dispatch(run_extract_clips(
        job_id=job.id,
        downloaded_video_id=video_id,
        opts=opts,
        user_id="local",
    ))
    video_title = video.title or "Untitled"
    mode_label = "user-specified" if mode == "manual" else "viral"
    await ws_manager.send({
        "type": "job_started",
        "job_id": job.id,
        "job_type": "generate",
        "message": f"Extracting {mode_label} clips from: {video_title}",
        "input_data": {
            "type": "clip_extraction",
            "downloaded_video_id": video_id,
            "mode": mode,
            "max_clips": max_clips,
        },
    }, "local")
    return {"job_id": job.id}


class SuggestClipsRequest(BaseModel):
    """Wire model for POST /downloaded/{id}/suggest-clips.

    A strict subset of ExtractClipsRequest: the AI-targeting knobs plus the
    transcription controls. Nothing about captions, emoji, silence or aspect
    belongs here — this endpoint produces no clip to apply them to.
    """
    max_clips: Optional[int] = None
    min_duration: Optional[int] = None
    max_duration: Optional[int] = None
    user_query: Optional[str] = None
    target_platform: Optional[str] = None
    genre: Optional[str] = None
    whisper_quality: str = "balanced"
    force_retranscribe: bool = False


@router.post("/downloaded/{video_id}/suggest-clips")
async def suggest_clips(video_id: str, body: SuggestClipsRequest | None = None):
    """Find viral moments WITHOUT cutting them — the plan half of AI picks.

    Runs the same transcript load and the same AI window selection as
    mode="ai" extraction, then stops and hands the windows back. The Clipper
    bench draws them on its timeline as proposals the user can drag, delete or
    accept; accepting posts them straight back through manual mode, so what
    gets cut is what was on screen.

    Free to look at, and that is the point: the AI's choice of in and out
    point used to be final and invisible until after the clips existed.

    Body params (all optional, AI-targeting only):
        max_clips: how many moments to propose. Clamped to
            `_MANUAL_MAX_RANGES`, because every proposal has to survive the
            round trip back through manual mode to be cut.
        min_duration / max_duration: int seconds
        user_query / target_platform / genre: same vocabulary as
            POST /extract-clips
        whisper_quality / force_retranscribe: transcription controls, used
            only when the source has no cached transcript

    Returns:
        {"job_id": str} — poll /api/jobs/{id}; on success `output_json`
        carries {"type": "clip_suggestions", "suggestions": [
            {start, end, title, score, hook_score, hook_type, reason}, ...]}
    """
    req = body or SuggestClipsRequest()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Downloaded video not found")
    if not video.video_path or not Path(video.video_path).exists():
        raise HTTPException(status_code=400, detail="Video file not found on disk")

    duration = video.duration_seconds or 0
    min_duration = req.min_duration
    max_duration = req.max_duration
    if min_duration is not None:
        min_duration = max(10, int(min_duration))
    if max_duration is not None:
        max_duration = max(15, int(max_duration))
    if min_duration and max_duration and min_duration >= max_duration:
        raise HTTPException(status_code=400,
                            detail="min_duration must be less than max_duration")

    # Capped at the manual-mode ceiling on purpose: a proposal the bench
    # cannot submit is worse than one it never made. Users who want more clips
    # than the bench can review still have direct extraction.
    if req.max_clips is not None:
        max_clips = max(1, min(int(req.max_clips), _MANUAL_MAX_RANGES))
    else:
        max_clips = int(min(auto_clip_count(duration), _MANUAL_MAX_RANGES))

    _WHISPER_QUALITIES = {"fast", "balanced", "accurate", "best"}
    wq = (req.whisper_quality or "balanced").strip().lower()
    if wq not in _WHISPER_QUALITIES:
        wq = "balanced"

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import run_suggest_clips, dispatch
    from backend.core.ws_manager import ws_manager
    from backend.services.clip_options import ExtractOptions

    job = await create_job("generate", "local", {
        "downloaded_video_id": video_id,
        "type": "clip_suggestion",
        "max_clips": max_clips,
    })
    opts = ExtractOptions(
        mode="ai",
        max_clips=max_clips,
        min_duration=min_duration,
        max_duration=max_duration,
        whisper_quality=wq,
        force_retranscribe=bool(req.force_retranscribe),
        user_query=(req.user_query or "").strip() or None,
        target_platform=(req.target_platform or "").strip().lower() or None,
        genre=(req.genre or "").strip().lower() or None,
    )
    dispatch(run_suggest_clips(
        job_id=job.id,
        downloaded_video_id=video_id,
        opts=opts,
        user_id="local",
    ))
    await ws_manager.send({
        "type": "job_started",
        "job_id": job.id,
        "job_type": "generate",
        "message": f"Finding the strongest moments in: {video.title or 'Untitled'}",
        "input_data": {
            "type": "clip_suggestion",
            "downloaded_video_id": video_id,
            "max_clips": max_clips,
        },
    }, "local")
    return {"job_id": job.id}


@router.post("/downloaded/{video_id}/ai-action")
async def ai_action(video_id: str, body: dict = None):
    """Run an inline AI action on a downloaded video's insights/script.

    Actions:
        strengthen_hook — Rewrite the hook to be more attention-grabbing
        translate — Translate insights/suggested angle to a target language
        rewrite_shorter — Make the suggested angle more concise
        rewrite_for_platform — Adapt for a specific platform (tiktok/youtube/instagram)
        suggest_titles — Generate 5 click-worthy title alternatives
        improve_angle — Elaborate and strengthen the suggested angle
    """
    body = body or {}
    action = body.get("action", "").strip()
    target_language = body.get("language", "").strip()
    target_platform = body.get("platform", "").strip()

    VALID_ACTIONS = {
        "strengthen_hook", "translate", "rewrite_shorter",
        "rewrite_for_platform", "suggest_titles", "improve_angle",
    }
    if action not in VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid action: {action}. Valid: {', '.join(sorted(VALID_ACTIONS))}")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Downloaded video not found")

    insights = _safe_json(video.insights_json, {})
    if not insights:
        raise HTTPException(status_code=400, detail="No insights available. Run analysis first.")

    # Build context from existing insights
    context_parts = []
    if insights.get("hook"):
        context_parts.append(f"Current hook: {insights['hook']}")
    if insights.get("suggested_angle"):
        context_parts.append(f"Suggested angle: {insights['suggested_angle']}")
    if insights.get("suggested_title"):
        context_parts.append(f"Suggested title: {insights['suggested_title']}")
    if insights.get("why_viral"):
        context_parts.append(f"Why it went viral: {insights['why_viral']}")
    if insights.get("structure"):
        context_parts.append(f"Video structure: {insights['structure']}")
    if insights.get("tone"):
        context_parts.append(f"Tone: {insights['tone']}")
    if insights.get("key_phrases"):
        context_parts.append(f"Key phrases: {', '.join(insights['key_phrases'][:10])}")
    context = "\n".join(context_parts)

    # Build the prompt based on action
    prompts = {
        "strengthen_hook": (
            f"You are a viral video expert. Rewrite this hook to be more attention-grabbing and "
            f"impossible to scroll past. Make it punchy, curiosity-driven, and under 10 words.\n\n"
            f"Current hook: {insights.get('hook', 'N/A')}\n\n"
            f"Context:\n{context}\n\n"
            f"Return ONLY the improved hook text, nothing else."
        ),
        "translate": (
            f"Translate all the following video insights into {target_language or 'Chinese'}. "
            f"Keep the same structure and meaning. Return as JSON with the same keys.\n\n"
            f"Insights:\n{json.dumps(insights, ensure_ascii=False, indent=2)}\n\n"
            f"Return ONLY valid JSON, nothing else."
        ),
        "rewrite_shorter": (
            f"Rewrite this video angle to be more concise and punchy — aim for 1-2 sentences max.\n\n"
            f"Current angle: {insights.get('suggested_angle', 'N/A')}\n\n"
            f"Context:\n{context}\n\n"
            f"Return ONLY the shortened angle text, nothing else."
        ),
        "rewrite_for_platform": (
            f"Adapt this video concept specifically for {target_platform or 'TikTok'}.\n\n"
            f"Consider the platform's audience, typical video length, trending formats, and what performs well.\n\n"
            f"Current concept:\n{context}\n\n"
            f"Return a JSON object with keys: hook, suggested_angle, suggested_title, format_tips. "
            f"Return ONLY valid JSON, nothing else."
        ),
        "suggest_titles": (
            f"Generate 5 click-worthy, curiosity-driven title alternatives for a video based on these insights.\n\n"
            f"Context:\n{context}\n\n"
            f"Requirements:\n"
            f"- Each title should be under 60 characters\n"
            f"- Use power words, numbers, or curiosity gaps\n"
            f"- Vary the style (question, list, bold claim, how-to, revelation)\n\n"
            f"Return ONLY a JSON array of 5 title strings, nothing else."
        ),
        "improve_angle": (
            f"Elaborate and strengthen this video angle. Make it more specific, more actionable, "
            f"and more likely to resonate with viewers.\n\n"
            f"Current angle: {insights.get('suggested_angle', 'N/A')}\n\n"
            f"Full context:\n{context}\n\n"
            f"Return a 2-3 sentence improved angle that is specific and compelling. "
            f"Return ONLY the improved angle text, nothing else."
        ),
    }

    prompt = prompts[action]

    from backend.models.user_settings import UserSettings
    async with AsyncSessionLocal() as db:
        us_result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == "local")
        )
        user_settings = us_result.scalar_one_or_none()

    from backend.core.ai_provider import get_ai_client
    from backend.core.exceptions import AIKeyMissingError

    try:
        ai = get_ai_client(user_settings)
        result_text = await ai.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        result_text = result_text.strip()

        # For JSON-returning actions, try to parse
        if action in ("translate", "rewrite_for_platform", "suggest_titles"):
            # Strip markdown code fences if present
            if result_text.startswith("```"):
                lines = result_text.split("\n")
                result_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                result_text = result_text.strip()
            try:
                parsed = json.loads(result_text)
                return {"action": action, "result": parsed, "raw": result_text}
            except json.JSONDecodeError:
                return {"action": action, "result": result_text, "raw": result_text}

        return {"action": action, "result": result_text}
    except AIKeyMissingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"AI action '{action}' failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"AI action failed: {e}")


@router.get("/downloaded/{video_id}/thumbnail")
async def get_downloaded_thumbnail(video_id: str):
    """Serve the thumbnail for a downloaded video.

    Falls back to extracting one from the video file (ffmpeg) if thumbnail_path
    isn't set, then backfills the DB so we don't re-extract next time.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Existing thumbnail first.
    if video.thumbnail_path:
        thumb = Path(video.thumbnail_path)
        if thumb.exists():
            return FileResponse(thumb, media_type="image/jpeg")

    # No thumbnail yet — extract one from the video file.
    video_file = video.video_path
    if not video_file or not Path(video_file).exists():
        raise HTTPException(status_code=404, detail="No thumbnail or video file available")

    try:
        from backend.services.ffmpeg_service import extract_thumbnail
        thumb_path = await extract_thumbnail(
            Path(video_file),
            output_path=settings.THUMBNAILS_DIR / f"dl_{video_id[:8]}_thumb.jpg",
            timestamp=2.0,
        )
        if thumb_path and thumb_path.exists():
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(DownloadedVideo).where(DownloadedVideo.id == video_id)
                )
                row = result.scalar_one_or_none()
                if row:
                    row.thumbnail_path = str(thumb_path)
                    await db.commit()
            return FileResponse(thumb_path, media_type="image/jpeg")
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="Could not generate thumbnail")


# ── Cutting-bench support (Clipper) ───────────────────────────────────
# Three read-only sources the bench needs to SHOW a video instead of asking
# the user to type timestamps at it: a scrubbing filmstrip, an exact frame,
# and where the talking happens. All three are local and cache-friendly.

_STRIP_COUNT_STEP = 4   # quantize the requested cell count so a resizing
                        # window can't mint a cache entry per pixel.


def _immutable_image(path: Path):
    """A cached bench image. The key carries the source's mtime, which is what
    makes `immutable` safe: a re-downloaded source writes a new file, which is
    a new key, which is a new URL."""
    return FileResponse(
        path, media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


_STRIP_BUILD_LOCKS: dict[str, asyncio.Lock] = {}


@router.get("/downloaded/{video_id}/filmstrip")
async def get_downloaded_filmstrip(
    video_id: str,
    n: int = Query(32, ge=4, le=96),
    h: int = Query(64, ge=24, le=160),
):
    """Serve a horizontal contact sheet of the source, `n` frames wide.

    The Clipper bench stretches this behind its timeline, so cell i covers
    `duration * i / n` .. `duration * (i+1) / n` — scene changes become
    visible before the user drags anything.

    `n` is quantized to a multiple of 4 and the result is cached on disk keyed
    by (video id, file mtime, n, h).
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Downloaded video not found")
    if not video.video_path or not Path(video.video_path).exists():
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    src = Path(video.video_path)
    n = max(4, min(96, round(n / _STRIP_COUNT_STEP) * _STRIP_COUNT_STEP))
    # Apply the density cap HERE too, so the cache key names the strip that
    # actually gets built. extract_filmstrip clamps by duration internally
    # (MIN_CELL_SEC), so on a short source n=96 and n=32 produce the same sheet
    # — and were cached under two keys, each paying its own ffmpeg run. The
    # client reads the true cell count off the DECODED image rather than off
    # its own request, so a clamp here costs it nothing.
    from backend.services.ffmpeg_service import MIN_CELL_SEC
    if video.duration_seconds:
        n = max(4, min(n, int(video.duration_seconds / MIN_CELL_SEC)))
    try:
        mtime = int(src.stat().st_mtime)
    except OSError:
        raise HTTPException(status_code=404, detail="Video file not readable")

    strips = settings.THUMBNAILS_DIR / "strips"
    out = strips / f"{video_id}_{mtime}_{n}x{h}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return _immutable_image(out)

    # Single-flight per cache key: the Timeline strip and SourceBench's
    # denser warm-up sprite can race the same (n,h) after a resize, and an
    # uncached strip build is up to ~96 ffmpeg seeks — two concurrent
    # requests used to each pay the full build. The dict stays small
    # (sources × param combos) and entries are reused, so no eviction.
    lock = _STRIP_BUILD_LOCKS.setdefault(out.name, asyncio.Lock())
    async with lock:
        if out.exists() and out.stat().st_size > 0:
            return _immutable_image(out)
        from backend.services.ffmpeg_service import extract_filmstrip
        made = await extract_filmstrip(
            video_path=src, output_path=out, count=n, tile_height=h,
            duration=video.duration_seconds or None,
        )
    if not made:
        raise HTTPException(status_code=422,
                            detail="Could not build a filmstrip for this video")

    # Drop strips built from a previous copy of this source. Same id, older
    # mtime — nothing will ever request them again.
    try:
        for stale in strips.glob(f"{video_id}_*.jpg"):
            if not stale.name.startswith(f"{video_id}_{mtime}_"):
                stale.unlink(missing_ok=True)
    except OSError:
        pass

    return _immutable_image(out)


@router.get("/downloaded/{video_id}/frame")
async def get_downloaded_frame(
    video_id: str,
    t: float = Query(0.0, ge=0.0),
    w: int = Query(320, ge=64, le=960),
):
    """One frame of the source at `t` seconds — the bench's IN/OUT preview.

    Why this exists instead of two more `<video>` elements seeked to the in and
    out points, which is what the bench shipped with first: a media element
    that is only ever seeked still behaves like a player. Each one holds a
    range response open unconsumed, and a seek that comes back ERR_ABORTED
    leaves the element in `seeking` forever — measured, the OUT preview updated
    twice during a 700ms drag and then froze. An `<img>` has none of that
    machinery.

    Cached per 0.1s bucket per width, keyed by the source's mtime, so dragging
    back over ground you have already covered is a disk read.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Downloaded video not found")
    if not video.video_path or not Path(video.video_path).exists():
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    src = Path(video.video_path)
    try:
        mtime = int(src.stat().st_mtime)
    except OSError:
        raise HTTPException(status_code=404, detail="Video file not readable")

    # Query(ge=0.0) passes `inf` (inf >= 0), and with an unknown duration
    # (fresh import, analyzer not run yet) the tail clamp below is skipped —
    # int(round(inf * 10)) is an OverflowError → naked 500, and a huge finite
    # t spawns a doomed 30s-timeout ffmpeg per request. Bound it regardless.
    if not math.isfinite(t):
        raise HTTPException(status_code=422, detail="t must be a finite number")
    t = min(float(t), 863999.0)              # ~10 days; no real source is longer
    duration = video.duration_seconds or 0
    if duration:
        # Never seek past the last decodable frame — the same tail rule the
        # filmstrip learned the hard way.
        t = min(float(t), max(0.0, duration - 0.25))
    bucket = int(round(float(t) * 10))       # 0.1s granularity
    w = int(round(w / 32) * 32)              # quantize so a resize reuses the cache

    frames = settings.THUMBNAILS_DIR / "frames"
    out = frames / f"{video_id}_{mtime}_{w}_{bucket:07d}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return _immutable_image(out)

    from backend.services.ffmpeg_service import extract_frame_at
    got = await extract_frame_at(
        video_path=src, timestamp=bucket / 10.0, output_path=out,
        quality=4, scale_width=w,
    )
    if not got:
        raise HTTPException(status_code=422, detail="Could not read a frame at that time")
    return _immutable_image(out)


@router.get("/downloaded/{video_id}/segments")
async def get_downloaded_segments(video_id: str, limit: int = Query(1500, ge=1, le=5000)):
    """Timed speech segments for the source, for the bench's speech lane.

    The rows already carry `transcript_segments_json` (Whisper, written by the
    downloader/analyzer) — it just had no way out over REST. Word-level timings
    are stripped here: the lane draws blocks and snaps handles to phrase edges,
    and the `words` array is the bulk of the payload.

    Never 404s on a missing transcript — a source with no speech is a normal
    case, and the bench degrades to a filmstrip-only timeline. Returns
    `{duration, has_segments, truncated, segments: [{start, end, text}]}`.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Downloaded video not found")

    raw = []
    if video.transcript_segments_json:
        try:
            parsed = json.loads(video.transcript_segments_json)
            if isinstance(parsed, list):
                raw = parsed
        except (json.JSONDecodeError, TypeError):
            # Same posture as clip_extractor: a corrupt blob is a missing
            # transcript, not an error page.
            logger.warning("Corrupt transcript_segments_json for %s", video_id[:8])

    segments = []
    for seg in raw:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start"))
            end = float(seg.get("end"))
        except (TypeError, ValueError):
            continue
        if not (end > start):
            continue
        segments.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "text": (seg.get("text") or "").strip(),
        })

    segments.sort(key=lambda x: x["start"])
    truncated = len(segments) > limit
    if truncated:
        segments = segments[:limit]

    return {
        "duration": video.duration_seconds or 0,
        "has_segments": bool(segments),
        "truncated": truncated,
        "segments": segments,
    }


@router.get("/downloaded/{video_id}/stream")
async def stream_downloaded(video_id: str, request: Request):
    """Stream a downloaded video with Range support for seeking.

    Delegates to `videos.serve_media_with_range` — one implementation, one
    place to get an open-ended `bytes=N-` right (rule #32). This route used to
    answer with a plain FileResponse, which is fine for a download and wrong
    for the cutting bench, whose whole interaction is seeking.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == video_id)
        )
        video = result.scalar_one_or_none()
    if not video or not video.video_path:
        raise HTTPException(status_code=404, detail="Video not found")

    # Validate path is within storage directory (prevent path traversal)
    from backend.config import settings as app_settings
    path = Path(video.video_path).resolve()
    if not path.is_relative_to(app_settings.STORAGE_ROOT.resolve()):
        raise HTTPException(status_code=403, detail="Access denied")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    from backend.api.videos import serve_media_with_range
    return serve_media_with_range(path, request, filename=path.name)
