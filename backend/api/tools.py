# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""REST /api/tools — modular single-purpose video utilities (OSS variant).

Each tool is a thin HTTP wrapper that:
  1. Validates + saves an uploaded file to STORAGE_ROOT/tools/in/
  2. Creates a Job row + dispatches an async task via task_runner
  3. Returns {"job_id": ...} for the frontend to subscribe over WS

Output goes to STORAGE_ROOT/tools/out/{job_id}.{ext} and is served via
GET /api/tools/download/{job_id}.

OSS adaptation: no billing / cloud backend. Text-AI tools route through the
user's own AI key via backend.core.ai_provider; TTS uses local Edge TTS;
ffmpeg/whisper tools run locally. Media-gen tools (AI image/music/video/SFX/
thumbnail) are NOT ported — they need cloud media-gen providers OSS lacks.
"""
import json
import logging
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import select

from backend.config import settings as app_settings
from backend.database import AsyncSessionLocal
from backend.models.job import Job
from backend.services.caption_service import CAPTION_STYLES as _CAPTION_STYLE_DEFS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tools", tags=["tools"])


def tools_in_dir() -> Path:
    return app_settings.STORAGE_ROOT / "tools" / "in"


def tools_out_dir() -> Path:
    return app_settings.STORAGE_ROOT / "tools" / "out"


VIDEO_MAX_BYTES = 1000 * 1024 * 1024  # 1000 MB
LOGO_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
# The Audio tools take audio as well as video. They used to validate against
# VIDEO_EXTS alone, so a podcast mp3 was rejected with a 400 AFTER the upload
# had been sent — on the tools whose whole job is the audio track.
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS

# Caption styles this API accepts — DERIVED from the caption engine so the
# surfaces can't drift again. `brainrot` is excluded: it is a look applied by
# its own pipeline, not a general caption preset.
#
# This was hardcoded to ("viral", "classic", "bold") long after the engine grew
# to eleven, so the Captions tool offered three of them and posting any of the
# other seven came back 422.
_CAPTION_STYLE_IDS = tuple(k for k in _CAPTION_STYLE_DEFS if k != "brainrot")
CaptionStyleId = Literal[_CAPTION_STYLE_IDS]


async def read_upload_capped(file: UploadFile, max_bytes: int,
                             label: str = "File") -> bytes:
    """Read an upload into memory, aborting AT the cap rather than after it.

    `await file.read()` materializes the whole body before any size check can
    reject it, so a multi-gigabyte upload costs the memory whether or not it is
    allowed. Reading in chunks and stopping one byte over the limit turns that
    into a cheap 413.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                413, f"{label} too large (max {max_bytes // 1024 // 1024} MB)")
        chunks.append(chunk)
    if not total:
        raise HTTPException(400, f"{label} is empty")
    return b"".join(chunks)


async def save_upload(
    file: UploadFile,
    job_id: str,
    allowed_exts: set[str],
    max_bytes: int,
    suffix: str = "",
) -> Path:
    """Validate + save an uploaded file, return the on-disk Path.

    suffix disambiguates multi-file uploads (e.g. watermark takes video+logo).
    Stored as STORAGE_ROOT/tools/in/{job_id}{suffix}{ext}.
    """
    if not file or not file.filename:
        raise HTTPException(400, "No file provided")
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed_exts:
        raise HTTPException(400, f"Unsupported format {ext}. Use: {sorted(allowed_exts)}")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Uploaded file is empty")
    if len(raw) > max_bytes:
        raise HTTPException(413, f"File too large (max {max_bytes // 1024 // 1024} MB)")

    tools_in_dir().mkdir(parents=True, exist_ok=True)
    target = tools_in_dir() / f"{job_id}{suffix}{ext}"
    target.write_bytes(raw)
    return target


def tool_out_path(job_id: str, ext: str = ".mp4") -> Path:
    tools_out_dir().mkdir(parents=True, exist_ok=True)
    return tools_out_dir() / f"{job_id}{ext}"


def safe_tools_path(path_str: str) -> Path:
    """Resolve a tool file path, reject anything outside STORAGE_ROOT/tools/."""
    if not path_str:
        raise HTTPException(404)
    p = Path(path_str).resolve()
    root = (app_settings.STORAGE_ROOT / "tools").resolve()
    if not p.is_relative_to(root):
        raise HTTPException(403, "Access denied")
    return p


# Output extension → content-type, for the lightweight /download-meta probe
# the frontend uses to PICK a preview element (it branches on
# `content_type.startsWith("video/")`, which is why the default here is
# video/mp4 rather than octet-stream).
#
# The download response itself no longer reads this map: it goes through
# `videos.serve_media_with_range`, which types the bytes off the same map every
# other media route uses. Keep this one to the question it answers — "which
# element should render this?" — and don't grow it into a second MIME table.
_TOOL_OUTPUT_MEDIA = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain",
    ".srt": "text/plain",
    ".vtt": "text/vtt",
    ".json": "application/json",
}


@router.get("/download-meta/{job_id}")
async def download_meta(job_id: str):
    """Lightweight metadata for a completed tool output — content-type, size,
    filename — WITHOUT streaming the (potentially large) file. The frontend
    calls this to choose the right inline preview element before pointing a
    streaming <video>/<audio>/<img> at /download.

    Returns: {ready, content_type, ext, size, filename}.
    """
    async with AsyncSessionLocal() as db:
        job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != "success":
        return {"ready": False, "status": job.status}
    output = json.loads(job.output_json or "{}")
    path_str = output.get("file")
    if not path_str:
        return {"ready": False, "reason": "no_file"}
    try:
        path = safe_tools_path(path_str)
    except HTTPException:
        return {"ready": False, "reason": "bad_path"}
    if not path.exists():
        return {"ready": False, "reason": "expired"}
    ext = path.suffix.lower()
    return {
        "ready": True,
        "content_type": _TOOL_OUTPUT_MEDIA.get(ext, "video/mp4"),
        "ext": ext,
        "size": path.stat().st_size,
        "filename": path.name,
    }


@router.get("/download/{job_id}")
async def download_tool_result(job_id: str, request: Request):
    """Serve the output of a completed tool job."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != "success":
        raise HTTPException(400, f"Job is {job.status}, not ready for download")
    output = json.loads(job.output_json or "{}")
    path_str = output.get("file")
    if not path_str:
        raise HTTPException(404, "Output file path missing from job")
    path = safe_tools_path(path_str)
    if not path.exists():
        raise HTTPException(404, "Output file no longer on disk (expired?)")

    tool_label = job.job_type.replace(":", "_")
    # Range-aware, because this route is not only a download: ToolRunner
    # mounts it as the result preview's `<video src>`, so it is scrubbed as
    # often as it is saved. A request with no Range (which is what the
    # Download button sends) still gets the whole file with the filename
    # attached.
    from backend.api.videos import serve_media_with_range
    return serve_media_with_range(
        path, request, filename=f"viralmint_{tool_label}{path.suffix}")


# ── Tool endpoints ─────────────────────────────────────────────────────────────
# Each endpoint is a thin wrapper: validate upload → create job → dispatch runner.

@router.post("/captions")
async def captions_tool(
    file: UploadFile = File(...),
    style: CaptionStyleId = Form("viral"),
    emoji_style: Literal["none", "minimal", "moderate", "heavy"] = Form("moderate"),
):
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_captions
    job = await create_job("tool:captions", "local", {"style": style, "emoji_style": emoji_style})
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_captions(job.id, in_path, style, emoji_style))
    return {"job_id": job.id}


@router.post("/reframe")
async def reframe_tool(
    file: UploadFile = File(...),
):
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_reframe
    job = await create_job("tool:reframe", "local", {})
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_reframe(job.id, in_path))
    return {"job_id": job.id}


@router.post("/audio-enhance")
async def audio_enhance_tool(
    file: UploadFile = File(...),
):
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_audio_enhance
    job = await create_job("tool:audio_enhance", "local", {})
    in_path = await save_upload(file, job.id, MEDIA_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_audio_enhance(job.id, in_path))
    return {"job_id": job.id}


@router.post("/watermark")
async def watermark_tool(
    file: UploadFile = File(...),
    logo: UploadFile = File(...),
    position: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = Form("bottom-right"),
    opacity: float = Form(0.8),
    size_pct: float = Form(8.0),
):
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_watermark
    job = await create_job("tool:watermark", "local", {
        "position": position, "opacity": opacity, "size_pct": size_pct,
    })
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    logo_path = await save_upload(logo, job.id, IMAGE_EXTS, LOGO_MAX_BYTES, suffix="_logo")
    dispatch(run_tool_watermark(
        job.id, in_path, logo_path,
        position=position, opacity=opacity, size_pct=size_pct,
    ))
    return {"job_id": job.id}


MERGE_CLIPS_MIN = 2
MERGE_CLIPS_MAX = 10


@router.post("/merge-clips")
async def merge_clips_tool(
    files: list[UploadFile] = File(...),
    target_aspect: Literal["9:16", "16:9", "1:1", "auto"] = Form("auto"),
    transition: Literal["none", "crossfade"] = Form("none"),
):
    """Stitch N user-uploaded clips into one mp4. Each clip is center-cropped
    to the target aspect, re-encoded to a uniform codec, then concatenated.
    Clip order is preserved as the order of `files`."""
    if len(files) < MERGE_CLIPS_MIN:
        raise HTTPException(400, f"Need at least {MERGE_CLIPS_MIN} clips")
    if len(files) > MERGE_CLIPS_MAX:
        raise HTTPException(400, f"Max {MERGE_CLIPS_MAX} clips per merge")
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_merge_clips
    job = await create_job("tool:merge_clips", "local", {
        "clip_count": len(files),
        "target_aspect": target_aspect,
    })
    in_paths: list[Path] = []
    for i, f in enumerate(files):
        p = await save_upload(f, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES, suffix=f"_{i:02d}")
        in_paths.append(p)
    dispatch(run_tool_merge_clips(job.id, in_paths, target_aspect, transition=transition))
    return {"job_id": job.id}


@router.post("/remove-silence")
async def remove_silence_tool(
    file: UploadFile = File(...),
):
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_remove_silence
    job = await create_job("tool:remove_silence", "local", {})
    in_path = await save_upload(file, job.id, MEDIA_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_remove_silence(job.id, in_path))
    return {"job_id": job.id}


@router.post("/translate")
async def translate_tool(
    file: UploadFile = File(...),
    target_language: str = Form(...),
    mode: Literal["captions_only", "full_dub"] = Form("captions_only"),
    voice_id: str = Form(""),
    caption_style: str = Form("viral"),
    emoji_style: str = Form("moderate"),
):
    """Translate captions (and optionally dub audio) to another language.

    Modes:
      - captions_only: original audio kept; burn translated captions
      - full_dub: replace audio with target-language Edge-TTS voiceover +
                  burn translated captions on the dubbed video
    """
    if mode not in ("captions_only", "full_dub"):
        raise HTTPException(400, f"Invalid mode: {mode}")
    if not (target_language or "").strip():
        raise HTTPException(400, "target_language is required")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_translate
    job = await create_job("tool:translate", "local", {
        "target_language": target_language,
        "mode": mode,
        "voice_id": voice_id if mode == "full_dub" else None,
        "caption_style": caption_style,
    })
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_translate(
        job.id, in_path, target_language.strip(), mode,
        voice_id, caption_style, emoji_style,
    ))
    return {"job_id": job.id}


@router.post("/hook-analysis")
async def hook_analysis(file: UploadFile = File(...)):
    """Analyze the opening hook of a video — score 1-10 + suggested
    alternative openings.

    Output is structured JSON (no downloadable file). The job's output_data
    carries the result; the UI fetches /api/jobs/{id} after job_complete.
    """
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_hook_analysis
    job = await create_job("tool:hook_analysis", "local", {
        "filename": file.filename,
    })
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_hook_analysis(job.id, in_path))
    return {"job_id": job.id}


# ── AI prompt assistance ─────────────────────────────────────────────────────
# Synchronous (no job): the user clicks "Enhance" next to a prompt/script
# field and gets an improved version back inline. Routes every prompt-driven
# tool through ONE endpoint — `kind` swaps the guidance so the rewrite is
# model-appropriate. Uses the user's own AI key (BYOK).
_ENHANCE_GUIDANCE = {
    "image": (
        "You rewrite prompts for a text-to-image model. "
        "Produce ONE vivid, concrete prompt covering subject, composition, "
        "lighting, style, lens/camera, mood and color. No impossible camera "
        "directions. Under 80 words."
    ),
    "video": (
        "You rewrite prompts for a text-to-video model. Describe a SINGLE shot: "
        "subject, motion, camera movement, lighting, mood, pacing. Under 60 words."
    ),
    "music": (
        "You rewrite music-generation prompts. Specify genre, instrumentation, "
        "tempo/energy, mood and intended use. Under 40 words. No lyrics."
    ),
    "voiceover": (
        "You improve a short voice-over SCRIPT for a social video. Open with a "
        "strong first-line hook, keep lines spoken-word natural and tight, end "
        "with a clear payoff. Preserve the user's intent."
    ),
    "metadata": (
        "You expand a rough topic into a clear one-paragraph video description "
        "the metadata generator can work from. Factual, on-topic, no hashtags."
    ),
    "script": (
        "You improve a short-form video script: strong hook, punchy lines, clear "
        "call to action. Preserve the user's intent."
    ),
    "lyrics": (
        "You write/improve SONG LYRICS for an AI music model. Use clear "
        "section tags like [Verse], [Chorus], [Bridge]. Keep lines singable, "
        "rhythmic and concise. Match the user's theme. If a draft is given, keep "
        "its structure and intent."
    ),
}
_ENHANCE_NOUN = {
    "image": "image prompt", "video": "video prompt", "music": "music prompt",
    "voiceover": "voice-over script", "metadata": "video description",
    "script": "script", "lyrics": "song lyrics",
}
_ENHANCE_DRAFT_MAX = 4000


@router.post("/enhance-prompt")
async def enhance_prompt(
    draft: str = Form(""),
    kind: Literal["image", "video", "music", "voiceover", "metadata", "script", "lyrics"] = Form("image"),
    context: str = Form(""),
):
    """Rewrite a creative prompt/script into a stronger version (or, when the
    draft is empty, write a solid starting prompt). Synchronous — returns the
    improved text immediately so the field can swap it inline.

    Returns: {"enhanced": "<text>"}  (HTTP 4xx with `detail` on failure)
    """
    draft = (draft or "").strip()[:_ENHANCE_DRAFT_MAX]
    guidance = _ENHANCE_GUIDANCE.get(kind, _ENHANCE_GUIDANCE["image"])
    noun = _ENHANCE_NOUN.get(kind, "prompt")

    from backend.core.ai_provider import get_ai_client
    from backend.models.user_settings import UserSettings
    async with AsyncSessionLocal() as db:
        user_settings = (await db.execute(
            select(UserSettings).where(UserSettings.user_id == "local")
        )).scalar_one_or_none()

    try:
        ai = get_ai_client(user_settings)
    except Exception:
        raise HTTPException(401, "Configure an AI provider key in Settings to use AI prompt enhancement.")

    system = (
        f"You are a prompt engineer for short-form content creators. {guidance} "
        f"Return ONLY the rewritten {noun} — no preamble, quotes, labels or "
        "explanation. Reply in the same language the user wrote in."
    )
    if draft:
        user_msg = f"Improve this {noun}:\n\n{draft}"
    else:
        user_msg = f"Write a strong, ready-to-use {noun} for a faceless short-form creator."
    if context:
        user_msg += f"\n\nContext: {context[:500]}"

    try:
        out = await ai.chat(
            messages=[{"role": "user", "content": user_msg}],
            system=system,
            max_tokens=400,
        )
    except Exception as e:
        logger.warning("enhance-prompt AI call failed: %s", e)
        raise HTTPException(502, "Couldn't reach the AI just now — try again in a moment.")

    enhanced = (out or "").strip().strip('"').strip()
    if not enhanced:
        raise HTTPException(502, "The AI returned an empty result — try again.")
    return {"enhanced": enhanced}


@router.post("/voiceover")
async def voiceover_tool(
    text: str = Form(...),
    voice_id: Optional[str] = Form(None),
    provider: str = Form("edge_tts"),
    video: Optional[UploadFile] = File(None),
):
    """Generate a voice-over from a script.

    `provider` is "edge_tts" (free, local) or "openai_tts" (your own key). It
    used to be absent, so the page's provider picker was decorative and an
    OpenAI voice id reached Edge TTS, which rejects it — the job simply failed.
    A paid provider with no key degrades to Edge with a constraint warning.

    If a video is also uploaded, the voice is overlaid on top of it.
    """
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "Script is empty")
    if len(text) > 10_000:
        raise HTTPException(413, "Script too long (max 10,000 characters)")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_voiceover
    job = await create_job("tool:voiceover", "local", {
        "voice_id": voice_id, "provider": provider, "chars": len(text),
    })

    video_path = None
    if video and video.filename:
        video_path = await save_upload(video, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)

    dispatch(run_tool_voiceover(
        job.id, text, voice_id or "",
        video_path=video_path, provider=provider,
    ))
    return {"job_id": job.id}


@router.post("/transform")
async def transform_tool(
    operation: Literal["flip_h", "flip_v", "rotate_cw", "rotate_ccw", "rotate_180",
                       "loop", "volume", "mute"] = Form(...),
    amount: str = Form(""),
    file: UploadFile = File(...),
):
    """Quick local video transforms: flip / rotate / loop / volume / mute."""
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_transform
    job = await create_job("tool:transform", "local", {"operation": operation, "amount": amount})
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_transform(job.id, in_path, operation, amount))
    return {"job_id": job.id}


_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
_AUDIO_MAX_BYTES = 100 * 1024 * 1024  # 100 MB


@router.post("/music-visualizer")
async def music_visualizer_tool(
    style: Literal["waves", "bars", "spectrum"] = Form("waves"),
    palette: str = Form("sunset"),
    aspect: Literal["9:16", "1:1", "16:9"] = Form("9:16"),
    file: UploadFile = File(...),
):
    """Audio file → animated visualizer video (waveform / musical bars /
    spectrum) synced to the audio. Pure local FFmpeg."""
    if not (file and file.filename):
        raise HTTPException(400, "An audio file is required")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_music_visualizer
    job = await create_job("tool:music_visualizer", "local", {
        "style": style, "palette": palette, "aspect": aspect,
    })
    audio_path = await save_upload(file, job.id, _AUDIO_EXTS, _AUDIO_MAX_BYTES)
    dispatch(run_tool_music_visualizer(job.id, audio_path, style=style, palette=palette, aspect=aspect))
    return {"job_id": job.id}


# ── Pure-FFmpeg conversion tools (no AI) ───────────────────────────────────

@router.post("/gif")
async def gif_tool(
    file: UploadFile = File(...),
    fps: int = Form(15),
    width: int = Form(540),
    start_seconds: float = Form(0.0),
    duration_seconds: float = Form(0.0),  # 0 = whole video
):
    """Convert a video to an animated GIF via two-pass palette generation."""
    if not 5 <= fps <= 30:
        raise HTTPException(400, "fps must be 5-30")
    if not 240 <= width <= 1080:
        raise HTTPException(400, "width must be 240-1080")
    # Two-sided so NaN/inf are rejected rather than reaching ffmpeg — see the
    # note on the same pair in /trim.
    from backend.services.video_utils import MAX_MEDIA_SECONDS
    if not 0.0 <= start_seconds <= MAX_MEDIA_SECONDS:
        raise HTTPException(400, "start_seconds must be a number >= 0")
    if not 0.0 <= duration_seconds <= 60.0:
        raise HTTPException(400, "duration_seconds must be 0-60 (0 means whole video)")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_gif
    job = await create_job("tool:gif", "local", {
        "fps": fps, "width": width,
        "start_seconds": start_seconds, "duration_seconds": duration_seconds,
    })
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_gif(
        job.id, in_path,
        fps=fps, width=width,
        start_seconds=start_seconds, duration_seconds=duration_seconds,
    ))
    return {"job_id": job.id}


@router.post("/speed")
async def speed_tool(
    file: UploadFile = File(...),
    speed: float = Form(1.5),
    keep_pitch: bool = Form(True),
):
    """Speed up or slow down a video. Accepts 0.25x to 4x."""
    if not 0.25 <= speed <= 4.0:
        raise HTTPException(400, "speed must be 0.25-4.0")
    if abs(speed - 1.0) < 0.01:
        raise HTTPException(400, "speed=1.0 is a no-op — pick a value other than 1x")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_speed
    job = await create_job("tool:speed", "local", {
        "speed": speed, "keep_pitch": keep_pitch,
    })
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_speed(job.id, in_path, speed=speed, keep_pitch=keep_pitch))
    return {"job_id": job.id}


# ── AI Text tools: Metadata / Chapters generators ──────────────────────────

_METADATA_INPUT_TEXT_MAX = 20_000  # generous — fits a 90-minute transcript


@router.post("/metadata")
async def metadata_tool(
    text: str = Form(""),
    file: UploadFile = File(None),
    topic: str = Form(""),
):
    """Generate SEO metadata (title / description / tags / TikTok caption) for
    a video, transcript, or topic.

    Accepts three input modes — pick whichever the caller has:
      1. `text` — paste a script / transcript directly (fastest, no Whisper)
      2. `file` — upload a video; we Whisper-transcribe first then proceed
      3. `topic` — bare topic string like "AI startup ideas" (no source)
    """
    text = (text or "").strip()
    topic = (topic or "").strip()
    if not text and not topic and not (file and file.filename):
        raise HTTPException(400, "Provide one of: text, topic, or a video file")
    if len(text) > _METADATA_INPUT_TEXT_MAX:
        raise HTTPException(413, f"Text too long (max {_METADATA_INPUT_TEXT_MAX} chars)")
    if len(topic) > 500:
        raise HTTPException(413, "Topic too long (max 500 chars)")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_metadata
    job = await create_job("tool:metadata", "local", {
        "mode": "file" if file else ("text" if text else "topic"),
        "chars": len(text) or len(topic),
    })
    in_path = None
    if file and file.filename:
        in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_metadata(
        job.id, text=text, topic=topic, in_path=in_path,
    ))
    return {"job_id": job.id}


@router.post("/auto-chapters")
async def auto_chapters_tool(
    file: UploadFile = File(...),
    target_count: int = Form(0),  # 0 = auto-pick based on video length
):
    """Generate YouTube chapter markers (timestamps + titles) from a video.

    `target_count=0` lets the AI pick (typically ~1 chapter per 2-3 minutes
    of content). Explicit values clamp to 3-20."""
    if target_count < 0 or target_count > 20:
        raise HTTPException(400, "target_count must be 0-20 (0 means auto)")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_auto_chapters
    job = await create_job("tool:auto_chapters", "local", {"target_count": target_count})
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_auto_chapters(job.id, in_path, target_count=target_count))
    return {"job_id": job.id}


@router.post("/trim")
async def trim_tool(
    file: UploadFile = File(...),
    start_seconds: float = Form(0.0),
    end_seconds: float = Form(...),
):
    """Keep the [start, end] segment of a video, drop the rest.

    Pure FFmpeg, frame-accurate (re-encode, not keyframe-snapped).
    """
    # Bounded on BOTH sides, not `< 0`: NaN fails every comparison, so the
    # one-sided form passed it through all three checks below and handed ffmpeg
    # `-ss nan` (a Form float parses "nan"/"inf" happily). The same defect the
    # clip pipeline's timestamp parser carried.
    from backend.services.video_utils import MAX_MEDIA_SECONDS
    if not 0.0 <= start_seconds <= MAX_MEDIA_SECONDS:
        raise HTTPException(400, "start_seconds must be a number >= 0")
    if not 0.0 < end_seconds <= MAX_MEDIA_SECONDS:
        raise HTTPException(400, "end_seconds must be a number > 0")
    if end_seconds <= start_seconds:
        raise HTTPException(400, "end_seconds must be greater than start_seconds")
    if end_seconds - start_seconds < 0.5:
        raise HTTPException(400, "Trim range must be at least 0.5s")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_trim
    job = await create_job("tool:trim", "local", {
        "start_seconds": start_seconds, "end_seconds": end_seconds,
    })
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_trim(job.id, in_path, start_seconds, end_seconds))
    return {"job_id": job.id}


@router.post("/subtitles")
async def subtitles_tool(
    file: UploadFile = File(...),
    format: Literal["srt", "vtt", "txt"] = Form("srt"),
):
    """Transcribe a video and return a downloadable subtitle (.srt/.vtt) or
    plain transcript (.txt) file. No burn-in."""
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_subtitles
    job = await create_job("tool:subtitle_export", "local", {"format": format})
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_subtitles(job.id, in_path, fmt=format))
    return {"job_id": job.id}


@router.post("/compress")
async def compress_tool(
    file: UploadFile = File(...),
    resolution: Literal[
        "original", "1280x720", "854x480", "640x360", "426x240"
    ] = Form("original"),
    level: Literal["maximum", "high", "medium", "low", "minimal"] = Form("high"),
):
    """Re-encode a video smaller — for email, chat apps and upload limits.

    Two independent axes: `resolution` (keep the source size, or scale down to
    a named height) and `level` (how hard to squeeze at that size). Local
    FFmpeg only. Asking for a resolution TALLER than the source keeps the
    source size rather than upscaling; the job reports it.
    """
    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_compress
    job = await create_job("tool:compress", "local", {
        "resolution": resolution, "level": level,
    })
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_compress(job.id, in_path, resolution=resolution, level=level))
    return {"job_id": job.id}


@router.post("/crop")
async def crop_tool(
    file: UploadFile = File(...),
    x: float = Form(...),
    y: float = Form(...),
    w: float = Form(...),
    h: float = Form(...),
):
    """Crop a video to a rectangle you drew on the frame.

    The box arrives NORMALIZED (0-1, origin top-left) so it's independent of
    whatever size the preview was rendered at — the runner converts against
    the real source dimensions. The manual counterpart to `/reframe`, which
    picks the framing for you.
    """
    for name, value in (("x", x), ("y", y), ("w", w), ("h", h)):
        if not 0.0 <= value <= 1.0:
            raise HTTPException(400, f"{name} must be between 0 and 1")
    if w <= 0 or h <= 0:
        raise HTTPException(400, "Drag a crop box over the frame first")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_crop
    job = await create_job("tool:crop", "local", {
        "x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4),
    })
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_crop(job.id, in_path, x, y, w, h))
    return {"job_id": job.id}


@router.post("/auto-zoom")
async def auto_zoom_tool(
    file: UploadFile = File(...),
    zoom_factor: float = Form(1.15),
    words_per_group: int = Form(3),
):
    """Add subtle zoom "punch-in" pulses on spoken words for energy. Whisper
    word timings drive the pulses. Works standalone; pairs well after Captions."""
    if not 1.05 <= zoom_factor <= 1.4:
        raise HTTPException(400, "zoom_factor must be 1.05-1.4")
    if not 1 <= words_per_group <= 6:
        raise HTTPException(400, "words_per_group must be 1-6")

    from backend.agents.job_helper import create_job
    from backend.core.task_runner import dispatch
    from backend.core.tool_runners import run_tool_auto_zoom
    job = await create_job("tool:auto_zoom", "local", {
        "zoom_factor": zoom_factor, "words_per_group": words_per_group,
    })
    in_path = await save_upload(file, job.id, VIDEO_EXTS, VIDEO_MAX_BYTES)
    dispatch(run_tool_auto_zoom(job.id, in_path, zoom_factor=zoom_factor, words_per_group=words_per_group))
    return {"job_id": job.id}
