# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for the channel-baseline outlier detection in
backend/services/outlier_detection_service.py.

The outlier tiers (3× / 5× / 10× / 20× channel median) are
user-facing — they drive the colored chips on every scout-result
card (CLAUDE.md §7) and appear in many of the AI prompts. The exact
thresholds matter; a regression here changes what users see.
"""
from __future__ import annotations

import pytest

from backend.services.outlier_detection_service import (
    OUTLIER_THRESHOLDS,
    classify_outlier,
    compute_outlier_scores,
    compute_channel_stats,
)


# ─────────────────────────────────────────────────────────────────────────────
# classify_outlier — the tier mapping
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyOutlierTiers:
    """The four tier thresholds documented in CLAUDE.md §7. Locking the
    exact numbers down so a refactor can't silently rebalance them."""

    def test_below_3x_returns_none(self):
        assert classify_outlier(0.5) is None
        assert classify_outlier(1.0) is None
        assert classify_outlier(2.99) is None

    def test_exactly_3x_is_outlier(self):
        assert classify_outlier(3.0) == "OUTLIER"

    def test_3_to_5_band_is_outlier(self):
        assert classify_outlier(3.5) == "OUTLIER"
        assert classify_outlier(4.9) == "OUTLIER"

    def test_exactly_5x_is_strong(self):
        assert classify_outlier(5.0) == "STRONG"

    def test_5_to_10_band_is_strong(self):
        assert classify_outlier(7.5) == "STRONG"
        assert classify_outlier(9.99) == "STRONG"

    def test_exactly_10x_is_breakout(self):
        assert classify_outlier(10.0) == "BREAKOUT"

    def test_10_to_20_band_is_breakout(self):
        assert classify_outlier(15.0) == "BREAKOUT"
        assert classify_outlier(19.99) == "BREAKOUT"

    def test_exactly_20x_is_monster(self):
        assert classify_outlier(20.0) == "MONSTER"

    def test_far_above_20x_is_still_monster(self):
        assert classify_outlier(50.0) == "MONSTER"
        assert classify_outlier(1000.0) == "MONSTER"

    def test_zero_returns_none(self):
        assert classify_outlier(0.0) is None

    def test_none_input_returns_none(self):
        assert classify_outlier(None) is None  # type: ignore[arg-type]

    def test_thresholds_list_matches_implementation(self):
        """If anyone reorders OUTLIER_THRESHOLDS the tier assignment
        silently breaks. Lock the order (descending threshold)."""
        thresholds = [t for t, _ in OUTLIER_THRESHOLDS]
        assert thresholds == sorted(thresholds, reverse=True)
        # The 4 documented tiers exactly.
        assert thresholds == [20.0, 10.0, 5.0, 3.0]


# ─────────────────────────────────────────────────────────────────────────────
# compute_outlier_scores — the enrichment loop
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeOutlierScores:
    def test_uses_channel_median_when_provided(self):
        videos = [
            {"views": 10000},    # 1× median
            {"views": 50000},    # 5× median → STRONG
            {"views": 200000},   # 20× median → MONSTER
        ]
        out = compute_outlier_scores(videos, channel_median=10000)
        assert out[0]["outlier_score"] == 1.0
        assert out[0]["outlier_label"] is None
        assert out[1]["outlier_score"] == 5.0
        assert out[1]["outlier_label"] == "STRONG"
        assert out[2]["outlier_score"] == 20.0
        assert out[2]["outlier_label"] == "MONSTER"

    def test_falls_back_to_channel_avg_when_no_median(self):
        videos = [{"views": 30000}]  # 3× the avg baseline of 10000
        out = compute_outlier_scores(videos, channel_avg=10000)
        assert out[0]["outlier_score"] == 3.0
        assert out[0]["outlier_label"] == "OUTLIER"

    def test_falls_back_to_subscriber_heuristic_when_no_baseline(self):
        # 3% of 1000 subscribers = 30 baseline; 300 views = 10× → BREAKOUT
        videos = [{"views": 300}]
        out = compute_outlier_scores(videos, subscriber_count=1000)
        assert out[0]["outlier_label"] == "BREAKOUT"

    def test_no_baseline_at_all_yields_null_score(self):
        videos = [{"views": 50000}]
        out = compute_outlier_scores(videos)
        assert out[0]["outlier_score"] is None
        assert out[0]["outlier_label"] is None

    def test_zero_views_yields_null_score(self):
        videos = [{"views": 0}]
        out = compute_outlier_scores(videos, channel_median=10000)
        assert out[0]["outlier_score"] is None
        assert out[0]["outlier_label"] is None

    def test_accepts_view_count_field_as_alias(self):
        """Some upstream paths use `view_count` (YouTube API shape)
        instead of `views`. Both should work — this is the row format
        the channels endpoint returns."""
        videos = [{"view_count": 50000}]
        out = compute_outlier_scores(videos, channel_median=10000)
        assert out[0]["outlier_score"] == 5.0
        assert out[0]["outlier_label"] == "STRONG"

    def test_mutates_input_list_in_place_and_returns_same_ref(self):
        """The function is documented to mutate + return the same list.
        UI code depends on this — it passes a list it wants enriched."""
        videos = [{"views": 30000}]
        out = compute_outlier_scores(videos, channel_median=10000)
        assert out is videos
        assert "outlier_score" in videos[0]

    def test_records_channel_avg_views_on_each_video(self):
        """The UI displays "X× channel median" alongside the score —
        needs channel_avg_views per row, even when the baseline came
        from channel_median or subscriber heuristic."""
        videos = [{"views": 30000}]
        out = compute_outlier_scores(videos, channel_median=10000)
        assert out[0]["channel_avg_views"] == 10000

    def test_subscriber_heuristic_rounds_baseline(self):
        videos = [{"views": 90}]  # 3× the 30 baseline (3% of 1000)
        out = compute_outlier_scores(videos, subscriber_count=1000)
        assert out[0]["channel_avg_views"] == 30


# ─────────────────────────────────────────────────────────────────────────────
# compute_channel_stats — median / avg from a view-count series
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeChannelStats:
    def test_returns_median_and_avg(self):
        stats = compute_channel_stats([100, 200, 300, 400, 500])
        assert stats["median_views"] == 300
        assert stats["avg_views"] == 300

    def test_handles_single_video(self):
        stats = compute_channel_stats([42])
        assert stats["median_views"] == 42
        assert stats["avg_views"] == 42

    def test_handles_empty_list(self):
        """Empty channel = zero baseline. Don't crash."""
        stats = compute_channel_stats([])
        assert stats["median_views"] == 0
        assert stats["avg_views"] == 0

    def test_median_robust_against_one_viral_spike(self):
        """Why median > avg for this metric: one 100× viral video
        shouldn't pull the baseline up and hide the next outlier."""
        view_counts = [100, 100, 100, 100, 10_000_000]
        stats = compute_channel_stats(view_counts)
        assert stats["median_views"] == 100
        # avg would be ~2M — way off the baseline most videos sit at
        assert stats["avg_views"] > 1_000_000
