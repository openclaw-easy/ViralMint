# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""REST endpoints for generated videos."""
import asyncio
import io
import json
import logging
import re
import zipfile
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
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
    filters = []
    if status:
        filters.append(GeneratedVideo.status == status)
    if source_type:
        filters.append(GeneratedVideo.source_type == source_type)
    order = (GeneratedVideo.created_at.desc(), GeneratedVideo.id.desc())

    # A row whose rendered file is gone is OMITTED, never deleted: a read must
    # not mutate. This used to delete the row (and unlink its sibling files) on
    # every page load — so a file briefly unreachable (a sync client mid-move,
    # a permissions blip, a path the user renamed back an hour later) cost the
    # user the row for good, and any caller that merely LISTED videos (the
    # Library, Clip Studio, an MCP agent) did the deleting. Deleting stays an
    # explicit act: DELETE /api/videos/{id}.
    #
    # To keep pages full and `total` honest, liveness is decided over the
    # whole filtered set first — only (id, video_path), one stat per row, off
    # the event loop — and offset/limit then index the visible rows. A row with
    # no path yet (draft / failed) is legitimately file-less and stays visible.
    # With the storage root itself absent (external drive asleep), every
    # is_file() is False, so nothing is hidden until storage is back.
    rows = (await db.execute(
        select(GeneratedVideo.id, GeneratedVideo.video_path)
        .where(*filters).order_by(*order)
    )).all()

    from backend.config import settings as _settings

    def _visible_ids() -> list[str]:
        if not _settings.STORAGE_ROOT.exists():
            return [r.id for r in rows]
        return [r.id for r in rows if not r.video_path or Path(r.video_path).is_file()]

    visible = await asyncio.to_thread(_visible_ids)
    if len(visible) < len(rows):
        logger.debug("Videos: %d row(s) hidden — file missing on disk",
                     len(rows) - len(visible))
    total = len(visible)
    page_ids = visible[max(offset, 0): max(offset, 0) + limit]
    by_id = {}
    if page_ids:
        by_id = {v.id: v for v in (await db.execute(
            select(GeneratedVideo).where(GeneratedVideo.id.in_(page_ids))
        )).scalars().all()}
    videos = [by_id[i] for i in page_ids if i in by_id]

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
                "project_id": v.project_id,
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
        # The Smart Video Studio project this render came from — "Edit in studio".
        "project_id": v.project_id,
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

    _drop_video_files(video)

    await db.delete(video)
    await db.commit()
    return {"ok": True}


# Bulk ceilings. Delete is cheap per row; a ZIP is built in memory (mp4s are
# already compressed, so ZIP_STORED — no CPU, but every byte is held), so it is
# capped lower to keep that buffer bounded.
BULK_DELETE_MAX = 200
BULK_EXPORT_MAX = 50


def _video_files(video: GeneratedVideo) -> list[str]:
    """Every on-disk file a generated-video row owns — ONE list, used by the
    single delete and the bulk delete alike, so neither leaks a file the other
    removes (the cached 16:9 export is the one that used to be forgotten)."""
    return [p for p in (video.video_path, video.audio_path, video.thumbnail_path,
                        getattr(video, "video_path_landscape", None)) if p]


def _drop_video_files(video: GeneratedVideo) -> None:
    """Remove every file a generated-video row owns, for the single and the
    bulk delete alike: the files in _video_files, the thumbnail's scaled grid
    copies (thumb_scale) and the Library's extracted poster. A file that can't
    be removed is logged, never fatal — it must not strand the rest."""
    from backend.services.library_index import KEY_GENERATED, drop_poster_cache
    from backend.services.thumb_scale import scaled_siblings
    for path_str in _video_files(video):
        try:
            Path(path_str).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("delete: could not remove %s: %s", path_str, e)
    if video.thumbnail_path:
        for sib in scaled_siblings(Path(video.thumbnail_path)):
            try:
                sib.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("delete: could not remove %s: %s", sib, e)
    drop_poster_cache(f"{KEY_GENERATED}:{video.id}")


def _sanitize_filename(name: str, max_len: int = 60) -> str:
    """A safe archive member name from a video title: drops path separators
    and filesystem-hostile characters, collapses whitespace, truncates.
    Returns "clip" when nothing usable is left."""
    if not name:
        return "clip"
    cleaned = re.sub(r"[^A-Za-z0-9 _\-.()]+", "", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:max_len] or "clip"


def _zip_stream(files: list[tuple[str, Path]], bundle_name: str) -> StreamingResponse:
    """An in-memory ZIP of (arcname, path) pairs as an attachment. Name
    collisions get _2, _3, … in input order (two clips titled alike must not
    overwrite each other inside the archive)."""
    buf = io.BytesIO()
    used: dict[str, int] = {}
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for arcname, path in files:
            if not path.exists():
                continue
            final = arcname
            if arcname in used:
                used[arcname] += 1
                stem, sep, ext = arcname.rpartition(".")
                final = f"{stem}_{used[arcname]}.{ext}" if sep else f"{arcname}_{used[arcname]}"
            else:
                used[arcname] = 1
            zf.write(path, arcname=final)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{bundle_name}.zip"'},
    )


def _id_list(body: dict | None, key: str, cap: int) -> list[str]:
    ids = (body or {}).get(key)
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400, detail=f"{key} must be a non-empty list of ids")
    if len(ids) > cap:
        raise HTTPException(status_code=400, detail=f"At most {cap} at once")
    return list(dict.fromkeys(ids))


@router.post("/videos/bulk-delete")
async def bulk_delete_videos(body: dict = None, db: AsyncSession = Depends(get_db)):
    """Delete several generated videos (the Clipper's "Delete N") and every
    file each one owns. Body: {"clip_ids": [...]}. Returns {"ok", "deleted"}.
    Ids that match no row are skipped, not an error — the rest still go."""
    ids = _id_list(body, "clip_ids", BULK_DELETE_MAX)
    videos = (await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id.in_(ids))
    )).scalars().all()
    if not videos:
        raise HTTPException(status_code=404, detail="No matching clips found")
    for video in videos:
        _drop_video_files(video)
        await db.delete(video)
    await db.commit()
    return {"ok": True, "deleted": len(videos)}


@router.post("/videos/bulk-export")
async def bulk_export_videos(body: dict = None, db: AsyncSession = Depends(get_db)):
    """Several generated videos as one ZIP (the Clipper's "Download ZIP").

    Body: {"video_ids": [...], "include_thumbnails": false}. Members are
    `{title}.mp4` (+ `{title}.jpg`). Rows whose file is missing or outside the
    storage dir are skipped; 404 when nothing at all is left to zip.
    """
    ids = _id_list(body, "video_ids", BULK_EXPORT_MAX)
    include_thumbs = bool((body or {}).get("include_thumbnails"))
    videos = (await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id.in_(ids))
    )).scalars().all()
    if not videos:
        raise HTTPException(status_code=404, detail="No matching videos found")

    files: list[tuple[str, Path]] = []
    for v in videos:
        if not v.video_path:
            continue
        try:
            path = _safe_resolve_path(v.video_path)
        except HTTPException:
            continue
        if not path.exists():
            continue
        base = _sanitize_filename(v.title or f"clip_{v.id[:8]}")
        files.append((f"{base}.mp4", path))
        if include_thumbs and v.thumbnail_path:
            try:
                thumb = _safe_resolve_path(v.thumbnail_path)
            except HTTPException:
                continue
            if thumb.exists():
                files.append((f"{base}.jpg", thumb))
    if not files:
        raise HTTPException(status_code=404, detail="No video files found on disk")
    n_videos = sum(1 for name, _ in files if name.endswith(".mp4"))
    return await asyncio.to_thread(_zip_stream, files, f"viralmint-clips-{n_videos}")


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
    ".ogg": "audio/ogg", ".flac": "audio/flac", ".aac": "audio/aac",
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
        # 410, typed: the row exists, its file does not. The Clipper's
        # inspector keys on the header to say "this clip's file is gone"
        # instead of showing a black player.
        raise HTTPException(status_code=410, detail="Video file not found on disk",
                            headers={"X-ViralMint-Error": "clip_file_missing",
                                     # a cached 410 outlived a restored file
                                     "Cache-Control": "no-store"})

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


def thumbnail_cache_headers(versioned: bool) -> dict[str, str]:
    """Cache policy for a thumbnail response.

    With `?v=<mtime>` in the URL (the Library index stamps it) the response can
    be cached forever: a regenerated thumbnail has a new mtime, hence a new URL.
    Without it the browser revalidated on every scroll-into-view, so a long
    grid reloaded its cards as you scrolled.
    """
    if versioned:
        return {"Cache-Control": "public, max-age=31536000, immutable"}
    return {"Cache-Control": "public, max-age=60, must-revalidate"}


@router.get("/videos/{video_id}/thumbnail")
async def get_thumbnail(
    video_id: str,
    v: str | None = Query(None, description="Cache-busting version (the file's mtime)"),
    w: int | None = Query(None, description="Grid width — see thumb_scale.ALLOWED_WIDTHS"),
    db: AsyncSession = Depends(get_db),
):
    """Serve the thumbnail image.

    `?w=` serves a downscaled copy. The stored thumbnail is as large as the
    render (1920x1080 for a 16:9 output): cheap on the wire, but ~7.9 MB of
    BITMAP once decoded, and a Library page draws dozens of them into 140px
    boxes. The browser cannot hold that, so it evicts and re-decodes on scroll —
    which is what made tiles go black and fill back in.
    """
    result = await db.execute(
        select(GeneratedVideo).where(GeneratedVideo.id == video_id)
    )
    video = result.scalar_one_or_none()
    if not video or not video.thumbnail_path:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    path = Path(video.thumbnail_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail file not found on disk")

    from backend.services import thumb_scale
    path = await thumb_scale.scaled_thumbnail(path, thumb_scale.normalize_width(w))
    # Typed from the file actually served: a scaled copy keeps PNG when its
    # source has alpha.
    return FileResponse(path, media_type=thumb_scale.media_type(path),
                        headers=thumbnail_cache_headers(bool(v)))


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
