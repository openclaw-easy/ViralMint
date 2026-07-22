# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for `_find_silent_gaps` — silent-region backfill window selection.

The function fills the un-AI'd portions of a source video with "this
section is silent" clip windows so the user gets coverage of the whole
upload even when speech is sparse. Two behaviours we lock in:

1. `min_gap_duration` is a hard lower bound on emitted clip length —
   gaps shorter than the user's pinned `min_duration` (which we forward
   as `min_gap_duration` from the call site in `extract_viral_clips`)
   are dropped, not silently emitted at sub-min length.

2. The user's `max_duration` caps individual clips; longer silent
   stretches are split into evenly-sized chunks below the cap.

Both behaviours are new on 2026-05-26 — pre-fix the function defaulted
`min_gap_duration=10.0`, so a user asking for 15-20s clips on a sparse
podcast would get 10s silent-section clips that violated the lower
bound. See _find_silent_gaps in backend/services/clip_extractor.py for
the implementation.
"""
from __future__ import annotations

import pytest

from backend.services.clip_extractor import _find_silent_gaps


def _seg(start: float, end: float, text: str = "hi") -> dict:
    return {"start": start, "end": end, "text": text}


class TestSilentGapsRespectsUserMin:
    """When the user pins `min_duration`, silent gaps below that floor
    must NOT be emitted. Pre-fix the hard-coded 10s floor would let
    10-15s gaps through even when the user asked for ≥15s clips."""

    def test_gap_below_user_min_is_dropped(self):
        # Source: 0-60s with one 12s gap between segments (10s-22s).
        # User pins min=15. The 12s gap is below 15, so we emit 0 gap clips.
        segments = [_seg(0, 10), _seg(22, 60)]
        out = _find_silent_gaps(
            segments=segments,
            clip_windows=[],
            duration=60.0,
            min_gap_duration=15.0,  # user pinned ≥ 15s
            max_clip_duration=60.0,
        )
        assert out == [], "12s gap should not emit when user requires ≥15s"

    def test_gap_above_user_min_emits(self):
        # Same shape, but the gap is now 20s (10s-30s). Above user's 15s
        # floor, below the 60s cap, so we get one window.
        segments = [_seg(0, 10), _seg(30, 60)]
        out = _find_silent_gaps(
            segments=segments,
            clip_windows=[],
            duration=60.0,
            min_gap_duration=15.0,
            max_clip_duration=60.0,
        )
        assert len(out) == 1
        assert out[0]["start"] == 10.0
        assert out[0]["end"] == 30.0

    def test_default_floor_is_10s_when_user_did_not_pin(self):
        """Auto-mode behaviour stays as it was: 10s floor when caller
        omits `min_gap_duration`. A regression here would mean the
        default changed and downstream callers (legacy code paths) would
        silently start dropping clips they used to keep."""
        segments = [_seg(0, 5), _seg(16, 30)]  # 11s gap
        out = _find_silent_gaps(
            segments=segments,
            clip_windows=[],
            duration=30.0,
            # min_gap_duration omitted → defaults to 10.0
            max_clip_duration=60.0,
        )
        assert len(out) == 1
        assert out[0]["end"] - out[0]["start"] == pytest.approx(11.0, abs=0.001)


class TestSilentGapsHonorsMaxCap:
    """Longer silent stretches are split into evenly-sized chunks below
    `max_clip_duration`. The pinned user max comes through verbatim from
    `extract_viral_clips`."""

    def test_long_gap_splits_below_user_max(self):
        # 50s gap with user max=20: ceil(50/20)=3 chunks of ~16.7s each.
        segments = [_seg(0, 5), _seg(55, 60)]
        out = _find_silent_gaps(
            segments=segments,
            clip_windows=[],
            duration=60.0,
            min_gap_duration=10.0,
            max_clip_duration=20.0,
        )
        assert len(out) == 3
        for w in out:
            length = w["end"] - w["start"]
            assert length <= 20.0, "chunk must not exceed user max"
            assert length >= 10.0, "chunk must clear the min_gap_duration floor"

    def test_short_gap_emits_single_window(self):
        # 15s gap with max=20 → one window, no split.
        segments = [_seg(0, 5), _seg(20, 25)]
        out = _find_silent_gaps(
            segments=segments,
            clip_windows=[],
            duration=25.0,
            min_gap_duration=10.0,
            max_clip_duration=20.0,
        )
        assert len(out) == 1


class TestSilentGapsAvoidsOverlap:
    """Silent windows must not overlap AI-selected clip windows. The
    function takes both `segments` (speech regions) AND `clip_windows`
    (AI picks) as "already covered" before computing gaps."""

    def test_ai_window_blocks_gap_emission_in_that_range(self):
        # Source 0-60s, single 5s of speech at 0-5. AI picked 20-40s as
        # a clip. Remaining "uncovered" stretches are 5-20s (15s) and
        # 40-60s (20s) — both above the 10s floor, so both should emit.
        segments = [_seg(0, 5)]
        ai_clips = [{"start": 20.0, "end": 40.0}]
        out = _find_silent_gaps(
            segments=segments,
            clip_windows=ai_clips,
            duration=60.0,
            min_gap_duration=10.0,
            max_clip_duration=60.0,
        )
        # Sort by start to make assertions deterministic
        out.sort(key=lambda w: w["start"])
        assert len(out) == 2
        # First window covers the gap between speech and AI clip
        assert out[0]["start"] == 5.0
        assert out[0]["end"] == 20.0
        # Second window covers post-AI-clip silence
        assert out[1]["start"] == 40.0
        assert out[1]["end"] == 60.0


class TestSilentGapsBudgetCap:
    """`budget` parameter limits how many silent windows can be returned.
    Used at the call site to cap total clips at `max_clips`."""

    def test_budget_stops_emission_early(self):
        # 60s source with 3 separate gaps; budget=1 → only the first emits.
        segments = [_seg(10, 12), _seg(25, 27), _seg(40, 42)]
        out = _find_silent_gaps(
            segments=segments,
            clip_windows=[],
            duration=60.0,
            min_gap_duration=5.0,
            max_clip_duration=60.0,
            budget=1,
        )
        assert len(out) == 1
