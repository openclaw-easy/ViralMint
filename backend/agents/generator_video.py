# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Video generation strategy helpers for the generator pipeline.

Generates videos using stock footage (Pexels or Pixabay, whichever is
configured) matched to script content, with the user's own images filling
scenes where they supplied any.
Falls back to Ken Burns image zoom or text-on-background if no stock provider
is configured.
Called from GeneratorAgent._generate_video().
"""
import hashlib
import logging
from pathlib import Path

from backend.config import settings
from backend.core.ai_provider import get_ai_client
from backend.core.exceptions import GenerationError
from backend.core.ws_manager import ws_manager
from backend.services import stock_media

logger = logging.getLogger(__name__)


def resolve_still_input(reference: str) -> Path | None:
    """Turn one image reference from a request into a path on this machine.

    Two forms, and only two — every still-image field in the studio speaks the
    same small vocabulary so a reference that works in one field works in all
    of them:

      * `/api/media/<name>` — something uploaded through /api/media/upload.
        Resolved under TMP_DIR by BASENAME, so a reference carrying `..` or an
        absolute path cannot escape the upload directory.
      * anything else — a path on this machine. ViralMint is a local
        self-hosted app whose user already owns the filesystem, so this is
        deliberately not restricted further.

    Returns None when the reference doesn't resolve to a file that exists;
    callers decide whether that is fatal or just one image fewer.
    """
    if not reference:
        return None

    if reference.startswith("/api/media/"):
        # Basename only — `Path(x).name` strips traversal the same way
        # /api/media/{filename} does when it serves these back.
        candidate = settings.TMP_DIR / Path(reference).name
        return candidate if candidate.exists() else None

    candidate = Path(reference)
    return candidate if candidate.exists() else None


async def generate_stock_video(
    script: str,
    voice_path: Path,
    aspect_ratio: str,
    user_settings,
    visual_style: str = None,
    transition_style: str = None,
    user_images: list[str] | None = None,
) -> Path:
    """Stock footage matched to script keywords, with any images the user
    brought filling the opening scenes."""

    resolved_images: list[Path] = []
    for ref in (user_images or []):
        path = resolve_still_input(ref)
        if path is None:
            # Say it out loud. Uploads live in the scratch dir, so a reference
            # CAN go stale between picking the image and hitting Generate —
            # and a photo quietly missing from the finished video is the exact
            # failure this feature must not have. Same treatment as an
            # unreadable file gets further down the pipeline.
            logger.warning("User image not found, skipping: %s", ref)
            await ws_manager.send_constraint_warning(
                constraint="user_image_missing",
                message=(
                    f"Couldn't find \"{Path(ref).name}\" any more — that scene "
                    f"will use stock footage instead. Try adding it again."
                ),
                severity="warning",
            )
            continue
        resolved_images.append(path)

    # Stock footage is what fills the scenes the user did NOT bring an image
    # for. With no provider key AND no images there is nothing to build a
    # video out of; with images we can still build one entirely from them.
    if not stock_media.configured_providers() and not resolved_images:
        logger.info("No stock media API key configured — skipping stock footage tier")
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
        aspect_ratio=aspect_ratio,
        ai_client=ai_client,
        visual_style=visual_style,
        transition_style=transition_style,
        user_images=resolved_images,
    )


async def generate_kenburns_video(start_image: str, voice_path: Path, aspect_ratio: str) -> Path:
    """Image-to-video: apply Ken Burns zoom/pan effects to user-provided image."""
    from backend.services.ffmpeg_service import generate_kenburns_video as _kenburns

    image_path = resolve_still_input(start_image)
    if image_path is None:
        raise GenerationError(f"Start image not found: {start_image}")

    output_path = settings.GENERATED_DIR / f"kenburns_{hashlib.md5(str(image_path).encode()).hexdigest()[:8]}.mp4"
    return await _kenburns(
        image_paths=[image_path],
        audio_path=voice_path,
        output_path=output_path,
        aspect_ratio=aspect_ratio,
    )
