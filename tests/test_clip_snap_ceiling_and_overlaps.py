# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Two defects in the AI clip path, after the AI has picked its windows.

1. Sentence-snap moves each end up to ±2 s onto a transcript segment edge, so
   a two-sided snap can add 4 s: a window at a user's pinned max_duration came
   out longer than the maximum they asked for (20 s became 23 s), while the
   docstring said it couldn't. It now falls back end-only → start-only →
   original when the ceiling would break.
2. Time-overlapping windows were dropped only on the chunked (long-video)
   path. The single-call path trusted the prompt's "don't overlap", so two
   picks covering the same seconds (10-40 s and 12-42 s) were cut as two
   clips. The filter now runs after the snap, which also catches overlaps
   the snap itself creates.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.services import clip_extractor
from backend.services.clip_extractor import (
    _dedupe_clips_by_topic,
    _remove_overlapping_clips,
    _snap_to_sentence_boundaries,
)
from backend.services.clip_options import ExtractOptions


class TestOverlapRemoval:
    """Topic dedup compares AI-written words, so two clips covering the same
    seconds with different phrasing both survive it."""

    def _pair(self):
        return [
            {"start": 10.0, "end": 40.0, "virality_score": 9,
             "title": "Why margins collapsed", "hook": "margins",
             "reason": "explains unit economics"},
            {"start": 12.0, "end": 42.0, "virality_score": 8,
             "title": "Founder on hiring pace", "hook": "hiring",
             "reason": "candid remark about recruiting velocity"},
        ]

    def test_topic_dedup_alone_keeps_the_overlap(self):
        assert len(_dedupe_clips_by_topic(self._pair())) == 2

    def test_overlap_filter_drops_the_lower_scored_one(self):
        kept = _remove_overlapping_clips(self._pair())
        assert [(w["start"], w["end"]) for w in kept] == [(10.0, 40.0)]

    def test_adjacent_clips_are_not_overlapping(self):
        """Back-to-back windows are a legitimate result: the filter has a 2 s
        tolerance and must not collapse them into one."""
        windows = [
            {"start": 0.0, "end": 30.0, "virality_score": 9},
            {"start": 30.0, "end": 60.0, "virality_score": 8},
        ]
        assert len(_remove_overlapping_clips(windows)) == 2


class TestSnapRespectsPinnedMaxDuration:
    # Boundaries at 8/10 (starts) and 10/32 (ends): snapping 10-30 to the
    # nearest edges pulls `end` out to 32, a 22 s clip.
    SEGMENTS = [{"start": 8.0, "end": 10.0}, {"start": 10.0, "end": 32.0}]
    WINDOW = [{"start": 10.0, "end": 30.0, "virality_score": 9}]

    def test_unpinned_snap_still_extends(self):
        out = _snap_to_sentence_boundaries(self.WINDOW, self.SEGMENTS)
        assert out[0]["end"] == 32.0

    def test_pinned_max_is_not_exceeded(self):
        out = _snap_to_sentence_boundaries(self.WINDOW, self.SEGMENTS, max_duration=20)
        assert out[0]["end"] - out[0]["start"] <= 20

    def test_both_ends_moving_out_falls_back_to_one_end(self):
        """The 23 s case: start 10→9 and end 30→32 together break a 20 s
        ceiling; snapping one end alone keeps it."""
        segs = [{"start": 9.0, "end": 32.0}]
        out = _snap_to_sentence_boundaries(self.WINDOW, segs, max_duration=20)
        assert out[0]["end"] - out[0]["start"] <= 20
        assert _snap_to_sentence_boundaries(self.WINDOW, segs)[0] == {
            "start": 9.0, "end": 32.0, "virality_score": 9}       # unpinned: both ends move

    def test_snap_within_the_ceiling_is_kept(self):
        out = _snap_to_sentence_boundaries(self.WINDOW, self.SEGMENTS, max_duration=25)
        assert out[0]["end"] == 32.0

    def test_already_oversized_window_is_still_tidied(self):
        """A window that arrives longer than the ceiling shouldn't freeze: the
        bar is "no worse than we started"."""
        over = [{"start": 10.0, "end": 31.0, "virality_score": 9}]
        out = _snap_to_sentence_boundaries(over, self.SEGMENTS, max_duration=15)
        assert out[0]["end"] - out[0]["start"] <= 21.0

    def test_no_segments_returns_input_untouched(self):
        out = _snap_to_sentence_boundaries(self.WINDOW, [], max_duration=20)
        assert out[0]["start"] == 10.0 and out[0]["end"] == 30.0

    def test_snap_never_collapses_a_clip(self):
        """Boundaries clustered at one point must not produce a zero-length
        window: the original survives instead."""
        segs = [{"start": 20.0, "end": 20.2}]
        win = [{"start": 20.0, "end": 21.0, "virality_score": 5}]
        out = _snap_to_sentence_boundaries(win, segs)
        assert out[0]["end"] - out[0]["start"] >= 1.0

    def test_the_input_is_not_mutated(self):
        win = [{"start": 10.0, "end": 30.0}]
        _snap_to_sentence_boundaries(win, self.SEGMENTS, max_duration=20)
        assert win == [{"start": 10.0, "end": 30.0}]


class _FakeVideo:
    """The DownloadedVideo attributes extract_viral_clips reads."""

    def __init__(self):
        self.id = "test_video_snap"
        self.title = "Talk"
        self.duration_seconds = 90
        self.video_path = "/tmp/fake.mp4"
        self.audio_path = "/tmp/fake.mp3"
        self.transcript_segments_json = None
        self.transcript = None
        self.transcript_language = None


class TestTheAiPathIsWired:
    """The fixes only matter if the pipeline passes the ceiling and runs the
    filter: drive the real extract_viral_clips with the AI pick, the
    transcription and the cutting stubbed, and look at what reaches the cut."""

    # Speech covers the whole 90 s (no silent-gap backfill), with segment
    # edges at 9 and 32 that pull the 10-30 pick out to 23 s when unpinned.
    SEGMENTS = [
        {"start": 0.0, "end": 9.0, "text": "opening words " * 6},
        {"start": 9.0, "end": 32.0, "text": "the main point of the talk " * 6},
        {"start": 32.0, "end": 90.0, "text": "the rest of the talk goes on " * 6},
    ]
    PICKS = [
        {"start": 10.0, "end": 30.0, "virality_score": 9, "title": "A"},
        {"start": 12.0, "end": 31.0, "virality_score": 7, "title": "B"},   # same seconds as A
    ]

    async def _windows_reaching_the_cut(self, opts):
        seen = {}

        async def capture(**kw):
            seen["windows"] = kw["clip_windows"]
            return []
        with patch.object(clip_extractor, "_load_or_transcribe_segments",
                          new=AsyncMock(return_value=self.SEGMENTS)), \
             patch.object(clip_extractor, "_select_clip_windows",
                          new=AsyncMock(return_value=[dict(p) for p in self.PICKS])), \
             patch.object(clip_extractor, "_process_clips_parallel", new=capture):
            try:
                await clip_extractor.extract_viral_clips(video=_FakeVideo(), user_settings=None, opts=opts)
            except Exception:  # noqa: BLE001 — no clip comes back from the stub; only the input matters
                pass
        return seen["windows"]

    @pytest.mark.asyncio
    async def test_a_pinned_max_survives_the_snap(self):
        windows = await self._windows_reaching_the_cut(ExtractOptions(max_clips=2, max_duration=20))
        assert windows and all(w["end"] - w["start"] <= 20 for w in windows), windows

    @pytest.mark.asyncio
    async def test_overlapping_picks_are_cut_once(self):
        windows = await self._windows_reaching_the_cut(ExtractOptions(max_clips=2))
        assert [w["title"] for w in windows] == ["A"]


class TestSnapRespectsPinnedMinDuration:
    """Snapping both ends INWARD shortens a clip by up to 4 s: a 20 s window
    became 16 s under a pinned 20 s minimum."""

    SEGMENTS = [{"start": 0.0, "end": 12.0}, {"start": 12.0, "end": 28.0}, {"start": 28.0, "end": 60.0}]
    WINDOW = [{"start": 10.0, "end": 30.0}]

    def test_unpinned_snap_still_tightens(self):
        assert _snap_to_sentence_boundaries(self.WINDOW, self.SEGMENTS)[0] == {"start": 12.0, "end": 28.0}

    def test_pinned_min_is_not_broken(self):
        out = _snap_to_sentence_boundaries(self.WINDOW, self.SEGMENTS, min_duration=20)
        assert out[0]["end"] - out[0]["start"] >= 20

    def test_a_snap_that_stays_above_the_floor_is_kept(self):
        assert _snap_to_sentence_boundaries(self.WINDOW, self.SEGMENTS, min_duration=15)[0] == {"start": 12.0, "end": 28.0}

    def test_an_already_short_window_is_still_tidied(self):
        short = [{"start": 10.0, "end": 27.0}]          # 17 s, under the 20 s floor before any snap
        out = _snap_to_sentence_boundaries(short, self.SEGMENTS, min_duration=20)
        assert out[0]["end"] - out[0]["start"] >= 17.0   # never shorter than it arrived

    @pytest.mark.asyncio
    async def test_the_ai_path_passes_the_floor(self):
        segs = [
            {"start": 0.0, "end": 12.0, "text": "opening words " * 6},
            {"start": 12.0, "end": 28.0, "text": "the main point of the talk " * 6},
            {"start": 28.0, "end": 90.0, "text": "the rest of the talk goes on " * 6},
        ]
        seen = {}

        async def capture(**kw):
            seen["windows"] = kw["clip_windows"]
            return []
        with patch.object(clip_extractor, "_load_or_transcribe_segments", new=AsyncMock(return_value=segs)), \
             patch.object(clip_extractor, "_select_clip_windows",
                          new=AsyncMock(return_value=[{"start": 10.0, "end": 30.0, "virality_score": 9, "title": "A"}])), \
             patch.object(clip_extractor, "_process_clips_parallel", new=capture):
            try:
                await clip_extractor.extract_viral_clips(
                    video=_FakeVideo(), user_settings=None, opts=ExtractOptions(max_clips=1, min_duration=20))
            except Exception:  # noqa: BLE001 — the stub returns no clips; only the input matters
                pass
        assert seen["windows"][0]["end"] - seen["windows"][0]["start"] >= 20
