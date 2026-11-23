# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""own_audio.join_words: how Whisper's word tokens become a scene's caption line.

Found in a browser test of My audio: "sixty thousand" spoken came back as
the tokens " 60" and ",000" and the caption read "up to 60 ,000 workers." Whisper marks a word break with a
leading space; a token WITHOUT one that starts with punctuation (or continues a number) belongs to the word
before it. Nothing else changes: a token without a leading space that starts with a letter still gets its
space (a producer that drops leading spaces must never glue two real words), CJK still joins without one,
and a Korean word break is still kept.
"""
from backend.services.own_audio import Word, join_words


def w(text: str, lead: bool = True) -> Word:
    return Word(text, 0.0, 0.1, lead_space=lead)


def test_a_thousands_separator_stays_inside_the_number():
    words = [w("up"), w("to"), w("60"), w(",000", lead=False), w("workers.")]
    assert join_words(words) == "up to 60,000 workers."


def test_a_percent_sign_and_a_decimal_stay_with_their_number():
    assert join_words([w("about"), w("50", ), w("%", lead=False), w("of"), w("them.")]) == "about 50% of them."
    assert join_words([w("it"), w("costs"), w("3"), w(".5", lead=False), w("dollars.")]) == "it costs 3.5 dollars."


def test_digits_continuing_digits_join():
    assert join_words([w("in"), w("20"), w("26", lead=False), w("we")]) == "in 2026 we"


def test_a_trailing_punctuation_token_joins_the_word_before_it():
    assert join_words([w("Really"), w("?", lead=False), w("Yes"), w("!", lead=False)]) == "Really? Yes!"


def test_an_apostrophe_suffix_joins():
    assert join_words([w("the"), w("bee"), w("'s", lead=False), w("job.")]) == "the bee's job."


def test_ordinary_words_keep_their_spaces():
    assert join_words([w("Honeybees"), w("live"), w("in"), w("colonies.")]) == "Honeybees live in colonies."


def test_a_letter_token_without_a_leading_space_is_never_glued_on():
    # Defensive: a transcript whose producer dropped the leading spaces must still read as words.
    assert join_words([w("Hi", lead=False), w("there", lead=False)]) == "Hi there"


def test_cjk_joins_without_a_space_and_a_korean_break_is_kept():
    assert join_words([w("蜜蜂", lead=False), w("很", lead=False), w("忙。", lead=False)]) == "蜜蜂很忙。"
    assert join_words([w("꿀벌은"), w("바쁘다.")]) == "꿀벌은 바쁘다."


def test_a_hyphenated_word_joins():
    assert join_words([w("a"), w("well"), w("-known", lead=False), w("fact.")]) == "a well-known fact."


# ── the words a caption shows one by one (cut_lines' `words`) ──────────────────
# The line was fixed above, but word-by-word captions are drawn from each take's `words`, one
# entry per token: the prod render of 2026-11-20 still showed "TO 60 ,000 WORKERS" and "$9 .99 AT".
from backend.services.own_audio import caption_words, cut_lines  # noqa: E402


def tw(text: str, start: float, end: float, lead: bool = True) -> Word:
    return Word(text, start, end, lead_space=lead)


def test_caption_words_merge_a_continuation_into_its_word_and_keep_its_timing():
    got = caption_words([tw("up", 0.0, 0.2), tw("60", 0.3, 0.5), tw(",000", 0.5, 0.9, lead=False), tw("workers.", 1.0, 1.4)])
    assert [(w.text, w.start, w.end) for w in got] == [("up", 0.0, 0.2), ("60,000", 0.3, 0.9), ("workers.", 1.0, 1.4)]


def test_caption_words_leave_letters_and_cjk_alone():
    letters = [tw("Hi", 0, 0.1, lead=False), tw("there", 0.1, 0.2, lead=False)]
    assert [w.text for w in caption_words(letters)] == ["Hi", "there"]
    cjk = [tw("蜜蜂", 0, 0.1, lead=False), tw("很", 0.1, 0.2, lead=False), tw("忙。", 0.2, 0.3, lead=False)]
    assert [w.text for w in caption_words(cjk)] == ["蜜蜂", "很", "忙。"]


def test_cut_lines_words_read_like_the_line():
    seg = {"words": [{"word": " It", "start": 0.0, "end": 0.2}, {"word": " costs", "start": 0.2, "end": 0.5},
                     {"word": " $9", "start": 0.5, "end": 0.8}, {"word": ".99", "start": 0.8, "end": 1.1},
                     {"word": " for", "start": 1.2, "end": 1.3}, {"word": " 60", "start": 1.3, "end": 1.5},
                     {"word": ",000", "start": 1.5, "end": 1.9}, {"word": " bees.", "start": 2.0, "end": 2.4}]}
    (line,) = cut_lines({"language": "en", "segments": [seg]}, 3.0, "lib")
    assert line["text"] == "It costs $9.99 for 60,000 bees."
    assert " ".join(w[0] for w in line["words"]) == line["text"]
