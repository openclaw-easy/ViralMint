# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Whisper splits a number into tokens with no leading space; a caption word is the whole number.

Measured with faster-whisper `base` on Edge TTS of "...up to 60,000 workers.
A jar costs $9.99 ... and 3.5 percent ...": the words came back as
' 60', ',000', ' $9', '.99', ' 3', '.5', '%', and each token was its own
caption word ("60 ,000", "$9 .99")."""
from backend.services.caption_service import _extract_word_timestamps


def _seg(words):
    return [{"start": 0.0, "end": 9.0, "words": [
        {"word": w, "start": i * 0.2, "end": i * 0.2 + 0.2} for i, w in enumerate(words)]}]


def test_a_number_whisper_heard_in_two_tokens_is_one_caption_word():
    out = _extract_word_timestamps(_seg([" up", " to", " 60", ",000", " workers.", " $9", ".99", " 3", ".5", "%"]))
    assert [w["text"] for w in out] == ["up", "to", "60,000", "workers.", "$9.99", "3.5%"]
    sixty = out[2]
    assert (sixty["start"], sixty["end"]) == (0.4, 0.8)          # the first token's start, the last token's end


def test_a_word_with_its_own_space_is_never_glued_on():
    out = _extract_word_timestamps(_seg([" 60", " 000", " -", " ok"]))
    assert [w["text"] for w in out] == ["60", "000", "-", "ok"]


def test_letters_without_a_space_stay_separate_words():
    """A producer that drops every leading space (or a CJK token) can't glue two words together."""
    out = _extract_word_timestamps(_seg(["hello", "world", "你好", "世界", "'s"]))
    assert [w["text"] for w in out] == ["hello", "world", "你好", "世界's"]
