# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""REST endpoints for generated videos."""
import json
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.generated_video import GeneratedVideo
from backend.core.exceptions import safe_json_loads as _safe_json

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/videos")
async def list_videos(
    status: str = Query(None),
    source_type: str = Query(None),
    limit: int = Query(20, le=100),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    """List generated videos with optional status / source_type filters.

    `source_type` filters server-side (e.g. "clip_extraction" for Clip
    Studio). Clip Studio used to pull an unfiltered page of 100 and filter in
    the browser, so a library with enough recent non-clip rows rendered
    "No clips yet" over a library full of clips.
    """
    base_query = select(GeneratedVideo).order_by(GeneratedVideo.created_at.desc())
    if status:
        base_query = base_query.where(GeneratedVideo.status == status)
    if source_type:
        base_query = base_query.where(GeneratedVideo.source_type == source_type)

    # Self-heal + BACKFILL: prune rows whose rendered file is gone, then
    # re-query so the caller still gets a full page.
    #
    # Only rows that HAD a file (video_path set) but it's now missing on disk —
    # a draft/failed row with no path yet is legitimately file-less and left
    # alone. Local is_file() is authoritative so the delete is safe; it stops
    # broken/dead tiles from lingering in the Library forever.
    #
    # Why the loop: pruning used to happen *within* the fetched page and the
    # page was returned short. With enough stale rows the whole page could be
    # pruned, so a limit-3 request answered `videos: []` next to `total: 431` —
    # which reads as a bug to any caller and gives it no way forward. Deleting
    # rows inside [offset, offset+limit) shifts later rows INTO that window, so
    # re-running the same offset/limit query is the correct backfill.
    live: list = []
    pruned = 0
    passes = 0
    _MAX_PASSES = 5  # bounds the work when a whole tail of rows is orphaned
    # Distinguish a missing FILE from a missing VOLUME: with the storage
    # root itself absent (external drive asleep/unmounted, VIRALMINT_DATA_DIR
    # on removable media), every row's is_file() is False and this prune
    # would delete the user's entire video table + sibling files on a single
    # page load. Skip pruning entirely until storage is back; rows render as
    # they are and the next healthy request prunes true orphans.
    from backend.config import settings as _settings
    _storage_present = _settings.STORAGE_ROOT.exists()
    while True:
        passes += 1
        # Rows already kept occupy [offset, offset + len(live)) in the CURRENT
        # (post-delete) ordering, because deletions were all inside the window
        # and rows before `offset` are untouched. So the next slice starts
        # after them — re-using the original `offset` on a later pass would
        # re-fetch what `live` already holds and duplicate it in the response.
        result = await db.execute(
            base_query.offset(offset + len(live)).limit(limit - len(live)))
        page = result.scalars().all()
        if not page:
            break
        pruned_this_pass = 0
        for v in page:
            if _storage_present and v.video_path and not Path(v.video_path).is_file():
                try:
                    for ps in (v.video_path, v.audio_path,
                               v.thumbnail_path, v.video_path_landscape):
                        if ps:
                            pp = Path(ps)
                            if pp.exists():
                                pp.unlink(missing_ok=True)
                    await db.delete(v)
                    pruned += 1
                    pruned_this_pass += 1
                    continue
                except Exception:
                    logger.warning("Could not prune orphaned video row %s", v.id)
            live.append(v)
        if pruned_this_pass:
            # Commit so the next pass's OFFSET sees the shrunk table (and so
            # `total` below reflects the pruned set).
            await db.commit()
        if len(live) >= limit or not pruned_this_pass:
            break
        if passes >= _MAX_PASSES:
            logger.warning(
                "Videos: stopped backfilling after %d passes (%d pruned, page "
                "has %d/%d) — many orphaned rows in this range",
                passes, pruned, len(live), limit)
            break
    if pruned:
        logger.info("Videos: pruned %d orphaned row(s) with missing files", pruned)
    videos = live

    from sqlalchemy import func
    count_query = select(func.count(GeneratedVideo.id))
    if status:
        count_query = count_query.where(GeneratedVideo.status == status)
    if source_type:
        count_query = count_query.where(GeneratedVideo.source_type == source_type)
    total = (await db.execute(count_query)).scalar()

    return {
        "total": total,
        "videos": [
            {
                "id": v.id,
                "title": v.title,
                "niche": v.niche,
                "status": v.status,
                "aspect_ratio": v.aspect_ratio,
                "gen_tier": v.gen_tier,
                "video_path": v.video_path,
                "thumbnail_path": v.thumbnail_path,
                "youtube_title": v.youtube_title,
                "youtube_description": v.youtube_description,
                "youtube_tags": _safe_json(v.youtube_tags_json, []),
                "tiktok_title": v.tiktok_title,
                "tiktok_description": v.tiktok_description,
                "youtube_video_id": v.youtube_video_id,
                "tiktok_publish_id": v.tiktok_publish_id,
                "uploaded_platforms": _safe_json(v.uploaded_platforms_json, []),
                "source_type": v.source_type,
                "source_downloaded_video_id": v.source_downloaded_video_id,
                "clip_start_seconds": v.clip_start_seconds,
                "clip_end_seconds": v.clip_end_seconds,
                "clip_virality_score": v.clip_virality_score,
                "clip_hook_score": v.clip_hook_score,
                "clip_hook_type": v.clip_hook_type,
                "clip_virality_reason": v.clip_virality_reason,
                "clip_score_breakdown_json": v.clip_score_breakdown_json,
                "caption_status": v.caption_status,
                "clip_selection": v.clip_selection,
                "metadata_status": v.metadata_status,
                "duration_seconds": v.duration_seconds,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in videos
        ],
    }


@router.get("/videos/{video_id}")
async def get_video(video_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single generated video by ID."""
    result = await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id == video_id)
    )
    v = result.scalar_one_or_none()
    if not v:
        raise HTTPException(status_code=404, detail="Video not found")

    return {
        "id": v.id,
        "title": v.title,
        "script": v.script,
        "niche": v.niche,
        "status": v.status,
        "aspect_ratio": v.aspect_ratio,
        "gen_tier": v.gen_tier,
        "video_path": v.video_path,
        "audio_path": v.audio_path,
        "thumbnail_path": v.thumbnail_path,
        "duration_seconds": v.duration_seconds,
        "voice_id": v.voice_id,
        "youtube_title": v.youtube_title,
        "youtube_description": v.youtube_description,
        "youtube_tags": _safe_json(v.youtube_tags_json, []),
        "tiktok_title": v.tiktok_title,
        "tiktok_description": v.tiktok_description,
        "youtube_video_id": v.youtube_video_id,
        "tiktok_publish_id": v.tiktok_publish_id,
        "uploaded_platforms": _safe_json(v.uploaded_platforms_json, []),
        "estimated_cost_usd": v.estimated_cost_usd,
        "video_path_landscape": v.video_path_landscape,
        "has_landscape": bool(v.video_path_landscape),
        "source_type": v.source_type,
        "source_downloaded_video_id": v.source_downloaded_video_id,
        "clip_start_seconds": v.clip_start_seconds,
        "clip_end_seconds": v.clip_end_seconds,
        "clip_virality_score": v.clip_virality_score,
        "clip_hook_score": v.clip_hook_score,
        "clip_hook_type": v.clip_hook_type,
        "clip_virality_reason": v.clip_virality_reason,
        "clip_score_breakdown_json": v.clip_score_breakdown_json,
        "caption_status": v.caption_status,
        "clip_selection": v.clip_selection,
        "metadata_status": v.metadata_status,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


@router.post("/videos/generate")
async def generate_video(body: dict = None):
    """Generate a video from scratch (custom script, no source video)."""
    body = body or {}

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import run_generate, dispatch

    job = await create_job("generate", "local", body)
    dispatch(run_generate(
        job_id=job.id,
        downloaded_video_id=None,
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


@router.post("/videos/{video_id}/upload")
async def upload_video(video_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    """Trigger upload of a generated video to specified platforms."""
    result = await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id == video_id)
    )
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status not in ("ready", "failed"):
        raise HTTPException(status_code=400, detail=f"Video status is '{video.status}', must be 'ready' or 'failed'")

    platforms = body.get("platforms", ["youtube"])
    user_id = video.user_id or "local"

    from backend.agents.job_helper import create_job
    job = await create_job("upload", user_id, {
        "generated_video_id": video_id,
        "platforms": platforms,
    })

    from backend.core.task_runner import run_upload as task_upload, dispatch
    dispatch(task_upload(
        job_id=job.id, generated_video_id=video_id, platforms=platforms, user_id=user_id,
    ))

    return {"job_id": job.id}


@router.patch("/videos/{video_id}")
async def edit_video(video_id: str, body: dict = None, db: AsyncSession = Depends(get_db)):
    """
    Edit generated video metadata after generation.
    Editable fields: title, script, youtube_title, youtube_description,
    youtube_tags, tiktok_title, tiktok_description.
    """
    body = body or {}
    result = await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id == video_id)
    )
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    EDITABLE_FIELDS = {
        "title", "script", "youtube_title", "youtube_description",
        "tiktok_title", "tiktok_description",
    }

    updated = []
    for field in EDITABLE_FIELDS:
        if field in body:
            setattr(video, field, body[field])
            updated.append(field)

    # youtube_tags is stored as JSON
    if "youtube_tags" in body:
        tags = body["youtube_tags"]
        if isinstance(tags, list):
            video.youtube_tags_json = json.dumps(tags)
        elif isinstance(tags, str):
            video.youtube_tags_json = json.dumps([t.strip() for t in tags.split(",") if t.strip()])
        updated.append("youtube_tags")

    if not updated:
        raise HTTPException(status_code=400, detail="No editable fields provided")

    await db.commit()
    await db.refresh(video)

    return {
        "id": video.id,
        "updated_fields": updated,
        "title": video.title,
        "script": video.script,
        "youtube_title": video.youtube_title,
        "youtube_description": video.youtube_description,
        "youtube_tags": _safe_json(video.youtube_tags_json, []),
        "tiktok_title": video.tiktok_title,
        "tiktok_description": video.tiktok_description,
    }


@router.post("/videos/{video_id}/regenerate-thumbnail")
async def regenerate_thumbnail(video_id: str, db: AsyncSession = Depends(get_db)):
    """Regenerate the AI thumbnail for a generated video. Runs inline (2-5s)."""
    result = await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id == video_id)
    )
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if not video.video_path or not Path(video.video_path).exists():
        raise HTTPException(status_code=400, detail="Video file not found on disk")

    from backend.models.user_settings import UserSettings
    from backend.database import AsyncSessionLocal
    us_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == (video.user_id or "local"))
    )
    user_settings = us_result.scalar_one_or_none()

    from backend.services.thumbnail_service import generate_ai_thumbnail
    thumb_path = await generate_ai_thumbnail(
        video_path=video.video_path,
        script=video.script or "",
        title=video.youtube_title or video.title or "",
        user_settings=user_settings,
    )

    video.thumbnail_path = str(thumb_path)
    await db.commit()

    return {"ok": True, "thumbnail_path": str(thumb_path)}


@router.delete("/videos/{video_id}")
async def delete_video(video_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a generated video and its files."""
    result = await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id == video_id)
    )
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Clean up files
    for path_str in [video.video_path, video.audio_path, video.thumbnail_path]:
        if path_str:
            p = Path(path_str)
            if p.exists():
                p.unlink(missing_ok=True)

    await db.delete(video)
    await db.commit()
    return {"ok": True}


def _safe_resolve_path(path_str: str) -> Path:
    """Resolve a file path and verify it's within the storage directory. Prevents path traversal."""
    from backend.config import settings
    storage_root = settings.STORAGE_ROOT.resolve()
    resolved = Path(path_str).resolve()
    if not resolved.is_relative_to(storage_root):
        raise HTTPException(status_code=403, detail="Access denied: path outside storage directory")
    return resolved


# MIME by suffix. This helper also serves Library image and audio assets, so
# it cannot assume video/mp4 — a .png streamed as application/octet-stream is
# rejected by anything that checks `blob.type`.
_MEDIA_TYPES = {
    ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".flac": "audio/flac",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
    # Tool outputs are not all media: subtitle exports, transcripts and
    # metadata dumps come through the same Library asset route.
    ".srt": "text/plain", ".vtt": "text/vtt", ".txt": "text/plain",
    ".json": "application/json",
}


def serve_media_with_range(path: Path, request, filename: str | None = None):
    """Serve a media file with HTTP Range support, bounding open-ended ranges.

    Every scrubbing surface goes through here — the Clipper bench's player,
    the Library players, clip previews. The header parsing (and the reason an
    open-ended `bytes=N-` must NOT be answered to EOF) lives in
    `core.http_range`, which is the only place that decision is made.

    `request` is the Starlette Request; it is untyped here so callers that
    only have the header do not have to import it.
    """
    from starlette.responses import StreamingResponse

    from backend.core.http_range import RangeNotSatisfiable, parse_range

    file_size = path.stat().st_size
    media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")

    try:
        rng = parse_range(request.headers.get("range"), file_size)
    except RangeNotSatisfiable as e:
        raise HTTPException(status_code=416, detail=e.detail, headers=e.headers)

    if rng is not None:
        start, end = rng
        content_length = end - start + 1

        def iter_file():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    data = f.read(min(65536, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            iter_file(),
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
            },
        )

    # No Range at all (a download, a fetch) — FileResponse uses sendfile and
    # never touches the worker pool, so it stays uncapped.
    return FileResponse(
        path, media_type=media_type, filename=filename,
        headers={"Accept-Ranges": "bytes"},
    )


@router.get("/videos/{video_id}/stream")
async def stream_video(video_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Stream/serve the video file, with Range support for seeking."""
    result = await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id == video_id)
    )
    video = result.scalar_one_or_none()
    if not video or not video.video_path:
        raise HTTPException(status_code=404, detail="Video not found")

    path = _safe_resolve_path(video.video_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    return serve_media_with_range(path, request, filename=f"{video.title or 'video'}.mp4")


@router.post("/videos/{video_id}/export")
async def export_video(video_id: str, body: dict = None, db: AsyncSession = Depends(get_db)):
    """
    Export a generated video to a different aspect ratio.
    Body: { "target_aspect": "16:9", "method": "auto" }
    Supported aspects: 9:16, 16:9, 1:1, 4:5
    Methods: auto, letterbox, crop, blur_fill
    """
    body = body or {}
    target_aspect = body.get("target_aspect", "16:9")
    # "auto" picks crop when WIDENING and blur_fill when narrowing —
    # fitting a 9:16 short inside 16:9 left the content a narrow strip at a
    # third of the frame (see ffmpeg_service.pick_reframe_method). An
    # explicit method from the caller still wins.
    method = body.get("method", "auto")

    if target_aspect not in ("9:16", "16:9", "1:1", "4:5"):
        raise HTTPException(status_code=400, detail=f"Unsupported aspect ratio: {target_aspect}")
    if method not in ("auto", "letterbox", "crop", "blur_fill"):
        raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")

    result = await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id == video_id)
    )
    video = result.scalar_one_or_none()
    if not video or not video.video_path:
        raise HTTPException(status_code=404, detail="Video not found")

    source_path = _safe_resolve_path(video.video_path)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    # Skip conversion if already in the target aspect ratio
    if video.aspect_ratio == target_aspect:
        return FileResponse(source_path, media_type="video/mp4",
                            filename=f"{video.title or 'video'}_{target_aspect.replace(':', 'x')}.mp4")

    # Reuse a cached landscape export if one is already on disk
    if target_aspect == "16:9" and video.video_path_landscape:
        cached = _safe_resolve_path(video.video_path_landscape)
        if cached.exists():
            return FileResponse(cached, media_type="video/mp4",
                                filename=f"{video.title or 'video'}_16x9.mp4")

    from backend.services.ffmpeg_service import convert_aspect_ratio
    try:
        output_path = await convert_aspect_ratio(source_path, target_aspect=target_aspect, method=method)
        # Cache the landscape export so stream_video_format can serve it
        # without re-converting (this is what the duplicate, always-shadowed
        # export_video_format route used to do before it was removed).
        if target_aspect == "16:9":
            video.video_path_landscape = str(output_path)
            await db.commit()
        return FileResponse(output_path, media_type="video/mp4",
                            filename=f"{video.title or 'video'}_{target_aspect.replace(':', 'x')}.mp4")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.get("/videos/{video_id}/performance")
async def get_video_performance(video_id: str, db: AsyncSession = Depends(get_db)):
    """Get performance metrics history for an uploaded video."""
    result = await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id == video_id)
    )
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    from backend.services.performance_tracker import get_video_performance as get_perf
    return await get_perf(video_id)


@router.get("/videos/performance/summary")
async def performance_summary():
    """Get aggregate performance stats across all uploaded videos."""
    from backend.services.performance_tracker import get_performance_summary
    return await get_performance_summary()


@router.get("/videos/performance/optimal-time")
async def optimal_posting_time(platform: str = Query("youtube")):
    """Recommend the best time to post based on historical performance data."""
    if platform not in ("youtube", "tiktok", "instagram"):
        raise HTTPException(status_code=400, detail="Platform must be youtube, tiktok, or instagram")
    from backend.services.performance_tracker import recommend_posting_time
    return await recommend_posting_time(platform=platform)


@router.get("/videos/{video_id}/thumbnail")
async def get_thumbnail(video_id: str, db: AsyncSession = Depends(get_db)):
    """Serve the thumbnail image."""
    result = await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id == video_id)
    )
    video = result.scalar_one_or_none()
    if not video or not video.thumbnail_path:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    path = Path(video.thumbnail_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail file not found on disk")

    return FileResponse(path, media_type="image/jpeg")


# NOTE: a second `@router.post("/videos/{video_id}/export")` handler
# (`export_video_format`, returning a JSON path instead of the file) used to
# live here. FastAPI matches the FIRST registered route, so it was dead code
# from the day it was added — every request went to `export_video` above. Its
# one useful behaviour, caching the 16:9 render into `video_path_landscape`,
# now lives there. Do not re-add a duplicate path.


@router.get("/videos/{video_id}/stream/{aspect}")
async def stream_video_format(video_id: str, aspect: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Stream a specific aspect ratio version. aspect: 'original' or '16x9'."""
    result = await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id == video_id)
    )
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    if aspect == "16x9" and video.video_path_landscape:
        path = _safe_resolve_path(video.video_path_landscape)
    elif video.video_path:
        path = _safe_resolve_path(video.video_path)
    else:
        path = None

    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")

    return serve_media_with_range(
        path, request, filename=f"{video.title or 'video'}_{aspect}.mp4")
