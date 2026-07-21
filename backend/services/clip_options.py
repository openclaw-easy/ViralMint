# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Clip-extraction options — one object threaded endpoint → runner → service.

Replaces the ~14 keyword arguments that used to be re-declared (with defaults)
in three places: the extract-clips API endpoint, run_extract_clips, and
extract_viral_clips. Defaults live HERE only, so adding a knob is a one-line
field change instead of editing three signatures + the dispatch call.

Kept deliberately dependency-free (stdlib dataclass only) so the API layer can
import it without pulling the heavy clip_extractor module (whisper/ffmpeg).
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class ExtractOptions:
    """Everything that controls a clip-extraction run.

    `mode` is the discriminator: "ai" (viral-clip picker) or "manual" (cut
    `time_ranges` verbatim). The AI-only knobs (min/max_duration, user_query,
    target_platform, genre) are ignored in manual mode. The rest
    (caption_style, emoji_style, whisper_quality, force_retranscribe,
    remove_silence, force_vertical) are shared post-processing.
    """
    mode: str = "ai"
    max_clips: int = 3
    caption_style: str = "viral"
    whisper_quality: str = "balanced"
    force_retranscribe: bool = False
    min_duration: Optional[int] = None
    max_duration: Optional[int] = None
    remove_silence: bool = False
    force_vertical: bool = False
    user_query: Optional[str] = None
    target_platform: Optional[str] = None
    emoji_style: str = "moderate"
    genre: Optional[str] = None
    time_ranges: Optional[list[dict]] = None
