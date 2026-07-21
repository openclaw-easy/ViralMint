# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for the chat/planner + pipeline ports from the hosted variant.

Covers the <quick_replies> JSON parsing, the chunked transcript-correction
splitter (which fixed silent data loss on long videos), the ExtractOptions
dataclass, and the music-mix voiceover-level fix.
"""
import inspect

from backend.agents import planner as pl
from backend.agents import analyzer as an
from backend.services.clip_options import ExtractOptions


class TestQuickReplies:
    def test_parses_json_array(self):
        r = pl._parse_quick_replies('prefix <quick_replies>["Yes","No","Maybe"]</quick_replies> tail')
        assert r == ["Yes", "No", "Maybe"]

    def test_missing_block_is_empty(self):
        assert pl._parse_quick_replies("no chips here") == []

    def test_malformed_json_is_empty(self):
        assert pl._parse_quick_replies("<quick_replies>not json</quick_replies>") == []

    def test_non_list_is_empty(self):
        assert pl._parse_quick_replies('<quick_replies>{"a":1}</quick_replies>') == []


class TestTranscriptChunking:
    def test_long_text_splits_and_preserves_content(self):
        text = "This is a spoken sentence. " * 400  # well over one chunk
        chunks = an._split_for_correction(text)
        assert len(chunks) >= 2
        # No content lost: every chunk stays within budget and the sentence
        # count is conserved across the split.
        assert all(len(c) <= an.CORRECTION_CHUNK_CHARS + 200 for c in chunks)
        assert sum(c.count("spoken sentence") for c in chunks) == text.count("spoken sentence")

    def test_short_text_single_chunk(self):
        chunks = an._split_for_correction("Just one short sentence.")
        assert len(chunks) == 1


class TestExtractOptions:
    def test_constructs_with_defaults(self):
        opts = ExtractOptions()
        assert hasattr(opts, "mode")
        assert opts.remove_silence in (False, None)
        assert opts.user_query in (None, "")

    def test_round_trips_values(self):
        opts = ExtractOptions(mode="manual", max_clips=5, target_platform="tiktok", genre="podcast")
        assert opts.mode == "manual"
        assert opts.max_clips == 5
        assert opts.target_platform == "tiktok"
        assert opts.genre == "podcast"


class TestMusicMixFilter:
    def test_amix_disables_normalize_and_limits(self):
        # Regression guard: amix's default normalize=1 halved the voiceover.
        # The mix filter must set normalize=0 and add an alimiter peak guard.
        from backend.services import music_service as ms

        src = inspect.getsource(ms)
        assert "normalize=0" in src
        assert "alimiter" in src
