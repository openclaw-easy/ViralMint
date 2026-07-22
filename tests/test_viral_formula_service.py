# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend/services/viral_formula_service.py.

NOTE ON SCOPE: the task brief described "pure scoring logic (engagement
rate, recency bonus, views/likes scores, 0-100 clamp)" — but in the OSS
tree that formula lives in `backend/agents/scout.compute_virality_score`
and is already covered by tests/test_scout_scoring.py. The OSS
viral_formula_service module contains only the cross-video AI formula
generator `generate_viral_formula`, so this file unit-tests THAT module's
deterministic behavior (min-analysis gate, trimming, code-fence stripping,
JSON parsing, failure fail-open) with a mocked ai_client — no network.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services import viral_formula_service as vf


def _analyses(n):
    return [
        {"hook": f"hook{i}", "structure": "listicle", "tone": "casual",
         "why_viral": "curiosity", "scores": {"virality": 70 + i},
         "suggested_angle": "angle", "key_phrases": ["a", "b"]}
        for i in range(n)
    ]


class TestGenerateViralFormula:
    async def test_too_few_analyses_returns_none_without_calling_ai(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="{}")
        out = await vf.generate_viral_formula("finance", _analyses(2), ai)
        assert out is None
        ai.chat.assert_not_called()  # short-circuits before the AI call

    async def test_parses_plain_json(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value='{"winning_formula_summary": "do X"}')
        out = await vf.generate_viral_formula("finance", _analyses(3), ai)
        assert out == {"winning_formula_summary": "do X"}

    async def test_strips_code_fence(self):
        ai = MagicMock()
        ai.chat = AsyncMock(
            return_value='```json\n{"hook_patterns": {"confidence": 0.8}}\n```')
        out = await vf.generate_viral_formula("gaming", _analyses(5), ai)
        assert out["hook_patterns"]["confidence"] == 0.8

    async def test_ai_exception_returns_none(self):
        ai = MagicMock()
        ai.chat = AsyncMock(side_effect=RuntimeError("upstream down"))
        out = await vf.generate_viral_formula("gaming", _analyses(4), ai)
        assert out is None

    async def test_caps_at_15_analyses_in_prompt(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="{}")
        await vf.generate_viral_formula("news", _analyses(30), ai)
        prompt = ai.chat.call_args.kwargs["messages"][0]["content"]
        # FORMULA_PROMPT is formatted with n = len(trimmed) which is capped at 15.
        assert "analyzed 15 competitor videos" in prompt

    async def test_niche_injected_into_prompt(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="{}")
        await vf.generate_viral_formula("underwater basket weaving", _analyses(3), ai)
        prompt = ai.chat.call_args.kwargs["messages"][0]["content"]
        assert "underwater basket weaving" in prompt
