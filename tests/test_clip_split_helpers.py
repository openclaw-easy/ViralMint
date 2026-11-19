# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The clip_extractor split moved its pure helpers into clip_prompts / clip_windows / clip_caption_timing.
tests/test_clip_extractor_split.py pins the re-exports; this file pins the edges of the moved code that no other
test reached, so a later edit to one of the new modules can't change them silently."""
import json
import math

import pytest

from backend.services import clip_caption_timing as ct
from backend.services import clip_prompts as cp
from backend.services import clip_windows as cw


# ── clip_windows: language-aware counting ───────────────────────────────────

def test_cjk_is_counted_in_characters_without_punctuation():
    assert cw._has_cjk("我们开始吧") and cw._has_cjk("こんにちは") and cw._has_cjk("안녕하세요")
    assert not cw._has_cjk("hello world")
    assert cw._count_speech_units("我们，开始吧！") == 5           # punctuation is not speech
    assert cw._count_speech_units("one two  three") == 3
    assert cw._count_speech_units("") == 0


def test_a_clip_count_without_a_transcript_is_at_most_three():
    assert cw._estimate_realistic_clip_count([], 600, 10) == 3
    assert cw._estimate_realistic_clip_count([], 600, 2) == 2
    assert cw._estimate_realistic_clip_count([{"start": 0, "end": 5, "text": "x"}], 0, 10) == 3


def test_a_cjk_source_is_counted_at_its_own_speech_rate():
    # 300 characters of Chinese over 300 s: at 4.5 chars/s, 15 s clips hold 67 chars → 4 clips of content.
    segs = [{"start": i * 10, "end": i * 10 + 10, "text": "我" * 10} for i in range(30)]
    assert cw._estimate_realistic_clip_count(segs, 300, 10, min_duration=15) == 4


# ── clip_windows: timestamps ────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, None, "", "1:2:3:4"])
def test_a_bad_timestamp_is_a_readable_error(bad):
    with pytest.raises(ValueError):
        cw._parse_timestamp(bad)


def test_good_timestamps():
    assert cw._parse_timestamp(45) == 45.0
    assert cw._parse_timestamp("10:38") == 638.0
    assert cw._parse_timestamp("1:02:15") == 3735.0


# ── clip_windows: silent gaps ───────────────────────────────────────────────

def test_silent_gaps_need_a_length_and_some_speech():
    assert cw._find_silent_gaps([{"start": 0, "end": 5}], [], 0) == []
    # Nothing covered at all (zero-length cues are ignored): the fully silent case is handled upstream.
    assert cw._find_silent_gaps([{"start": 3, "end": 3}], [{"start": 4, "end": 4}], 120) == []


def test_overlapping_speech_and_clips_merge_before_gaps_are_found():
    segs = [{"start": 0, "end": 10}, {"start": 5, "end": 20}]
    clips = [{"start": 18, "end": 30}]
    gaps = cw._find_silent_gaps(segs, clips, 60, min_gap_duration=10)
    assert [(g["start"], g["end"]) for g in gaps] == [(30.0, 60.0)]
    assert gaps[0]["title"] == "Silent section 30s–60s" and gaps[0]["virality_score"] == 3.0


def test_a_long_gap_is_split_and_the_budget_stops_emission():
    segs = [{"start": 0, "end": 10}]
    gaps = cw._find_silent_gaps(segs, [], 190, min_gap_duration=10, max_clip_duration=60)
    assert len(gaps) == 3 and all(g["end"] - g["start"] <= 60 for g in gaps)
    assert len(cw._find_silent_gaps(segs, [], 190, min_gap_duration=10, max_clip_duration=60, budget=2)) == 2


def test_a_split_chunk_shorter_than_the_minimum_is_dropped():
    # A 25 s gap split into 60 s chunks is one chunk; with max 12 s it is three chunks of 8.3 s, each under 10 s.
    gaps = cw._find_silent_gaps([{"start": 0, "end": 5}], [], 30, min_gap_duration=10, max_clip_duration=12)
    assert gaps == []


# ── clip_windows: cached segments ───────────────────────────────────────────

def test_cached_segments():
    assert cw._parse_segments(None, "abcdef12") is None
    assert cw._parse_segments("{corrupt", "abcdef12") is None        # re-transcribe, never crash
    assert cw._parse_segments("[]", "abcdef12") is None              # empty is "none worth using"
    assert cw._parse_segments(json.dumps([{"start": 0}]), "abcdef12") == [{"start": 0}]


# ── clip_windows: snapping ──────────────────────────────────────────────────

def test_snap_leaves_a_window_it_cant_read_and_one_it_cant_improve():
    segs = [{"start": 10.0, "end": 14.0}, {"start": 14.0, "end": 30.0}]
    unreadable = {"start": "soon", "end": 20}
    missing = {"end": 20}
    tiny = {"start": 13.6, "end": 14.1}           # every candidate collapses under 1 s
    out = cw._snap_to_sentence_boundaries([unreadable, missing, tiny], segs)
    assert out == [unreadable, missing, tiny]
    assert out[2] is tiny, "an unimprovable window must come back as itself"


def test_snap_with_no_segment_edges_keeps_the_window():
    w = {"start": 11.0, "end": 29.0}
    assert cw._snap_to_sentence_boundaries([w], [{"text": "no times"}]) == [w]


def test_snap_moves_both_ends_inside_the_slide_and_never_mutates_the_input():
    segs = [{"start": 10.0, "end": 14.0}, {"start": 14.0, "end": 30.0}]
    w = {"start": 11.2, "end": 28.5, "title": "t"}
    out = cw._snap_to_sentence_boundaries([w], segs)
    assert (out[0]["start"], out[0]["end"]) == (10.0, 30.0) and out[0]["title"] == "t"
    assert w == {"start": 11.2, "end": 28.5, "title": "t"}


# ── clip_caption_timing ─────────────────────────────────────────────────────

def test_retime_drops_a_segment_that_was_cut_and_one_whose_words_all_were():
    keep = [(0.0, 2.0), (6.0, 8.0)]
    segs = [
        {"start": 0.5, "end": 1.5, "text": "kept"},
        {"start": 3.0, "end": 4.0, "text": "cut silence"},                # maps to one point: dropped
        {"start": 1.0, "end": 7.0, "text": "spans", "words": [
            {"start": 3.0, "end": 3.5}, {"start": 4.0, "end": 5.0}]},     # every word inside the cut
        {"start": 6.5, "end": 7.5, "text": "after", "words": [{"start": 6.5, "end": 7.0}]},
    ]
    out = ct._retime_segments_after_removal(segs, keep)
    assert [s["text"] for s in out] == ["kept", "after"]
    assert (out[1]["start"], out[1]["end"]) == (2.5, 3.5)
    assert out[1]["words"] == [{"start": 2.5, "end": 3.0}]


def test_filter_and_offset_skips_segments_outside_the_clip():
    segs = [{"start": 0, "end": 5, "text": "before"}, {"start": 10, "end": 12, "text": "in",
            "words": [{"start": 10, "end": 11}, {"start": 30, "end": 31}]}, {"start": 40, "end": 45, "text": "after"}]
    out = ct._filter_and_offset_segments(segs, 8, 20)
    assert [s["text"] for s in out] == ["in"]
    assert (out[0]["start"], out[0]["end"]) == (2, 4) and out[0]["words"] == [{"start": 2, "end": 3}]


# ── clip_prompts ────────────────────────────────────────────────────────────

def test_genre_bias_of_nothing_is_nothing():
    assert cp._build_genre_bias_block(None) == ""
    assert cp._build_genre_bias_block("   ") == ""
    assert cp._build_genre_bias_block("no-such-genre") == ""


def test_segments_text_of_a_short_source_is_every_line_until_the_cap():
    segs = [{"start": float(i), "end": i + 1.0, "text": f"line {i}"} for i in range(5)]
    text = cp._build_segments_text(segs, 60)
    assert text.splitlines()[0] == "[0.0s - 1.0s] line 0" and len(text.splitlines()) == 5
    capped = cp._build_segments_text(segs, 60, max_chars=40)
    assert capped.splitlines()[-1].startswith("... (truncated at ") and "of 60s total" in capped
    assert cp._build_segments_text([], 60) == ""


def test_segments_text_of_a_long_source_samples_beginning_middle_and_end():
    """One running budget over the three samples always ran out inside the FIRST quarter (this branch only runs when
    the text is over twice the budget), so the model never saw a long podcast's middle or end. Each sample gets its
    own third of the budget."""
    segs = [{"start": i * 10.0, "end": i * 10.0 + 10, "text": "word " * 20} for i in range(360)]   # 3600 s
    text = cp._build_segments_text(segs, 3600, max_chars=8000)
    timed = [line for line in text.splitlines() if line.startswith("[")]
    starts = [float(line[1:line.index("s")]) for line in timed]
    assert starts[0] == 0.0
    assert any(1350 <= t < 2250 for t in starts), "the middle quarter never reached the model"
    assert any(t >= 2700 for t in starts), "the last quarter never reached the model"
    assert text.count("... (segments omitted) ...") == 2
    assert len(text) <= 8000


def test_a_long_source_under_twice_the_budget_is_read_straight():
    segs = [{"start": i * 10.0, "end": i * 10.0 + 10, "text": "w"} for i in range(200)]             # 2000 s, short
    text = cp._build_segments_text(segs, 2000, max_chars=12000)
    assert "omitted" not in text and len(text.splitlines()) == 200


def test_metadata_only_keeps_the_four_fields():
    assert cp._metadata_only("not a dict") == {}
    assert cp._metadata_only({"youtube_title": "t", "video_path": "/evil", "caption_status": "ok"}) == {"youtube_title": "t"}


def test_salvage_of_a_cut_off_array():
    assert cp._salvage_truncated_array('[{"a": 1}, {"b": 2}, {"c"') == [{"a": 1}, {"b": 2}]
    assert cp._salvage_truncated_array("no array here") is None
    assert cp._salvage_truncated_array("[{broken") is None


def test_a_sample_that_fits_its_share_is_shown_whole():
    # A long, dense opening and a quiet rest: the middle and the end each fit their third whole.
    segs = [{"start": i * 10.0, "end": i * 10.0 + 10, "text": "word " * 200} for i in range(100)]
    segs += [{"start": 1000.0 + i * 10, "end": 1010.0 + i * 10, "text": "hi"} for i in range(300)]   # to 4000 s
    text = cp._build_segments_text(segs, 4000, max_chars=9000)
    lines = text.splitlines()
    last_q = segs[-100:]
    assert all(f"[{s['start']:.1f}s - {s['end']:.1f}s] hi" in lines for s in last_q), "the end was cut though it fit"


def test_salvage_steps_back_past_a_broken_object():
    assert cp._salvage_truncated_array('[{"a": 1}, {"b": }') == [{"a": 1}]
