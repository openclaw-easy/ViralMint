# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for `_build_genre_bias_block` — the genre-bias prompt helper.

Same pattern as `_build_platform_bias_block` (see
test_clip_extractor_platform_bias.py for the original). The genre block
biases the AI's clip-selection criteria toward what makes a "good clip"
for a given content type — a podcast clip needs a standalone guest
insight, a tutorial needs a complete tip, etc.

Pure regex/dict lookups; no network.
"""
from __future__ import annotations

import pytest

from backend.services.clip_extractor import (
    _GENRE_ALIASES,
    _GENRE_BIAS,
    _build_genre_bias_block,
)


class TestBuildGenreBiasBlock:
    """Lock in the contract:
      - empty / None / unknown → "" (no bias)
      - known canonical key → returns the bias text
      - alias → resolves to canonical and returns that bias
    """

    def test_empty_returns_empty(self):
        assert _build_genre_bias_block("") == ""

    def test_none_returns_empty(self):
        assert _build_genre_bias_block(None) == ""

    def test_whitespace_only_returns_empty(self):
        assert _build_genre_bias_block("   ") == ""

    def test_unknown_returns_empty(self):
        # An unfamiliar string quietly degrades to no bias rather than
        # raising — frontends should be able to pass any user input
        # without first validating.
        assert _build_genre_bias_block("philosophy") == ""

    def test_known_canonical_keys(self):
        """All 8 declared genres must return non-empty bias text."""
        for key in _GENRE_BIAS:
            block = _build_genre_bias_block(key)
            assert block, f"genre {key!r} returned empty"
            assert "GENRE:" in block, f"genre {key!r} missing GENRE: marker"

    def test_case_insensitive(self):
        assert _build_genre_bias_block("PODCAST") == _build_genre_bias_block("podcast")
        assert _build_genre_bias_block("Tutorial") == _build_genre_bias_block("tutorial")

    def test_aliases_resolve(self):
        """Each alias must produce the same bias as its canonical key.
        Locks in the alias map so a rename can't silently break callers."""
        for alias, canonical in _GENRE_ALIASES.items():
            assert _build_genre_bias_block(alias) == _build_genre_bias_block(canonical), (
                f"alias {alias!r} should resolve to {canonical!r}"
            )

    def test_block_has_leading_newline(self):
        """The bias is concatenated with user_query_block + platform_bias_block
        inside the prompt template. Each block contributes its own leading
        newline so they stack cleanly. If this newline drifts, blocks run
        into each other and the AI sees malformed prompt structure."""
        block = _build_genre_bias_block("podcast")
        assert block.startswith("\n")
        assert block.endswith("\n")

    def test_specific_keys_have_distinct_text(self):
        """Two different genres must not return the same bias — otherwise
        the prompt is wasted tokens. Cheap sanity check."""
        bias_podcast = _build_genre_bias_block("podcast")
        bias_tutorial = _build_genre_bias_block("tutorial")
        bias_gaming = _build_genre_bias_block("gaming")
        assert bias_podcast != bias_tutorial
        assert bias_podcast != bias_gaming
        assert bias_tutorial != bias_gaming

    def test_genre_set_matches_expected(self):
        """If a future refactor adds or drops a genre, the frontend dropdown
        in ClipStudio.jsx must be updated too. Lock the set."""
        assert set(_GENRE_BIAS.keys()) == {
            "podcast", "interview", "qa", "vlog",
            "tutorial", "gaming", "reaction", "lecture",
        }
