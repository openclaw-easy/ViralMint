# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Both clip doors (extract-clips, suggest-clips) build ExtractOptions through ONE helper.

Two hand-copied normalisations drift: a vocabulary or clamp fixed in one door
and not the other means the same request means different things depending on
which button sent it.
"""
import dataclasses
import inspect

import pytest
from fastapi import HTTPException

from backend.api import downloaded
from backend.api.downloaded import ExtractClipsRequest, build_extract_options


@pytest.mark.asyncio
async def test_normalises_like_extract_clips():
    opts = await build_extract_options(ExtractClipsRequest(
        caption_style="bold", emoji_style="LOUD", target_platform=" TikTok ",
        genre=" Podcast ", user_query="  pricing  ",
        min_duration=5, max_duration=10,
    ))
    assert dataclasses.is_dataclass(opts)
    assert opts.caption_style == "bold"
    assert opts.emoji_style == "moderate"          # unknown -> moderate
    assert opts.target_platform == "tiktok"
    assert opts.genre == "podcast"
    assert opts.user_query == "pricing"
    assert opts.min_duration == 10                 # clamped up to 10
    assert opts.max_duration == 15                 # clamped up to 15
    assert opts.force_vertical is False
    assert opts.max_clips == 0 and opts.time_ranges is None


@pytest.mark.asyncio
async def test_rejects_bad_mode_and_inverted_bounds():
    with pytest.raises(HTTPException) as e:
        await build_extract_options(ExtractClipsRequest(mode="magic"))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        await build_extract_options(ExtractClipsRequest(min_duration=40, max_duration=20))
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_manual_mode_keeps_its_own_bounds():
    """Manual mode cuts the user's ranges verbatim: the AI-mode clamps don't apply."""
    opts = await build_extract_options(ExtractClipsRequest(mode="manual", min_duration=5, max_duration=3))
    assert opts.mode == "manual" and opts.min_duration == 5 and opts.max_duration == 3


@pytest.mark.parametrize("door", ["extract_clips", "suggest_clips"])
def test_every_clip_door_uses_the_builder(door):
    """No door keeps its own copy of the normalisation or builds ExtractOptions itself."""
    src = inspect.getsource(getattr(downloaded, door))
    assert "build_extract_options(" in src
    assert "ExtractOptions(" not in src
    assert "_EMOJI_STYLES" not in src
    assert "max(10, int(" not in src
