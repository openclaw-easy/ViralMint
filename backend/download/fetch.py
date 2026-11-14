# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Fetch a video from a link. What every caller uses.

One path, in this order:

    1. The host's rate-limit cooldown. A host that just answered 429 is not
       asked again: the person is told when to retry, at once.
    2. The probe: live streams, over-long sources and dead links end here.
    3. yt-dlp, through the engine's ladder.
    4. If the link itself looks wrong (no extractor, 404, nothing playable,
       an unexplained failure): one AI rewrite of the link with the user's
       own AI key, then steps 1-3 once more with the corrected link.

Never after an abandonment (stall, wall clock): the abandoned thread still
owns the output stem. Callers own what surrounds a fetch: the job row and
the Library row.

The result is the dict callers have always read, plus `url` (the link
actually fetched, when the fix changed it).
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from uuid import uuid4

from backend.config import settings
from backend.core.exceptions import DownloadError, JobCancelledError, RateLimitError
from backend.download import engine, health, messages, notices, progress
from backend.download.classify import ErrorClass
from backend.download.util import host_of

logger = logging.getLogger(__name__)

# A cooldown this short is just waited out; longer, the person is told.
SHORT_COOLDOWN_S = 15
# The failures a different LINK could fix. Everything else is a verdict on a
# link that worked (unavailable, sign-in, rate limit), ours (cookies), or the
# network.
_LINK_MAY_BE_WRONG = frozenset({
    ErrorClass.UNSUPPORTED_URL, ErrorClass.HTTP_404, ErrorClass.NO_FORMATS, ErrorClass.UNKNOWN,
})


async def fetch_video(
    url: str,
    *,
    output_dir: Path | None = None,
    filename: str | None = None,
    extract_audio: bool = True,
    job_id: str | None = None,
    user_id: str = "local",
    options: dict | None = None,
    fix_link: bool = True,
) -> dict:
    """Download the video at `url`.

    Returns {"video_path", "audio_path", "duration", "file_size_mb", "title",
    "views", "likes", "comments", "upload_date", "uploader", "uploader_url",
    "thumbnail", "description", "subtitles", "chapters", "tags", "category",
    "height", "requested_quality", "kept_subtitle_paths",
    "option_postprocessors_dropped", "url"}.

    `options` are per-download choices from backend.services.download_options
    (quality, subtitles, container, thumbnail, metadata); None means the
    standard download. `height` is what was delivered, which a quality cap
    does not guarantee.

    `job_id`: a cancel of that job stops the ladder between attempts and a
    moving transfer within a couple of seconds.

    `fix_link=False` skips the AI link fix (step 4).

    Raises backend.core.exceptions: VideoUnavailableError, BotDetectionError,
    RateLimitError, ExtractorMissingError, DownloadStalledError,
    DownloadTimeoutError, JobCancelledError, DownloadError.
    """
    from backend.services import download_options
    if not url or not isinstance(url, str):
        raise ValueError("url is required")
    out = Path(output_dir or settings.VIDEOS_DIR).resolve()     # yt-dlp's merge needs an absolute path
    out.mkdir(parents=True, exist_ok=True)
    req = engine.Request(
        url=url, output_dir=out, stem=filename or f"dl_{uuid4().hex[:12]}",
        options=download_options.normalize(options), extract_audio=extract_audio,
        job_id=job_id, user_id=user_id,
    )
    try:
        try:
            return await _once(req)
        except JobCancelledError:
            raise
        except DownloadError as first:
            corrected = await _corrected_link(req, first) if fix_link else None
            if not corrected:
                raise
            logger.info("[DOWNLOAD_STEP] host=%s step=link_fix — retrying once with %s",
                        host_of(req.url), corrected[:120])
            try:
                return await _once(dataclasses.replace(req, url=corrected))
            except DownloadError as second:
                raise second from first
    finally:
        await notices.after_download()


async def _once(req: engine.Request) -> dict:
    wait = health.cooldown_for(req.url)
    if wait > SHORT_COOLDOWN_S:
        minutes = max(1, round(wait / 60))
        error = RateLimitError(
            f"{messages.site_name(req.url)} rate-limited downloads from this network a moment ago. "
            f"Try again in about {minutes} minute{'s' if minutes != 1 else ''}."
        )
        error.error_class = ErrorClass.RATE_LIMITED
        raise error
    if wait > 0:
        await asyncio.sleep(wait)

    probed = await engine.probe(req.url, max_duration_s=engine.VIDEO_MAX_DURATION_S)
    result = await engine.run(req, force_generic=probed.force_generic)
    result["url"] = req.url
    return result


# ── One AI rewrite of the link ───────────────────────────────────────────────

async def _corrected_link(req: engine.Request, error: DownloadError) -> str | None:
    """A corrected link worth one more fetch, or None."""
    if getattr(error, "error_class", None) not in _LINK_MAY_BE_WRONG:
        return None                             # includes stalls and timeouts: they carry no class
    if await progress.job_is_cancelled(req.job_id):
        return None
    try:
        from sqlalchemy import select

        from backend.core.ai_retry import ai_fix_url
        from backend.database import AsyncSessionLocal
        from backend.models.user_settings import UserSettings
        async with AsyncSessionLocal() as db:
            row = await db.execute(select(UserSettings).where(UserSettings.user_id == req.user_id))
            user_settings = row.scalar_one_or_none()
        corrected = await ai_fix_url(req.url, str(error), user_settings)
    except Exception as e:  # noqa: BLE001 — the fix is optional; the first error stands
        logger.info("Link fix unavailable (%s); keeping the original error", str(e)[:120])
        return None
    if not corrected or corrected.strip() == req.url.strip():
        return None
    if await progress.job_is_cancelled(req.job_id):
        return None
    return corrected.strip()
