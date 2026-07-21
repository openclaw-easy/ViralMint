# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for the clipper hardening + structured-scoring ports.

Covers the bug fixes and new selection knobs consolidated from the hosted
variant: the realistic clip-count estimator, manual-mode timestamp parsing +
range validation, platform/genre bias blocks, hook-type validation, and the
score-breakdown / topic-dedup helpers.
"""
import pytest
from fastapi import HTTPException

from backend.services import clip_extractor as ce
from backend.api import downloaded as dl


def _dense_segments(seconds: int, step: float = 3.0) -> list[dict]:
    n = int(seconds / step)
    return [
        {"text": f"word{i} plus a few more spoken words here", "start": i * step, "end": i * step + step}
        for i in range(n)
    ]


class TestClipCountEstimator:
    def test_short_source_yields_multiple_clips(self):
        # Regression: the old estimator assumed 40s clips, collapsing a 63s
        # source to a single clip. With min_duration=15 it must allow >=2.
        segs = _dense_segments(63)
        assert ce._estimate_realistic_clip_count(segs, 63, 6, 15) >= 2

    def test_estimator_never_exceeds_requested_max(self):
        segs = _dense_segments(600)
        assert ce._estimate_realistic_clip_count(segs, 600, 3, 15) <= 3

    def test_cjk_detection(self):
        assert ce._has_cjk("你好世界") is True
        assert ce._has_cjk("hello world") is False


class TestParseTimestamp:
    def test_seconds(self):
        assert ce._parse_timestamp("90") == 90
        assert ce._parse_timestamp("7.5") == 7.5

    def test_mm_ss(self):
        assert ce._parse_timestamp("1:30") == 90

    def test_hh_mm_ss(self):
        assert ce._parse_timestamp("1:00:00") == 3600


class TestBiasBlocks:
    def test_known_platform_non_empty(self):
        assert len(ce._build_platform_bias_block("tiktok")) > 10

    def test_known_genre_non_empty(self):
        assert len(ce._build_genre_bias_block("podcast")) > 10

    def test_none_and_unknown_are_empty(self):
        assert ce._build_platform_bias_block(None) == ""
        assert ce._build_platform_bias_block("nope-not-a-platform") == ""
        assert ce._build_genre_bias_block(None) == ""


class TestHookTypes:
    def test_general_is_allowed(self):
        assert "general" in ce._ALLOWED_HOOK_TYPES

    def test_expected_hook_types_present(self):
        for t in ("curiosity_gap", "contrarian", "emotional_peak"):
            assert t in ce._ALLOWED_HOOK_TYPES


class TestScoreBreakdown:
    def test_partial_filled_with_default(self):
        out = ce._normalize_score_breakdown({"flow": 9}, 7.0)
        assert out["flow"] == 9.0
        assert out["value"] == 7.0 and out["trend"] == 7.0 and out["shareability"] == 7.0

    def test_garbage_yields_empty(self):
        assert ce._normalize_score_breakdown("not a dict", 6.0) == {}


class TestTopicDedup:
    def test_duplicate_topics_collapse(self):
        windows = [
            {"title": "A cat", "hook": "cats are great", "reason": "cat content"},
            {"title": "A cat", "hook": "cats are great", "reason": "cat content"},
            {"title": "Dog stuff", "hook": "dogs run fast", "reason": "dog content today"},
        ]
        assert len(ce._dedupe_clips_by_topic(windows)) == 2

    def test_distinct_topics_kept(self):
        windows = [
            {"title": "Cooking pasta", "hook": "boil water first", "reason": "kitchen basics"},
            {"title": "Fixing bikes", "hook": "check the chain", "reason": "repair guide"},
        ]
        assert len(ce._dedupe_clips_by_topic(windows)) == 2


class TestManualRangeValidation:
    def test_valid_ranges_normalized(self):
        out = dl._validate_manual_time_ranges(
            [{"start": 0, "end": 10}, {"start": 12, "end": 20}], 60.0
        )
        assert out == [{"start": 0.0, "end": 10.0}, {"start": 12.0, "end": 20.0}]

    def test_too_many_ranges_rejected(self):
        with pytest.raises(HTTPException):
            dl._validate_manual_time_ranges(
                [{"start": 0, "end": 5}] * (dl._MANUAL_MAX_RANGES + 1), 600.0
            )

    def test_end_before_start_rejected(self):
        with pytest.raises(HTTPException):
            dl._validate_manual_time_ranges([{"start": 5, "end": 3}], 60.0)

    def test_range_beyond_duration_rejected(self):
        with pytest.raises(HTTPException):
            dl._validate_manual_time_ranges([{"start": 0, "end": 100}], 60.0)
