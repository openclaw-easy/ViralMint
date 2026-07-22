# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend/services/broll_service.py.

The SaaS broll_service grew an FFmpeg auto-broll execution pipeline
(_plan_slots / _composite / add_auto_broll / _find_stock) that the OSS
build does not carry — those SaaS tests reference absent APIs and are not
ported. OSS broll_service exposes exactly two surfaces:

  • analyze_broll_timing — async AI planner (mock the ai_client, no network)
  • map_broll_to_timestamps — pure trigger-text → timestamp mapper

Both are exercised here.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services import broll_service as bs


# ── analyze_broll_timing (AI planner, mocked client) ────────────────────────

class TestAnalyzeBrollTiming:
    async def test_no_script_returns_none(self):
        assert await bs.analyze_broll_timing("", ai_client=MagicMock()) is None

    async def test_no_client_returns_none(self):
        assert await bs.analyze_broll_timing("some script", ai_client=None) is None

    async def test_parses_plain_json(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value='{"broll_cues": [], "total_broll_seconds": 0}')
        out = await bs.analyze_broll_timing("talk about cities", duration_seconds=60, ai_client=ai)
        assert out == {"broll_cues": [], "total_broll_seconds": 0}

    async def test_strips_code_fence(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value='```json\n{"broll_cues": [{"trigger_text": "x"}]}\n```')
        out = await bs.analyze_broll_timing("script", ai_client=ai)
        assert out["broll_cues"][0]["trigger_text"] == "x"

    async def test_ai_exception_returns_none(self):
        ai = MagicMock()
        ai.chat = AsyncMock(side_effect=RuntimeError("boom"))
        assert await bs.analyze_broll_timing("script", ai_client=ai) is None

    async def test_script_truncated_into_prompt(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="{}")
        long_script = "word " * 2000  # far over the 3000-char clamp
        await bs.analyze_broll_timing(long_script, ai_client=ai)
        sent = ai.chat.call_args.kwargs["messages"][0]["content"]
        # The service clamps the script to 3000 chars before formatting.
        assert long_script[:3000] in sent
        assert long_script not in sent  # full 10k-char script not embedded whole


# ── map_broll_to_timestamps (pure) ──────────────────────────────────────────

class TestMapBrollToTimestamps:
    def _words(self, spec):
        """spec: list of (text, start, end)."""
        return [{"word": t, "start": s, "end": e} for t, s, e in spec]

    def test_empty_inputs_return_empty(self):
        assert bs.map_broll_to_timestamps([], self._words([("hi", 0, 1)]), 10) == []
        assert bs.map_broll_to_timestamps([{"trigger_text": "hi"}], [], 10) == []

    def test_trigger_text_maps_to_word_start(self):
        words = self._words([
            ("intro", 0.0, 0.5), ("about", 0.5, 1.0),
            ("ocean", 1.0, 1.6), ("waves", 1.6, 2.2),
        ])
        cues = [{"trigger_text": "ocean waves", "duration_seconds": 3}]
        out = bs.map_broll_to_timestamps(cues, words, total_duration=2.2)
        assert len(out) == 1
        # start lands on/near the matched word; end = start + duration.
        assert out[0]["start_time"] >= 0
        assert round(out[0]["end_time"] - out[0]["start_time"], 2) == 3.0

    def test_missing_trigger_uses_position_pct(self):
        words = self._words([("a", 0.0, 1.0), ("b", 1.0, 2.0)])
        cues = [{"position_pct": 50, "duration_seconds": 2}]  # no trigger_text
        out = bs.map_broll_to_timestamps(cues, words, total_duration=10.0)
        assert out[0]["start_time"] == 5.0  # 50% of 10s
        assert out[0]["end_time"] == 7.0

    def test_unmatched_trigger_falls_back_to_position_pct(self):
        words = self._words([("hello", 0.0, 1.0), ("world", 1.0, 2.0)])
        cues = [{"trigger_text": "nonexistent phrase", "position_pct": 20,
                 "duration_seconds": 3}]
        out = bs.map_broll_to_timestamps(cues, words, total_duration=100.0)
        assert out[0]["start_time"] == 20.0  # 20% of 100s

    def test_original_cue_fields_preserved(self):
        words = self._words([("cats", 0.0, 1.0)])
        cues = [{"trigger_text": "cats", "duration_seconds": 2,
                 "search_query": "cat playing", "priority": "high"}]
        out = bs.map_broll_to_timestamps(cues, words, total_duration=1.0)
        assert out[0]["search_query"] == "cat playing"
        assert out[0]["priority"] == "high"

    def test_supports_text_key_alias(self):
        # word_timestamps may use "text" instead of "word".
        words = [{"text": "sunrise", "start": 4.0, "end": 5.0}]
        cues = [{"trigger_text": "sunrise", "duration_seconds": 1}]
        out = bs.map_broll_to_timestamps(cues, words, total_duration=5.0)
        assert len(out) == 1
        assert out[0]["end_time"] - out[0]["start_time"] == pytest.approx(1.0)
