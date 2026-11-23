# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""What Whisper heard -> scenes of the user's own audio ("My audio"). Pure."""
import pytest

from backend.services import own_audio as oa
from backend.services import studio_doc as sd

LIB = "au_0123456789abcdef"
S1 = "Cats sleep for most of the whole long day."      # 42 chars: two together pack over 60 units
S2 = "Then they wake up and eat their dinner fast."


def transcript(words):
    return {"segments": [{"words": [{"word": w, "start": s, "end": e} for w, s, e in words]}]}


def spoken(text, step=0.3, gap=0.0, t0=0.0):
    out, t = [], t0
    for w in text.split():
        out.append((f" {w}", round(t, 3), round(t + step * 0.8, 3)))
        t += step + (gap if w.endswith(".") else 0.0)
    return out


def two(gap=1.0):
    first = spoken(S1, gap=gap)
    t0 = first[-1][1] + 0.3 + gap
    return first + spoken(S2, t0=t0)


def test_two_sentences_are_two_scenes_cut_at_their_words():
    words = two()
    lines = oa.cut_lines(transcript(words), 12.0, LIB)
    assert [l["text"] for l in lines] == [S1, S2]
    first, second = lines
    assert first["start"] == 0.0 and first["end"] == pytest.approx(words[len(S1.split()) - 1][2] + oa.TAIL_S)
    s2_start = words[len(S1.split())][1]
    assert second["start"] == pytest.approx(s2_start - oa.LEAD_S)
    assert second["words"][0] == ["Then", pytest.approx(oa.LEAD_S), pytest.approx(oa.LEAD_S + 0.24)]
    assert first["key"] == oa.take_key(LIB, first["text"], first["start"], first["end"])


def test_a_repeated_sentence_cuts_two_takes():
    a = spoken(S1, gap=0.6)
    b = spoken(S1, t0=a[-1][1] + 0.3 + 0.6)
    lines = oa.cut_lines(transcript(a + b), 12.0, LIB)
    assert [l["text"] for l in lines] == [S1, S1]
    assert lines[0]["key"] != lines[1]["key"]


def test_bounds_never_overlap_and_never_pass_the_next_word():
    # no pause between the sentences: the tail and lead would overlap if not clamped
    a = spoken(S1)
    b = spoken(S2, t0=a[-1][2] + 0.01)
    lines = oa.cut_lines(transcript(a + b), 12.0, LIB)
    assert len(lines) == 2
    x, y = lines
    assert x["end"] <= b[0][1] + 1e-9 and y["start"] >= x["end"]


def test_a_transcript_with_no_full_stop_still_fits_every_scene():
    words = [(f" word{i}", 0.3 * i, 0.3 * i + 0.25) for i in range(500)]
    lines = oa.cut_lines(transcript(words), 151.0, LIB)
    assert all(len(l["text"]) <= sd.LINE_MAX for l in lines) and len(lines) <= sd.SCENE_MAX
    heard = [w[0] for l in lines for w in l["words"]]
    assert heard == [f"word{i}" for i in range(500)]


def test_a_cjk_monologue_joins_without_spaces_and_keeps_every_character():
    text = "猫整天都在睡觉。" * 120
    words = [(ch, 0.25 * i, 0.25 * i + 0.2) for i, ch in enumerate(text)]
    lines = oa.cut_lines(transcript(words), 240.5, LIB)
    assert all(" " not in l["text"] and len(l["text"]) <= sd.LINE_MAX for l in lines)
    assert "".join(l["text"] for l in lines) == text


def test_no_words_is_no_speech():
    with pytest.raises(oa.AudioRefused) as e:
        oa.cut_lines({"segments": [{"words": []}]}, 10.0, LIB)
    assert e.value.code == "no_speech"


def test_too_many_cuts_merge_rather_than_drop():
    cuts = [[oa.Word(f"s{i}.", i, i + 0.5)] for i in range(45)]
    merged = oa.merge_to(cuts, sd.SCENE_MAX)
    assert len(merged) == sd.SCENE_MAX and [w.text for c in merged for w in c] == [f"s{i}." for i in range(45)]


def test_korean_keeps_the_spaces_whisper_heard():
    words = [(" 안녕하세요", 0.0, 0.5), (" 여러분", 0.6, 1.0), (" 반갑습니다.", 1.1, 1.8)]
    lines = oa.cut_lines(transcript(words), 6.0, LIB)
    assert [l["text"] for l in lines] == ["안녕하세요 여러분 반갑습니다."]


def test_a_long_pause_splits_the_scene_within_the_door_limit():
    lines = oa.cut_lines(transcript([(" Hi.", 0.0, 0.4), (" Bye.", 130.0, 130.4)]), 140.0, LIB)
    assert [l["text"] for l in lines] == ["Hi.", "Bye."]
    assert all(l["duration"] <= sd.AUDIO_SCENE_MAX_S for l in lines)


def test_a_long_pause_inside_many_words_splits_at_the_biggest_gap_and_keeps_every_word():
    words = [(f" w{i}", 0.2 * i, 0.2 * i + 0.1) for i in range(20)]
    words += [(f" x{i}", 200.0 + 0.2 * i, 200.0 + 0.2 * i + 0.1) for i in range(20)]
    lines = oa.cut_lines(transcript(words), 240.0, LIB)
    assert all(l["duration"] <= sd.AUDIO_SCENE_MAX_S for l in lines) and len(lines) <= sd.SCENE_MAX
    assert [w[0] for l in lines for w in l["words"]] == [w[0].strip() for w in words]
