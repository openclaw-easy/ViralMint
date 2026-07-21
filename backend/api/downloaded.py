# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""REST /api/downloaded — downloaded & analyzed competitor videos."""
import json
import logging
import shutil
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
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
    Also cleans up orphaned audio files."""
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
    Body: { "urls": [{"url": "...", "title": "..."}, ...] }
    Returns: { "job_id": "uuid", "count": N }
    """
    body = body or {}
    urls = body.get("urls", [])
    if not urls:
        raise HTTPException(status_code=400, detail="No URLs provided")
    if len(urls) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 videos per batch")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import run_batch_download_urls, dispatch

    job = await create_job("download", "local", {
        "batch_urls": [u.get("url", "") for u in urls],
        "count": len(urls),
    })
    dispatch(run_batch_download_urls(
        job_id=job.id,
        urls=urls,
        user_id="local",
    ))
    return {"job_id": job.id, "count": len(urls)}


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


@router.post("/downloaded/{video_id}/extract-clips")
async def extract_clips(video_id: str, body: dict = None):
    """Extract viral clips from a downloaded long-form video.

    Body params:
        max_clips: int (1-50, default 3)
        caption_style: str (viral|classic|bold, default viral)
        min_duration: int (min clip seconds, optional)
        max_duration: int (max clip seconds, optional)
    """
    body = body or {}
    caption_style = body.get("caption_style", "viral")
    whisper_quality = body.get("whisper_quality", "balanced")
    force_retranscribe = body.get("force_retranscribe", False)
    min_duration = body.get("min_duration")
    max_duration = body.get("max_duration")
    # max_clips: if not specified, auto-calculate from video duration (~1 per 30s, min 3)
    max_clips_input = body.get("max_clips")
    max_clips = None  # will be computed after loading video

    # Validate custom duration range
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

    # Auto-calculate max_clips from duration if not specified (~1 clip per 30s, min 3, max 99)
    if max_clips_input is not None:
        max_clips = max(1, min(int(max_clips_input), 99))
    else:
        max_clips = max(3, min(99, duration // 30)) if duration > 0 else 5

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import run_extract_clips, dispatch
    from backend.core.ws_manager import ws_manager

    job = await create_job("generate", "local", {
        "downloaded_video_id": video_id,
        "type": "clip_extraction",
        "max_clips": max_clips,
    })
    dispatch(run_extract_clips(
        job_id=job.id,
        downloaded_video_id=video_id,
        max_clips=max_clips,
        caption_style=caption_style,
        whisper_quality=whisper_quality,
        force_retranscribe=force_retranscribe,
        min_duration=min_duration,
        max_duration=max_duration,
        user_id="local",
    ))
    video_title = video.title or "Untitled"
    await ws_manager.send({
        "type": "job_started",
        "job_id": job.id,
        "job_type": "generate",
        "message": f"Extracting clips from: {video_title}",
        "input_data": {"type": "clip_extraction", "downloaded_video_id": video_id, "max_clips": max_clips},
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


@router.get("/downloaded/{video_id}/stream")
async def stream_downloaded(video_id: str):
    """Stream/serve a downloaded video file."""
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

    return FileResponse(path, media_type="video/mp4")
