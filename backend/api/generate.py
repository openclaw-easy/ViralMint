# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
REST endpoints for video generation.

POST /api/generate/stock          — Stock footage (Pexels or Pixabay)
POST /api/generate/split-scenes   — AI-split script into scenes with stock keywords
POST /api/generate/motion/studio/* — the embedded Motion Graphics studio
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.downloaded_video import DownloadedVideo
from backend.core.prompt_rules import extract_json_text

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/generate", tags=["generate"])


# ── Request schemas ────────────────────────────────────────────────────────────

class StockScene(BaseModel):
    text: str
    keywords: list[str] = []


class StockGenerateRequest(BaseModel):
    script: str
    aspect_ratio: str = "9:16"
    visual_style: Optional[str] = None
    transition_style: Optional[str] = None
    tts_provider: str = "edge_tts"
    tts_voice: Optional[str] = None
    caption_enabled: bool = True
    caption_style: str = "viral"
    music_enabled: bool = True
    music_genre: str = "lofi"
    source_id: Optional[str] = None
    scenes: Optional[list[StockScene]] = None
    start_image: Optional[str] = None
    # The user's own stills. Each claims a scene in order from the hook; the
    # rest of the scenes still come from stock footage. Bounded so one request cannot
    # queue an unbounded number of renders — the scene grid caps at 12 anyway.
    user_images: list[str] = Field(default_factory=list, max_length=12)


class SplitScenesRequest(BaseModel):
    script: str
    aspect_ratio: str = "9:16"
    source_id: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _validate_source(source_id: Optional[str]) -> None:
    """Raise 404 if source_id is provided but doesn't exist."""
    if not source_id:
        return
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DownloadedVideo).where(DownloadedVideo.id == source_id)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Source video not found")


async def _dispatch_generate(*, source_id, custom_script, **kwargs) -> dict:
    """Create job and dispatch to the GeneratorAgent pipeline."""
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import run_generate, dispatch

    job_input = {"source_id": source_id} if source_id else {}
    job = await create_job("generate", "local", job_input)

    dispatch(run_generate(
        job_id=job.id,
        downloaded_video_id=source_id,
        user_id="local",
        custom_script=custom_script,
        **kwargs,
    ))
    return {"job_id": job.id}


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/stock")
async def generate_stock(body: StockGenerateRequest):
    """Generate a video using Pexels stock footage."""
    await _validate_source(body.source_id)
    from backend.services.caption_service import style_id_known, unknown_style_message
    if body.caption_enabled and not await style_id_known(body.caption_style):
        raise HTTPException(status_code=400, detail=unknown_style_message(body.caption_style))

    return await _dispatch_generate(
        source_id=body.source_id,
        custom_script=body.script,
        aspect_ratio=body.aspect_ratio,
        visual_style=body.visual_style,
        transition_style=body.transition_style,
        tts_provider=body.tts_provider,
        tts_voice=body.tts_voice,
        caption_enabled=body.caption_enabled,
        caption_style=body.caption_style,
        music_enabled=body.music_enabled,
        music_genre=body.music_genre,
        start_image=body.start_image,
        user_images=body.user_images,
    )


@router.post("/split-scenes")
async def split_scenes(body: SplitScenesRequest):
    """AI-split a script into stock-footage scenes with per-scene search keywords."""
    script = body.script.strip()
    if not script:
        raise HTTPException(400, "Script is empty")

    from backend.models.user_settings import UserSettings
    from backend.core.ai_provider import get_ai_client

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(UserSettings).limit(1))
        user_settings = result.scalar_one_or_none()

    try:
        ai_client = get_ai_client(user_settings)
    except Exception:
        return _fallback_split(script)

    # Build context from source video if available
    source_context = ""
    if body.source_id:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DownloadedVideo).where(DownloadedVideo.id == body.source_id)
            )
            source = result.scalar_one_or_none()
            if source and source.insights_json:
                source_context = f"\nSource video insights: {source.insights_json[:2000]}"

    system = (
        "You split video scripts into scenes for stock footage matching. "
        "For each scene, extract 2-4 stock footage search keywords that describe "
        "the visual content needed. Keywords are always in English (stock "
        "sites are searched in English); narration text stays verbatim. "
        "Return JSON only."
    )
    prompt = (
        f"Split this script into 4-8 scenes. For each scene, provide the narration text "
        f"and 2-4 stock footage search keywords.\n\n"
        f"Script:\n{script}\n{source_context}\n\n"
        f"Return JSON array: [{{\"text\": \"narration...\", \"keywords\": [\"keyword1\", \"keyword2\"]}}]"
    )

    try:
        response = await ai_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system=system,
            max_tokens=2048,
        )
        text = response.strip()
        text = extract_json_text(text)  # fence anywhere / leading prose (prompt_rules)
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        scenes = json.loads(text.strip())
        if not isinstance(scenes, list) or not scenes:
            return _fallback_split(script)
        return {"scenes": scenes}
    except Exception as e:
        logger.warning(f"AI scene splitting failed, using fallback: {e}")
        return _fallback_split(script)


def _fallback_split(script: str) -> dict:
    """Simple word-count-based split when AI is unavailable."""
    words = script.split()
    words_per_scene = 25  # ~10 seconds at 150 wpm
    scenes = []
    for i in range(0, len(words), words_per_scene):
        chunk = " ".join(words[i:i + words_per_scene])
        keywords = [w.lower().strip(".,!?") for w in chunk.split()[:4] if len(w) > 3]
        scenes.append({"text": chunk, "keywords": keywords[:3]})
    return {"scenes": scenes[:8]}


# ── Motion Graphics studio ────────────────────────────────────────────────────
# The engine is an on-demand plugin, so EVERY endpoint below pre-flights the
# install and answers with a structured envelope rather than a 500. "This needs
# a one-click install" is a state the UI can act on, not an error.

def _studio_gate():
    """Is the Motion Graphics plugin installed? Returns the install envelope
    when it isn't, or None to proceed. The envelope's single source is the
    exception class, so the API and the service layer can't drift on wording."""
    from backend.core.exceptions import HyperFramesNotInstalledError
    from backend.services.hyperframes_service import HyperFramesService
    if not HyperFramesService.is_installed():
        return HyperFramesNotInstalledError.ENVELOPE.copy()
    return None


@router.post("/motion/studio/start")
async def motion_studio_start(mode: str = "dark", restart: bool = False):
    """Start (idempotently) the embedded studio, themed for the app's `mode`.

    The theme is re-injected on every call so switching the app between light
    and dark re-skins the studio too. `restart=true` forces a clean restart,
    which anything that writes the composition behind the studio's back needs —
    a reused preview client can otherwise keep showing the previous one.
    → {ok, port, mode}.
    """
    gate = _studio_gate()
    if gate:
        return gate
    from backend.services.studio_service import StudioService
    try:
        return await StudioService.ensure_running(mode=mode, force=restart)
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Could not start studio: {str(e)[:160]}")


@router.post("/motion/studio/stop")
async def motion_studio_stop():
    """Stop the preview server. Safe to call when nothing is running."""
    from backend.services.studio_service import StudioService
    return await StudioService.stop()


@router.post("/motion/studio/sync-renders")
async def motion_studio_sync_renders():
    """Import any new studio-exported MP4s into the Library.

    Polled by the studio page while it is open, so an Export shows up in the
    Library without the user having to do anything. Never raises: a failed
    import is retried by the next poll, and an error toast on a background
    sync would be noise. → {imported, ids}.
    """
    gate = _studio_gate()
    if gate:
        return {"imported": 0, "ids": []}
    from backend.services.studio_service import StudioService
    try:
        return await StudioService.import_renders()
    except Exception as e:
        logger.warning("studio sync-renders failed: %s", e)
        return {"imported": 0, "ids": []}


@router.get("/motion/studio/comps")
async def motion_studio_list_comps():
    """Previous compositions — every composition replaced by a newer one is
    archived rather than overwritten. → {comps: [{file, size, modified}]}."""
    gate = _studio_gate()
    if gate:
        return {"comps": []}
    from backend.services.studio_service import StudioService
    return {"comps": await asyncio.to_thread(StudioService.list_comps)}


class MotionCleanupRequest(BaseModel):
    """`files` omitted → clear ALL archived compositions plus studio exports
    already imported into the Library. `files` given → delete just those."""
    files: Optional[list] = None


@router.post("/motion/studio/comps/cleanup")
async def motion_studio_cleanup(body: MotionCleanupRequest):
    gate = _studio_gate()
    if gate:
        return {"removed": [], "freed_mb": 0}
    from backend.services.studio_service import StudioService
    return await asyncio.to_thread(StudioService.cleanup_comps, body.files)


@router.post("/motion/studio/assets")
async def motion_studio_stage_asset(file: UploadFile = File(...)):
    """Stage an image, video or audio file into the studio project's assets/,
    so a composition can build it in. → {file, type, duration}."""
    gate = _studio_gate()
    if gate:
        return gate
    from backend.api.tools import read_upload_capped
    from backend.services.studio_service import StudioService
    # A capped read, not `await file.read()`: the cap is 200 MB and a user can
    # drop a multi-gigabyte video into a file picker. Reading it whole to then
    # reject it on size would already have cost the memory.
    data = await read_upload_capped(file, StudioService.ASSET_MAX_BYTES, "Asset")
    try:
        return await asyncio.to_thread(
            StudioService.stage_asset, file.filename or "file", data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class MotionSubmitRequest(BaseModel):
    """Set an externally-authored composition live in the studio."""
    html: str
    render: bool = False
    aspect_ratio: str = "9:16"
    title: Optional[str] = None


@router.post("/motion/studio/submit")
async def motion_studio_submit(body: MotionSubmitRequest):
    """Validate a composition, set it live, and optionally render it.

    Validation failures come back as `{ok: false, issues: [...]}` rather than an
    error, so whoever wrote the HTML — a person or a tool — can fix the named
    problems and resubmit. That is a deterministic loop; silently repairing
    someone's composition is not. → {ok, file, archived, issues, job_id?}.
    """
    gate = _studio_gate()
    if gate:
        return gate
    from backend.agents.generator_motion import ASPECT_DIMS
    if body.aspect_ratio not in ASPECT_DIMS:
        raise HTTPException(status_code=400,
                            detail=f"Unsupported aspect {body.aspect_ratio}")
    html = (body.html or "").strip()
    if not html or "<html" not in html.lower():
        raise HTTPException(status_code=400, detail="A complete HTML document is required")
    if len(html.encode()) > 1_000_000:
        raise HTTPException(status_code=400, detail="Composition too large (max 1 MB)")

    from backend.services.studio_service import StudioService
    issues = await StudioService._all_issues(html, StudioService.project_dir() / "assets")
    if issues:
        return {"ok": False, "issues": issues,
                "hint": "Fix these contract violations and resubmit the complete HTML."}
    result = await StudioService.import_composition(html)
    resp = {"ok": True, "issues": [], **result}
    if body.render:
        resp["job_id"] = await _dispatch_studio_render(
            body.aspect_ratio, body.title, quality="high")
    return resp


class MotionExportRequest(BaseModel):
    aspect_ratio: str = "9:16"
    title: Optional[str] = None
    # "high" renders at 4x device-pixel ratio and downscales, so type and
    # vectors re-rasterize sharper; "standard" renders at 1080p and is faster.
    quality: Literal["standard", "high"] = "high"


@router.post("/motion/studio/export")
async def motion_studio_export(body: MotionExportRequest):
    """Render the live studio composition to MP4 and put it in the Library.

    The studio has its own Export; this is the same thing reachable from
    ViralMint's side, so a composition can be exported without leaving the
    page's own controls. → {job_id}.
    """
    gate = _studio_gate()
    if gate:
        return gate
    from backend.agents.generator_motion import ASPECT_DIMS
    if body.aspect_ratio not in ASPECT_DIMS:
        raise HTTPException(status_code=400,
                            detail=f"Unsupported aspect {body.aspect_ratio}")
    return {"job_id": await _dispatch_studio_render(
        body.aspect_ratio, body.title, body.quality)}


async def _dispatch_studio_render(aspect_ratio: str, title: Optional[str],
                                  quality: str) -> str:
    """Create + dispatch the render job. One place, so the two callers can't
    drift on job type or on what `quality` means."""
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch, run_studio_render
    job = await create_job("motion_render", "local",
                           {"title": (title or "studio composition")[:200]})
    dispatch(run_studio_render(job_id=job.id, aspect_ratio=aspect_ratio,
                               title=title, supersample=(quality == "high")))
    return job.id


class MotionComposeRequest(BaseModel):
    """A brief for the AI to turn into a composition.

    `instruction` marks a refinement of what is already live rather than a
    fresh piece — it reaches the model as a requested change, so "make the
    headline bigger" does not produce an unrelated video.
    """
    topic: Optional[str] = None
    instruction: Optional[str] = None
    headline: Optional[str] = None
    style: Optional[str] = None
    accent: str = "#ffd60a"
    aspect_ratio: str = "9:16"
    duration_seconds: int = 6


@router.post("/motion/studio/author")
async def motion_studio_author(body: MotionComposeRequest):
    """Have the AI write a composition into the studio. → {job_id}.

    Authoring is a background job, not a request: a real model call plus the
    engine's own verification pass takes long enough that holding an HTTP
    connection open for it would time out somewhere unhelpful.
    """
    gate = _studio_gate()
    if gate:
        return gate
    from backend.agents.generator_motion import ASPECT_DIMS, MOTION_DURATIONS
    if body.aspect_ratio not in ASPECT_DIMS:
        raise HTTPException(status_code=400,
                            detail=f"Unsupported aspect {body.aspect_ratio}")
    if not (body.topic or "").strip() and not (body.instruction or "").strip():
        raise HTTPException(status_code=400,
                            detail="Describe the video you want, or the change to make")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch, run_studio_author
    # Clamp rather than reject: an out-of-range length from an older client
    # should still make a video.
    duration = min(MOTION_DURATIONS, key=lambda d: abs(d - (body.duration_seconds or 6)))
    brief = {
        "topic": body.topic, "instruction": body.instruction,
        "headline": body.headline, "style": body.style, "accent": body.accent,
        "aspect_ratio": body.aspect_ratio, "duration_seconds": duration,
        "assets": _staged_assets(),
    }
    job = await create_job("motion_compose", "local",
                           {"topic": (body.topic or body.instruction or "")[:200]})
    dispatch(run_studio_author(job_id=job.id, brief=brief))
    return {"job_id": job.id}


def _staged_assets() -> list:
    """Whatever the user has staged into the project, for the brief's ASSETS
    list. Filenames go to the model VERBATIM: a tidied-up name is a name that
    does not exist on disk, and the render aborts on it."""
    from backend.services.studio_service import StudioService
    out = []
    assets = StudioService.project_dir() / "assets"
    if not assets.is_dir():
        return out
    for f in sorted(assets.iterdir()):
        if not f.is_file() or f.name == "gsap.min.js":
            continue
        ext = f.suffix.lower()
        kind = next((k for k, exts in StudioService._ASSET_EXTS.items() if ext in exts), None)
        if not kind:
            continue
        entry = {"file": f.name, "type": kind}
        if kind in ("video", "audio"):
            from backend.services.video_utils import probe_duration
            entry["duration"] = round(probe_duration(f, 0.0), 1) or None
        out.append(entry)
    return out[:6]
