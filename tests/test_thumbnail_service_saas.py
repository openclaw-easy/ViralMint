# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend/services/thumbnail_service.py.

The SaaS thumbnail tests (test_thumbnail_from_frame, test_thumbnail_studio)
target APIs the OSS build does not carry — `generate_thumbnail_from_frame`,
the AI-thumbnail STUDIO endpoints, `_compose_thumbnail_prompt`,
`_SCRIPT_FONT_FILES` — so they are not ported. This file instead covers the
functions the OSS module actually exposes: frame scoring, Pillow
compositing, font loading/sizing, AI-headline generation, and the
generate_ai_thumbnail orchestration. FFmpeg/ffprobe and the AI client are
mocked; Pillow runs on tiny in-memory frames (no heavy rendering).
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image, ImageDraw

from backend.services import thumbnail_service as ts


def _write_flat(path: Path, size=(64, 64), color=(120, 120, 120)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _write_sharp(path: Path, size=(64, 64)):
    """A high-frequency checkerboard → large FIND_EDGES variance."""
    img = Image.new("RGB", size, (0, 0, 0))
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            if (x + y) % 2 == 0:
                px[x, y] = (255, 255, 255)
    img.save(path, "JPEG")


# ── _pick_best_frame ─────────────────────────────────────────────────────────

class TestPickBestFrame:
    def test_picks_sharpest(self, tmp_path):
        flat = tmp_path / "flat.jpg"
        sharp = tmp_path / "sharp.jpg"
        _write_flat(flat)
        _write_sharp(sharp)
        assert ts._pick_best_frame([flat, sharp]) == sharp

    def test_single_frame_returned(self, tmp_path):
        only = tmp_path / "only.jpg"
        _write_flat(only)
        assert ts._pick_best_frame([only]) == only

    def test_unreadable_frame_does_not_crash(self, tmp_path):
        good = tmp_path / "good.jpg"
        _write_sharp(good)
        bad = tmp_path / "bad.jpg"
        bad.write_bytes(b"not an image")
        # First path is the default; a decode error is swallowed.
        assert ts._pick_best_frame([good, bad]) == good


# ── _composite_thumbnail ─────────────────────────────────────────────────────

class TestCompositeThumbnail:
    def test_writes_valid_jpeg_with_headline(self, tmp_path):
        frame = tmp_path / "frame.jpg"
        _write_flat(frame, size=(320, 180))
        out = tmp_path / "thumb.jpg"
        ts._composite_thumbnail(frame, "SHOCKING TRUTH", out)
        assert out.exists() and out.stat().st_size > 0
        with Image.open(out) as im:
            im.verify()  # not corrupt

    def test_empty_headline_takes_early_save_path(self, tmp_path):
        frame = tmp_path / "frame.jpg"
        _write_flat(frame, size=(320, 180))
        out = tmp_path / "thumb.jpg"
        ts._composite_thumbnail(frame, "", out)
        assert out.exists()
        with Image.open(out) as im:
            # Resized to the standard 1280x720 canvas.
            assert im.size == (1280, 720)


# ── _load_bold_font / _auto_size_font ────────────────────────────────────────

class TestFontHelpers:
    def test_load_bold_font_scales_with_width(self):
        small = ts._load_bold_font(400)
        large = ts._load_bold_font(2000)
        assert getattr(large, "size", 0) >= getattr(small, "size", 0)

    def test_auto_size_font_shrinks_oversized_text(self):
        img = Image.new("RGB", (200, 100))
        draw = ImageDraw.Draw(img)
        font = ts._load_bold_font(1000)  # deliberately huge
        sized = ts._auto_size_font(draw, "A VERY LONG HEADLINE THAT OVERFLOWS", font,
                                   max_width=120)
        # Either it shrank (truetype path) or returned the same default font;
        # never larger than the original.
        assert getattr(sized, "size", getattr(font, "size", 0)) <= getattr(font, "size", 10**9)


# ── _get_ai_headline ─────────────────────────────────────────────────────────

class TestGetAiHeadline:
    async def test_empty_inputs_returns_empty(self):
        assert await ts._get_ai_headline("", "", None) == ""

    async def test_uppercases_and_strips_quotes(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value='  "shocking truth revealed"  ')
        with patch("backend.core.ai_provider.get_ai_client", return_value=ai):
            out = await ts._get_ai_headline("script text", "My Title", MagicMock())
        assert out == "SHOCKING TRUTH REVEALED"

    async def test_trims_to_five_words_when_too_long(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="one two three four five six seven")
        with patch("backend.core.ai_provider.get_ai_client", return_value=ai):
            out = await ts._get_ai_headline("s", "t", MagicMock())
        assert out == "ONE TWO THREE FOUR FIVE"

    async def test_ai_failure_falls_back_to_title_words(self):
        ai = MagicMock()
        ai.chat = AsyncMock(side_effect=RuntimeError("no ai"))
        with patch("backend.core.ai_provider.get_ai_client", return_value=ai):
            out = await ts._get_ai_headline("", "The Amazing Story Of Everything Ever", MagicMock())
        assert out == "THE AMAZING STORY OF"  # first 4 words, upper


# ── generate_ai_thumbnail (orchestration) ────────────────────────────────────

class TestGenerateAiThumbnail:
    async def test_happy_path_pipeline(self, tmp_path):
        frame = tmp_path / "cand.jpg"
        _write_flat(frame)
        out = tmp_path / "thumb.jpg"
        composited = []

        def _stub_composite(best, headline, output_path):
            composited.append((best, headline, output_path))
            output_path.write_bytes(b"\xff" * 512)

        with patch.object(ts, "_extract_candidate_frames",
                          new=AsyncMock(return_value=[frame])), \
             patch.object(ts, "_get_ai_headline",
                          new=AsyncMock(return_value="HEADLINE")), \
             patch.object(ts, "_composite_thumbnail", side_effect=_stub_composite):
            result = await ts.generate_ai_thumbnail(
                video_path=tmp_path / "v.mp4", script="s", title="t", output_path=out,
            )
        assert result == out
        assert composited and composited[0][1] == "HEADLINE"
        # temp candidate cleaned up after success
        assert not frame.exists()

    async def test_no_candidates_falls_back_to_plain_thumbnail(self, tmp_path):
        out = tmp_path / "thumb.jpg"
        with patch.object(ts, "_extract_candidate_frames",
                          new=AsyncMock(return_value=[])), \
             patch("backend.services.ffmpeg_service.extract_thumbnail",
                   new=AsyncMock(return_value=out)) as fallback:
            result = await ts.generate_ai_thumbnail(
                video_path=tmp_path / "v.mp4", output_path=out,
            )
        assert result == out
        fallback.assert_awaited_once()


# ── _get_video_duration ──────────────────────────────────────────────────────

class TestGetVideoDuration:
    async def test_parses_ffprobe_output(self, tmp_path):
        fake = MagicMock(returncode=0, stdout="42.5\n")
        with patch.object(ts.subprocess, "run", return_value=fake):
            assert await ts._get_video_duration(tmp_path / "v.mp4") == pytest.approx(42.5)

    async def test_ffprobe_failure_returns_zero(self, tmp_path):
        with patch.object(ts.subprocess, "run", side_effect=OSError("no ffprobe")):
            assert await ts._get_video_duration(tmp_path / "v.mp4") == 0.0
