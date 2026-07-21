# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Background music selection and mixing.
Mixes royalty-free music under voiceover at configurable volume.
Auto-downloads royalty-free tracks from Pixabay if none are bundled.
"""
import asyncio
import logging
import subprocess
from pathlib import Path

import httpx

from backend.config import settings
from backend.core.exceptions import VideoGenerationError
from backend.services.video_utils import probe_duration

logger = logging.getLogger(__name__)

# Bundled royalty-free music directory
MUSIC_DIR = settings.STORAGE_ROOT / "music"

MUSIC_GENRES = {
    "lofi":       "lo-fi chill beats",
    "cinematic":  "dramatic orchestral",
    "upbeat":     "energetic electronic",
    "ambient":    "calm atmospheric",
    "corporate":  "business motivational",
    "jazz":       "smooth jazz relaxing",
    "hiphop":     "hip hop trap beats",
    "classical":  "classical piano orchestral",
    "edm":        "EDM dance electronic",
    "acoustic":   "acoustic guitar folk",
    "rnb":        "R&B soul smooth",
    "rock":       "rock energetic guitar",
}

# Pixabay royalty-free music search terms
PIXABAY_GENRE_QUERIES = {
    "lofi":       "lo fi chill",
    "cinematic":  "cinematic dramatic",
    "upbeat":     "upbeat energetic",
    "ambient":    "ambient calm",
    "corporate":  "corporate motivational",
    "jazz":       "jazz smooth",
    "hiphop":     "hip hop trap",
    "classical":  "classical piano",
    "edm":        "edm dance",
    "acoustic":   "acoustic guitar",
    "rnb":        "rnb soul",
    "rock":       "rock guitar",
}


async def select_music(
    genre: str = "lofi",
    duration: int = 60,
    track_filename: str | None = None,
) -> Path | None:
    """
    Select a background music track.

    If `track_filename` is provided, loads that specific file from the user's
    library (strict match, no fallback). Otherwise matches `genre` via the
    existing glob, then falls back to any track, then the Pixabay stub.

    Returns None if nothing available (gracefully skipped by generator).
    """
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)

    # Explicit user-picked track overrides everything else.
    if track_filename:
        # Reject traversal / absolute paths — we only load from MUSIC_DIR.
        if "/" in track_filename or "\\" in track_filename or ".." in track_filename:
            logger.warning(f"Rejected unsafe track_filename: {track_filename!r}")
            return None
        candidate = (MUSIC_DIR / track_filename).resolve()
        if not candidate.is_relative_to(MUSIC_DIR.resolve()):
            logger.warning(f"Rejected track outside MUSIC_DIR: {track_filename!r}")
            return None
        if candidate.is_file() and candidate.stat().st_size > 0:
            logger.info(f"Selected user-picked track: {candidate.name}")
            return candidate
        logger.warning(f"track_filename {track_filename!r} not found on disk — falling back to genre")

    # Look for bundled tracks matching genre
    for ext in ("mp3", "wav", "ogg", "m4a"):
        for track in MUSIC_DIR.glob(f"*{genre}*.{ext}"):
            if track.is_file() and track.stat().st_size > 0:
                logger.info(f"Selected bundled music: {track.name}")
                return track

    # Look for any track as fallback
    for ext in ("mp3", "wav", "ogg", "m4a"):
        for track in MUSIC_DIR.glob(f"*.{ext}"):
            if track.is_file() and track.stat().st_size > 0:
                logger.info(f"Selected fallback music: {track.name}")
                return track

    # No bundled tracks — try auto-downloading from Pixabay
    track = await _download_from_pixabay(genre)
    if track:
        return track

    logger.info("No background music available")
    return None


async def _download_from_pixabay(genre: str) -> Path | None:
    """
    Download a royalty-free music track from Pixabay.
    Pixabay audio is CC0 (no attribution required).
    Note: Pixabay API signups are currently closed to new users.
    Users can add .mp3 files manually to storage/music/ instead.
    """
    logger.info(f"No bundled music for genre '{genre}' — users can add .mp3 files to storage/music/")
    return None


async def mix_audio(
    voice_path: Path,
    music_path: Path,
    output_path: Path = None,
    music_volume_db: float = -20.0,
) -> Path:
    """
    Mix voice + background music using FFmpeg.
    Music is:
    - Lowered to music_volume_db (default -20dB)
    - Faded in over 1s at start
    - Faded out over 2s at end
    - Trimmed to match voice duration
    """
    if output_path is None:
        output_path = voice_path.parent / f"{voice_path.stem}_mixed.mp3"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not music_path or not music_path.exists():
        logger.warning("No music file provided — returning voice only")
        return voice_path

    def _mix():
        # Get voice duration
        voice_duration = probe_duration(voice_path, default=60.0)

        # FFmpeg filter: lower music volume, fade in/out, mix with voice.
        # normalize=0: amix's default normalize=1 divides every input by the
        # active-input count, so a 2-input mix halves the VOICE (−6 dB) instead
        # of laying the music underneath it. normalize=0 keeps the voice at its
        # intended full level with the music as a true −20 dB bed; alimiter
        # guards the summed peak against clipping.
        fade_out_start = max(voice_duration - 2.0, 0)
        filter_complex = (
            f"[1:a]volume={music_volume_db}dB,"
            f"afade=t=in:d=1,"
            f"afade=t=out:st={fade_out_start:.1f}:d=2"
            f"[bg];"
            f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[mixraw];"
            f"[mixraw]alimiter=limit=0.95[out]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", str(voice_path),
            "-i", str(music_path),
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-c:a", "libmp3lame", "-b:a", "192k",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.warning(f"Music mixing failed: {result.stderr[:400]}")
            return voice_path  # Return voice only on failure

        return output_path

    return await asyncio.to_thread(_mix)
