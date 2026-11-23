# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Whisper splits a number into tokens with no leading space.

Measured with faster-whisper `base` on Edge TTS of "...up to 60,000 workers.
A jar costs $9.99 ... and 3.5 percent ...": the words came back as
' 60', ',000', ' $9', '.99', ' 3', '.5', '%'."""
import pytest

from backend.services.caption_service import _extract_word_timestamps

SEGMENTS = [{"start": 0.0, "end": 4.0, "words": [
    {"word": " up", "start": 0.0, "end": 0.2}, {"word": " to", "start": 0.2, "end": 0.4},
    {"word": " 60", "start": 0.4, "end": 0.8}, {"word": ",000", "start": 0.8, "end": 1.2},
    {"word": " workers.", "start": 1.2, "end": 1.6}, {"word": " $9", "start": 1.6, "end": 2.0},
    {"word": ".99", "start": 2.0, "end": 2.4}, {"word": " 3", "start": 2.4, "end": 2.6},
    {"word": ".5", "start": 2.6, "end": 2.8}, {"word": "%", "start": 2.8, "end": 3.0},
]}]


@pytest.mark.xfail(strict=True, reason=(
    "GAP: _extract_word_timestamps strips each Whisper token and keeps it as its own caption word, "
    "so a token with no leading space that continues the word before it (',000', '.99', '.5', '%') "
    "becomes a separate word: word-by-word captions (Clipper, the Captions tool) read '60 ,000', "
    "'$9 .99' and '3 .5 %', and the highlight steps onto ',000' alone. Reachable on any spoken "
    "number with a separator or a decimal. Hosted's caption builder has the same code."))
def test_a_number_whisper_heard_in_two_tokens_is_one_caption_word():
    texts = [w["text"] for w in _extract_word_timestamps(SEGMENTS)]
    assert texts == ["up", "to", "60,000", "workers.", "$9.99", "3.5%"]
