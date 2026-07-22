# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for the target_platform hook-bias helper in clip_extractor.

`_build_platform_bias_block(target_platform)` is the pure-function
mapping that turns a platform-name string into the bias text injected
into CLIP_SELECTION_PROMPT. Locking the behavior down here:

  - Empty / None / unknown → empty string (vanilla virality ranking)
  - Known canonical keys (tiktok / youtube_shorts / reels /
    linkedin / twitter) → bias text containing the expected
    hook-type emphasis
  - Aliases (youtube / instagram / x) → resolve to the canonical
    key and return the same bias

If a future refactor renames the platform keys or drops an alias,
these tests catch the regression before it ships.
"""
from __future__ import annotations

import pytest

from backend.services.clip_extractor import (
    _PLATFORM_ALIASES,
    _PLATFORM_BIAS,
    _build_platform_bias_block,
)


class TestBuildPlatformBiasBlock:
    """Pure-function contract for the helper. All branches covered."""

    def test_none_returns_empty(self):
        assert _build_platform_bias_block(None) == ""

    def test_empty_string_returns_empty(self):
        assert _build_platform_bias_block("") == ""

    def test_whitespace_only_returns_empty(self):
        assert _build_platform_bias_block("   ") == ""

    def test_unknown_platform_returns_empty(self):
        # Unknown values quietly degrade — the caller doesn't have to
        # check known-platform list before passing through.
        assert _build_platform_bias_block("myspace") == ""
        assert _build_platform_bias_block("yo") == ""

    def test_tiktok_includes_shocking_emotional_contrarian(self):
        out = _build_platform_bias_block("tiktok")
        assert "TARGET PLATFORM: TikTok" in out
        assert "shocking_claim" in out
        assert "emotional_peak" in out
        assert "contrarian" in out

    def test_youtube_shorts_includes_curiosity_numbers_story(self):
        out = _build_platform_bias_block("youtube_shorts")
        assert "TARGET PLATFORM: YouTube Shorts" in out
        assert "curiosity_gap" in out
        assert "number_promise" in out
        assert "story_loop" in out

    def test_reels_includes_emotional_story_curiosity(self):
        out = _build_platform_bias_block("reels")
        assert "TARGET PLATFORM: Instagram Reels" in out
        assert "emotional_peak" in out
        assert "story_loop" in out

    def test_linkedin_includes_actionable_numbers_contrarian(self):
        out = _build_platform_bias_block("linkedin")
        assert "TARGET PLATFORM: LinkedIn" in out
        assert "actionable_tip" in out
        assert "number_promise" in out
        assert "contrarian" in out
        # LinkedIn specifically avoids clickbait
        assert "clickbait" in out.lower() or "professional" in out.lower()

    def test_twitter_includes_shocking_contrarian_question(self):
        out = _build_platform_bias_block("twitter")
        assert "TARGET PLATFORM: Twitter" in out
        assert "shocking_claim" in out
        assert "contrarian" in out
        assert "question" in out

    # ── Alias resolution ────────────────────────────────────────────────

    def test_youtube_aliases_to_youtube_shorts(self):
        # The dropdown surfaces "youtube_shorts" but the MCP tool / agent
        # callers might say just "youtube". Both must work.
        out = _build_platform_bias_block("youtube")
        assert "TARGET PLATFORM: YouTube Shorts" in out

    def test_shorts_aliases_to_youtube_shorts(self):
        out = _build_platform_bias_block("shorts")
        assert "TARGET PLATFORM: YouTube Shorts" in out

    def test_instagram_aliases_to_reels(self):
        out = _build_platform_bias_block("instagram")
        assert "TARGET PLATFORM: Instagram Reels" in out

    def test_instagram_reels_alias_resolves(self):
        out = _build_platform_bias_block("instagram_reels")
        assert "TARGET PLATFORM: Instagram Reels" in out

    def test_x_aliases_to_twitter(self):
        out = _build_platform_bias_block("x")
        assert "TARGET PLATFORM: Twitter" in out

    # ── Case + whitespace handling ──────────────────────────────────────

    def test_uppercase_platform_resolves(self):
        # The dropdown's `value` field is lowercase, but the MCP tool
        # might receive a value the LLM hand-typed.
        out = _build_platform_bias_block("TIKTOK")
        assert "TARGET PLATFORM: TikTok" in out

    def test_mixed_case_with_whitespace_resolves(self):
        out = _build_platform_bias_block("  LinkedIn  ")
        assert "TARGET PLATFORM: LinkedIn" in out

    # ── Prompt-format-string compatibility ─────────────────────────────

    def test_return_starts_with_newline_when_set(self):
        """The injected block must start with a newline so it doesn't
        collide with the preceding `user_query_block` in the prompt
        format. (Empty-return case doesn't add anything.)"""
        out = _build_platform_bias_block("tiktok")
        assert out.startswith("\n")
        assert out.endswith("\n")

    def test_empty_string_does_not_crash_format(self):
        """The CLIP_SELECTION_PROMPT has `{platform_bias_block}` in it.
        An empty string must format cleanly without leaving stray
        characters. Also passes empty `genre_bias_block` (added 2026-05-18
        for Tier A genre-aware clip selection)."""
        from backend.services.clip_extractor import CLIP_SELECTION_PROMPT
        # This shouldn't raise, even with all-empty contextual blocks.
        formatted = CLIP_SELECTION_PROMPT.format(
            max_clips=5,
            min_clip=15,
            max_clip=60,
            title="Test",
            duration=300,
            niche="general",
            segments_text="...",
            user_query_block="",
            platform_bias_block="",
            genre_bias_block="",
        )
        # Sanity: the placeholders are gone from the formatted output.
        assert "{platform_bias_block}" not in formatted
        assert "{user_query_block}" not in formatted
        assert "{genre_bias_block}" not in formatted

    def test_bias_appears_in_full_prompt_when_set(self):
        """Integration check: the bias text actually shows up inside a
        fully-formatted prompt, not just from the helper in isolation."""
        from backend.services.clip_extractor import CLIP_SELECTION_PROMPT
        bias = _build_platform_bias_block("tiktok")
        prompt = CLIP_SELECTION_PROMPT.format(
            max_clips=5,
            min_clip=15,
            max_clip=60,
            title="Test",
            duration=300,
            niche="general",
            segments_text="...",
            user_query_block="",
            platform_bias_block=bias,
            genre_bias_block="",
        )
        assert "TARGET PLATFORM: TikTok" in prompt
        assert "shocking_claim" in prompt


class TestPlatformDataIntegrity:
    """Invariants on the data tables — catch silent renames / drops."""

    def test_canonical_keys_present(self):
        """All 5 canonical platform keys exist. If a refactor renames
        one, the UI dropdown + the MCP tool docstring would silently
        emit dead keys."""
        for key in ("tiktok", "youtube_shorts", "reels", "linkedin", "twitter"):
            assert key in _PLATFORM_BIAS, f"missing canonical key: {key}"

    def test_alias_targets_are_canonical_keys(self):
        """Every alias must point at a real key in _PLATFORM_BIAS.
        Without this, a dead alias would route to no-bias silently."""
        for alias, target in _PLATFORM_ALIASES.items():
            assert target in _PLATFORM_BIAS, (
                f"alias {alias!r} points at non-existent key {target!r}"
            )

    def test_every_bias_mentions_a_canonical_hook_type(self):
        """The prompts in CLIP_SELECTION_PROMPT use a fixed enum of
        hook_type values. Every platform bias should reference at
        least one of them so the AI has something concrete to act on."""
        valid_hooks = {
            "curiosity_gap", "contrarian", "emotional_peak", "question",
            "number_promise", "story_loop", "actionable_tip",
            "shocking_claim", "general",
        }
        for key, bias in _PLATFORM_BIAS.items():
            assert any(h in bias for h in valid_hooks), (
                f"{key} bias references no canonical hook_type"
            )
