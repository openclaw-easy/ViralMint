# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for `_dedupe_clips_by_topic` — topic-keyword Jaccard dedup.

The AI picker sometimes returns multiple clips that don't overlap in TIME
but cover the same TOPIC (a guest retelling a story; a host restating a
punchline). `_remove_overlapping_clips` handles time overlap;
`_dedupe_clips_by_topic` handles topic overlap so the user gets distinct
beats in their N final clips.

Pure function; no network, no AI calls.
"""
from __future__ import annotations

import pytest

from backend.services.clip_extractor import (
    TOPIC_DEDUP_THRESHOLD,
    _dedupe_clips_by_topic,
    _topic_keywords,
    _TOPIC_STOPWORDS,
)


# ── _topic_keywords ─────────────────────────────────────────────────────────


class TestTopicKeywords:
    def test_empty_input_returns_empty_set(self):
        assert _topic_keywords("") == set()
        assert _topic_keywords(None) == set()  # type: ignore[arg-type]

    def test_extracts_content_words(self):
        kw = _topic_keywords("The founder shipped a new SaaS product overnight")
        # Lowercased, alphabetic-only, length >= 4, stopwords filtered
        assert "founder" in kw
        assert "shipped" in kw
        assert "product" in kw
        assert "overnight" in kw

    def test_filters_stopwords(self):
        """The stopword set drops common high-frequency words that would
        inflate similarity between unrelated clips."""
        kw = _topic_keywords("This video has a great moment about the AI")
        # 'this', 'video', 'moment', 'about', 'great' are all stopwords;
        # 'has' is too short. After filter, almost nothing meaningful left.
        # All those words must NOT appear:
        for word in ("this", "video", "moment", "about", "great"):
            assert word not in kw

    def test_filters_short_tokens(self):
        """Tokens under 4 chars are dropped to filter conjunctions /
        articles / prepositions language-agnostically."""
        kw = _topic_keywords("a ai of in on to is be it I he we us")
        assert kw == set()

    def test_lowercases(self):
        kw = _topic_keywords("Tesla Robotaxi Launch")
        assert "tesla" in kw
        assert "robotaxi" in kw
        assert "launch" in kw
        # Original case must not survive
        assert "Tesla" not in kw

    def test_strips_non_alpha(self):
        """Numbers and punctuation are excluded — they generate noise
        rather than topic signal."""
        kw = _topic_keywords("$50M Series A 2026!")
        # Series, A would be kept by length filter; "$50M" tokenizes to nothing
        # at length≥4 since "50m" is digit-mixed.
        assert "series" in kw

    def test_stopwords_curated_set(self):
        """Lock the stopword set so a future edit doesn't silently leak
        common words into the keyword vocab."""
        # A few sentinel words that must be present
        for sw in ("video", "clip", "moment", "this", "that", "with"):
            assert sw in _TOPIC_STOPWORDS, f"{sw!r} should be a stopword"


# ── _dedupe_clips_by_topic ──────────────────────────────────────────────────


class TestDedupeClipsByTopic:
    """The contract: walk top-to-bottom of the (already sorted by virality)
    list; drop any clip with Jaccard ≥ threshold against an already-kept
    clip. Higher-virality wins."""

    def _clip(self, start, end, virality, title="", hook="", reason=""):
        return {
            "start": start, "end": end,
            "virality_score": virality,
            "title": title, "hook": hook, "reason": reason,
        }

    def test_single_clip_unchanged(self):
        clips = [self._clip(0, 30, 8.0, title="Founder built SaaS")]
        assert _dedupe_clips_by_topic(clips) == clips

    def test_empty_input(self):
        assert _dedupe_clips_by_topic([]) == []

    def test_drops_near_duplicate_lower_score_wins_higher(self):
        """Two clips on the same topic. The lower-virality one must be
        dropped; input order is high-then-low after sort by virality desc."""
        a = self._clip(
            0, 30, 9.0,
            title="founder built saas overnight",
            hook="he shipped a profitable saas in one weekend",
            reason="practical builder shipping in 48 hours",
        )
        # Same topic, near-identical keywords
        b = self._clip(
            120, 150, 6.0,
            title="founder shipped saas overnight",
            hook="he built a profitable saas product in one weekend",
            reason="builder shipping practical saas in 48 hours",
        )
        result = _dedupe_clips_by_topic([a, b])
        assert len(result) == 1
        assert result[0] is a

    def test_keeps_distinct_topics(self):
        """Two clips with no keyword overlap stay both kept."""
        a = self._clip(0, 30, 8.0, title="tesla autopilot crash investigation")
        b = self._clip(120, 150, 7.5, title="boston dynamics atlas backflip demo")
        result = _dedupe_clips_by_topic([a, b])
        assert len(result) == 2

    def test_preserves_order(self):
        """Surviving clips return in their input order, not re-sorted."""
        a = self._clip(0, 30, 9.0, title="tesla autopilot crash")
        b = self._clip(60, 90, 8.0, title="boston dynamics atlas")
        c = self._clip(120, 150, 7.0, title="openai gpt5 release")
        result = _dedupe_clips_by_topic([a, b, c])
        assert result == [a, b, c]

    def test_dedup_uses_only_text_fields(self):
        """Time fields (start/end) don't affect dedup — that's the whole
        point (those are handled by _remove_overlapping_clips). Two clips
        with the same text but distant time still get deduped."""
        a = self._clip(0, 30, 9.0, title="ai agent buys groceries")
        b = self._clip(900, 930, 5.0, title="ai agent buys groceries")
        result = _dedupe_clips_by_topic([a, b])
        assert len(result) == 1
        assert result[0] is a

    def test_clip_with_empty_text_kept(self):
        """A clip with no usable keywords (empty title/hook/reason) can't
        be compared, so it stays in the result rather than getting silently
        dropped. Rare; happens on legacy/partial AI responses."""
        empty_clip = self._clip(0, 30, 8.0)
        normal = self._clip(60, 90, 7.0, title="machine learning explainer")
        result = _dedupe_clips_by_topic([empty_clip, normal])
        assert len(result) == 2

    def test_threshold_constant_locked(self):
        """If the threshold drifts, the user-visible behavior changes
        (more dupes get through, or more distinct clips get dropped).
        Lock at 0.55."""
        assert TOPIC_DEDUP_THRESHOLD == 0.55

    def test_high_threshold_keeps_more(self):
        """Sanity check: bumping the threshold past 0.99 effectively
        disables dedup."""
        a = self._clip(0, 30, 9.0, title="founder built saas overnight quickly")
        b = self._clip(120, 150, 6.0, title="founder built saas overnight fast")
        # With threshold 0.99 these stay separate
        result = _dedupe_clips_by_topic([a, b], threshold=0.99)
        assert len(result) == 2
