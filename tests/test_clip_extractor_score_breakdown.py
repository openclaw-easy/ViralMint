# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for `_normalize_score_breakdown` — the 4-factor scoreboard
validator.

The AI returns a `score_breakdown` dict per clip with sub-scores for
flow / value / trend / shareability (each 1-10). This helper hardens the
shape before persistence:
  - Missing keys default to the clip's overall virality_score (so the UI
    bars don't collapse to zero on partial AI responses)
  - Out-of-range values clamp to [1, 10]
  - Non-numeric or non-dict input degrades gracefully

Pure function; no network. Tested in isolation because the live call
site is inside an async function that streams an AI response.
"""
from __future__ import annotations

import pytest

from backend.services.clip_extractor import (
    _SCORE_BREAKDOWN_KEYS,
    _normalize_score_breakdown,
)


class TestNormalizeScoreBreakdown:

    def test_keys_locked(self):
        """If the key tuple drifts, the prompt + UI + persistence all
        de-sync silently. Lock the exact 4 keys."""
        assert _SCORE_BREAKDOWN_KEYS == ("flow", "value", "trend", "shareability")

    def test_well_formed_input_passes_through(self):
        out = _normalize_score_breakdown(
            {"flow": 7, "value": 8, "trend": 6, "shareability": 9},
            default_score=5,
        )
        assert out == {"flow": 7.0, "value": 8.0, "trend": 6.0, "shareability": 9.0}

    def test_missing_key_uses_default(self):
        """The AI sometimes omits a key. Default to the clip's overall
        virality_score so the bar shows the same value as the chip."""
        out = _normalize_score_breakdown({"flow": 8, "value": 7}, default_score=6)
        assert out == {"flow": 8.0, "value": 7.0, "trend": 6.0, "shareability": 6.0}

    def test_empty_dict_uses_default_for_all(self):
        out = _normalize_score_breakdown({}, default_score=5.5)
        assert out == {"flow": 5.5, "value": 5.5, "trend": 5.5, "shareability": 5.5}

    def test_non_dict_returns_empty(self):
        """A list, None, or string means the AI gave us nothing usable —
        return {} so the UI hides the scoreboard for that clip rather
        than rendering fake bars at default_score."""
        assert _normalize_score_breakdown(None, default_score=5) == {}
        assert _normalize_score_breakdown([1, 2, 3], default_score=5) == {}
        assert _normalize_score_breakdown("hello", default_score=5) == {}
        assert _normalize_score_breakdown(42, default_score=5) == {}

    def test_clamps_to_1_10_range(self):
        """The AI sometimes emits 0 or >10; out-of-range values clamp
        rather than store garbage. 0 → 1; 99 → 10."""
        out = _normalize_score_breakdown(
            {"flow": 0, "value": 11, "trend": -3, "shareability": 100},
            default_score=5,
        )
        assert out["flow"] == 1.0
        assert out["value"] == 10.0
        assert out["trend"] == 1.0
        assert out["shareability"] == 10.0

    def test_non_numeric_falls_back_to_default(self):
        """A string or null in a sub-score field falls back to default
        rather than raising — robustness against AI shape drift."""
        out = _normalize_score_breakdown(
            {"flow": "high", "value": None, "trend": "8", "shareability": 7},
            default_score=6,
        )
        # "high" → can't parse → default(6)
        assert out["flow"] == 6.0
        # None → can't parse → default(6)
        assert out["value"] == 6.0
        # "8" → float("8") works → 8.0
        assert out["trend"] == 8.0
        # 7 → 7.0
        assert out["shareability"] == 7.0

    def test_float_values_preserved(self):
        """The AI can return decimals (e.g. 7.5). Don't truncate."""
        out = _normalize_score_breakdown(
            {"flow": 7.5, "value": 8.2, "trend": 6.9, "shareability": 9.1},
            default_score=5,
        )
        assert out == {"flow": 7.5, "value": 8.2, "trend": 6.9, "shareability": 9.1}

    def test_extra_keys_ignored(self):
        """If the AI invents a "creativity" key, drop it silently —
        we only persist the 4 documented sub-scores."""
        out = _normalize_score_breakdown(
            {"flow": 8, "value": 7, "trend": 6, "shareability": 9,
             "creativity": 10, "production_value": 4},
            default_score=5,
        )
        assert set(out.keys()) == set(_SCORE_BREAKDOWN_KEYS)
        assert "creativity" not in out

    def test_clamp_uses_strict_int_range(self):
        """Edge cases: exactly 1 and exactly 10 pass through unchanged
        (those are the labels' canonical endpoints)."""
        out = _normalize_score_breakdown(
            {"flow": 1, "value": 10, "trend": 1.0, "shareability": 10.0},
            default_score=5,
        )
        assert out == {"flow": 1.0, "value": 10.0, "trend": 1.0, "shareability": 10.0}
