# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Video generation strategy helpers for the generator pipeline.

Generates videos using Pexels stock footage matched to script content.
Falls back to Ken Burns image zoom or text-on-background if Pexels is unavailable.
Called from GeneratorAgent._generate_video().
"""
import hashlib
import logging
from pathlib import Path

from backend.config import settings
from backend.core.ai_provider import get_ai_client
from backend.core.exceptions import GenerationError

logger = logging.getLogger(__name__)


async def generate_stock_video(script: str, voice_path: Path, aspect_ratio: str, user_settings, visual_style: str = None, transition_style: str = None) -> Path:
    """Pexels stock footage matched to script keywords."""
    pexels_key = settings.PEXELS_API_KEY

    if not pexels_key:
        logger.info("Pexels API key not configured — skipping stock footage tier")
        return None

    from backend.services.pexels_service import build_stock_video

    ai_client = None
    try:
        ai_client = get_ai_client(user_settings)
    except Exception as e:
        logger.warning(f"AI client unavailable for stock video scene extraction (will use fallback): {e}")

    return await build_stock_video(
        script=script,
        voice_path=voice_path,
        pexels_api_key=pexels_key,
        aspect_ratio=aspect_ratio,
        ai_client=ai_client,
        visual_style=visual_style,
        transition_style=transition_style,
    )


async def generate_kenburns_video(start_image: str, voice_path: Path, aspect_ratio: str) -> Path:
    """Image-to-video: apply Ken Burns zoom/pan effects to user-provided image."""
    from backend.services.ffmpeg_service import generate_kenburns_video as _kenburns

    image_path = None
    if start_image.startswith("/api/media/"):
        filename = start_image.split("/")[-1]
        candidate = settings.TMP_DIR / filename
        if candidate.exists():
            image_path = candidate
    if image_path is None:
        image_path = Path(start_image)

    if not image_path.exists():
        raise GenerationError(f"Start image not found: {start_image}")

    output_path = settings.GENERATED_DIR / f"kenburns_{hashlib.md5(str(image_path).encode()).hexdigest()[:8]}.mp4"
    return await _kenburns(
        image_paths=[image_path],
        audio_path=voice_path,
        output_path=output_path,
        aspect_ratio=aspect_ratio,
    )
