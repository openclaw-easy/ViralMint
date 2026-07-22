# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
REST endpoints for video generation.

POST /api/generate/stock         — Stock footage (Pexels)
POST /api/generate/split-scenes  — AI-split script into scenes with Pexels keywords
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.downloaded_video import DownloadedVideo

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
    )


@router.post("/split-scenes")
async def split_scenes(body: SplitScenesRequest):
    """AI-split a script into stock-footage scenes with per-scene Pexels keywords."""
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
        "For each scene, extract 2-4 Pexels search keywords that describe "
        "the visual content needed. Return JSON only."
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
