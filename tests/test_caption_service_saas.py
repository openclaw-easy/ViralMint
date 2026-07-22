# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Ported from the SaaS caption_service tests. The emoji / CAPTION_STYLES /
EMOJI_KEYWORDS suites are dropped here (already covered by
tests/test_caption_service.py). Thumbnail-font-loader cases from the SaaS
file are dropped too — OSS thumbnail_service has no `_SCRIPT_FONT_FILES`
and its `_load_bold_font` takes no text argument. What remains: phrase-aware
line grouping / ASS event timing, script-aware font fallback, and
script-alignment detail beyond what test_caption_ports.py covers.
"""
import re

import pytest


class TestPhraseLineGrouping:
    """Caption overhaul: phrase-aware lines + continuous display."""

    def _words(self, spec):
        """spec: list of (text, start, end)."""
        return [{"text": t, "start": s, "end": e} for t, s, e in spec]

    def test_breaks_at_sentence_punctuation(self):
        from backend.services.caption_service import _group_words_into_lines
        words = self._words([
            ("Hello", 0.0, 0.3), ("world.", 0.35, 0.7),
            ("Next", 0.75, 1.0), ("sentence", 1.05, 1.5),
        ])
        lines = _group_words_into_lines(words, max_words=8, max_chars=100)
        assert len(lines) == 2
        assert [w["text"] for w in lines[0]] == ["Hello", "world."]

    def test_breaks_at_long_pause(self):
        from backend.services.caption_service import _group_words_into_lines
        words = self._words([
            ("one", 0.0, 0.3), ("two", 0.35, 0.6),
            ("three", 1.5, 1.8),  # 0.9s gap > pause threshold
        ])
        lines = _group_words_into_lines(words, max_words=8, max_chars=100)
        assert len(lines) == 2
        assert [w["text"] for w in lines[1]] == ["three"]

    def test_respects_max_words(self):
        from backend.services.caption_service import _group_words_into_lines
        words = self._words([(f"w{i}", i * 0.3, i * 0.3 + 0.25) for i in range(10)])
        lines = _group_words_into_lines(words, max_words=4, max_chars=1000)
        assert all(len(line) <= 4 for line in lines)
        assert sum(len(line) for line in lines) == 10

    def test_events_have_no_display_gaps_within_line(self):
        """Consecutive highlight events inside a line must be temporally
        contiguous even when Whisper words have gaps between them."""
        from backend.services.caption_service import (
            _generate_ass_events, CAPTION_STYLES,
        )
        words = self._words([
            ("Making", 0.0, 0.3),
            ("videos", 0.5, 0.9),     # 0.2s gap after "Making"
            ("is", 1.1, 1.2),         # 0.2s gap
            ("easy", 1.4, 1.9),       # 0.2s gap
        ])
        events = _generate_ass_events(words, CAPTION_STYLES["viral"])
        times = re.findall(
            r"Dialogue: 0,(\d+:\d+:\d+\.\d+),(\d+:\d+:\d+\.\d+)", events,
        )
        assert len(times) == 4  # one event per word, same line
        for i in range(len(times) - 1):
            assert times[i][1] == times[i + 1][0], (
                f"gap between event {i} end ({times[i][1]}) and "
                f"event {i+1} start ({times[i+1][0]})"
            )

    def test_line_holds_until_next_line(self):
        """A line's last event must extend to the next line's start when the
        silence between them is short (no blank screen between lines)."""
        from backend.services.caption_service import (
            _generate_ass_events, CAPTION_STYLES,
        )
        words = self._words([
            ("First.", 0.0, 0.5),
            ("Second", 1.0, 1.5),  # 0.5s gap < hold → line 1 holds to 1.0
        ])
        events = _generate_ass_events(words, CAPTION_STYLES["viral"])
        times = re.findall(
            r"Dialogue: 0,(\d+:\d+:\d+\.\d+),(\d+:\d+:\d+\.\d+)", events,
        )
        assert len(times) == 2
        assert times[0][1] == times[1][0] == "0:00:01.00"

    def test_viral_style_produces_longer_lines_than_before(self):
        """Regression guard for the '2-3 words flashing' complaint: a plain
        7-word sentence at speaking pace must land on ONE line with the
        default viral style."""
        from backend.services.caption_service import (
            _group_words_into_lines, _max_chars_for, CAPTION_STYLES,
        )
        style = CAPTION_STYLES["viral"]
        words = self._words([
            ("You", 0.0, 0.2), ("can", 0.25, 0.4), ("make", 0.45, 0.7),
            ("videos", 0.75, 1.1), ("for", 1.15, 1.3), ("just", 1.35, 1.6),
            ("cents", 1.65, 2.0),
        ])
        lines = _group_words_into_lines(
            words, style["words_per_group"], _max_chars_for(style),
        )
        assert len(lines) <= 2
        assert len(lines[0]) >= 6


class TestScriptAwareFontFallback:
    """Non-Latin caption text must never render as tofu boxes."""

    def test_latin_and_covered_scripts_detect_none(self):
        from backend.services.caption_service import detect_non_latin_script
        assert detect_non_latin_script("") is None
        assert detect_non_latin_script("Plain English, café €100!") is None
        # Cyrillic + Greek are covered by Arial — must NOT trigger an override.
        assert detect_non_latin_script("Привет мир αβγ") is None

    def test_script_detection(self):
        from backend.services.caption_service import detect_non_latin_script
        assert detect_non_latin_script("禁书为何被下架") == "han"
        assert detect_non_latin_script("これはテストです") == "kana"  # kana outranks han
        assert detect_non_latin_script("안녕하세요") == "hangul"
        assert detect_non_latin_script("هذا اختبار") == "arabic"
        assert detect_non_latin_script("นี่คือการทดสอบ") == "thai"
        # Mixed Latin + CJK still triggers (the CJK words need glyphs).
        assert detect_non_latin_script("BANNED 禁书 BOOK") == "han"

    def test_resolve_font_keeps_designed_font_for_latin(self):
        from backend.services.caption_service import resolve_caption_font
        assert resolve_caption_font("Arial Bold", "Plain English") == "Arial Bold"

    def test_resolve_font_overrides_per_platform(self, monkeypatch):
        import sys
        from backend.services.caption_service import resolve_caption_font
        monkeypatch.setattr(sys, "platform", "darwin")
        assert resolve_caption_font("Arial Bold", "禁书") == "PingFang SC"
        monkeypatch.setattr(sys, "platform", "win32")
        assert resolve_caption_font("Impact", "禁书") == "Microsoft YaHei"
        monkeypatch.setattr(sys, "platform", "linux")
        assert resolve_caption_font("Arial", "禁书") == "Noto Sans CJK SC"
        # Unknown platforms fall back to the Linux (Noto) map, never crash.
        monkeypatch.setattr(sys, "platform", "freebsd14")
        assert resolve_caption_font("Arial", "안녕") == "Noto Sans CJK KR"

    def test_ass_header_uses_cjk_font_for_chinese_captions(self, tmp_path):
        """End-to-end: generate_captions_ass with Chinese segments must emit a
        Style line whose Fontname has CJK coverage, not the Latin display font."""
        import asyncio
        from backend.services.caption_service import generate_captions_ass

        segments = [{
            "start": 0.0, "end": 2.0, "text": "禁书为何被下架",
            "words": [
                {"word": "禁书", "start": 0.0, "end": 0.6},
                {"word": "为何", "start": 0.6, "end": 1.2},
                {"word": "被下架", "start": 1.2, "end": 2.0},
            ],
        }]
        out = tmp_path / "cjk.ass"
        asyncio.run(generate_captions_ass(
            segments, style="viral", aspect_ratio="16:9", output_path=out,
            emoji_style="none",
        ))
        content = out.read_text(encoding="utf-8")
        assert "Style: Default,Arial Bold," not in content
        assert "禁书" in content

    def test_ass_header_keeps_designed_font_for_english(self, tmp_path):
        import asyncio
        from backend.services.caption_service import generate_captions_ass

        segments = [{
            "start": 0.0, "end": 1.0, "text": "Hello world",
            "words": [
                {"word": "Hello", "start": 0.0, "end": 0.5},
                {"word": "world", "start": 0.5, "end": 1.0},
            ],
        }]
        out = tmp_path / "latin.ass"
        asyncio.run(generate_captions_ass(
            segments, style="viral", aspect_ratio="16:9", output_path=out,
            emoji_style="none",
        ))
        assert "Style: Default,Arial Bold," in out.read_text(encoding="utf-8")


class TestScriptAlignment:
    """Captions must display the KNOWN script, not Whisper's homophone soup."""

    def _segs(self, words):
        return [{
            "start": words[0][1], "end": words[-1][2],
            "text": "".join(w[0] for w in words),
            "words": [{"word": t, "start": s, "end": e} for t, s, e in words],
        }]

    def test_homophones_corrected_timings_preserved(self):
        from backend.services.caption_service import align_script_to_segments
        # Whisper heard the wrong characters (real observed errors).
        segs = self._segs([
            ("这本书叫做", 0.0, 1.2), ("重真", 1.2, 1.8), ("情政的", 1.8, 2.6),
            ("王国君", 2.6, 3.4),
        ])
        script = "这本书叫做崇祯勤政的亡国君"
        out = align_script_to_segments(script, segs)
        words = out[0]["words"]
        assert [w["word"] for w in words] == ["这本书叫做", "崇祯", "勤政的", "亡国君"]
        # Timings untouched — alignment replaces TEXT only.
        assert [(w["start"], w["end"]) for w in words] == [
            (0.0, 1.2), (1.2, 1.8), (1.8, 2.6), (2.6, 3.4),
        ]

    def test_missing_punctuation_attaches_to_previous_word(self):
        from backend.services.caption_service import align_script_to_segments
        segs = self._segs([("这本书被下架了", 0.0, 1.5), ("非常有意思", 1.5, 3.0)])
        script = "这本书被下架了。非常有意思！"
        out = align_script_to_segments(script, segs)
        words = out[0]["words"]
        assert words[0]["word"] == "这本书被下架了。"
        assert words[1]["word"] == "非常有意思！"

    def test_latin_script_is_untouched(self):
        from backend.services.caption_service import align_script_to_segments
        segs = self._segs([("Hello", 0.0, 0.5), ("world", 0.5, 1.0)])
        out = align_script_to_segments("Hello world entirely different", segs)
        assert out is segs  # no-op for Latin-dominant scripts

    def test_unrelated_script_fails_open(self):
        from backend.services.caption_service import align_script_to_segments
        segs = self._segs([("重真情政", 0.0, 1.0)])
        out = align_script_to_segments("完全无关的另一段文字内容和主题都不一样" * 3, segs)
        assert out is segs  # similarity too low → keep ASR text

    def test_empty_inputs(self):
        from backend.services.caption_service import align_script_to_segments
        assert align_script_to_segments("", []) == []
        segs = self._segs([("你好世界今天天气", 0.0, 1.0)])
        assert align_script_to_segments("", segs) is segs

    def test_unworded_segments_pass_through_untouched(self):
        # Whisper sometimes returns a segment WITHOUT word timestamps —
        # alignment must pass it through, not silently drop it (caption gap).
        from backend.services.caption_service import align_script_to_segments
        segs = [
            {"start": 0.0, "end": 3.0, "text": "这本书讲的是重真皇帝情政却亡国的故事",
             "words": [{"word": "这本书讲的是", "start": 0.0, "end": 1.0},
                       {"word": "重真皇帝", "start": 1.0, "end": 1.8},
                       {"word": "情政却亡国的故事", "start": 1.8, "end": 3.0}]},
            {"start": 3.0, "end": 3.6, "text": "结束", "words": []},
        ]
        out = align_script_to_segments("这本书讲的是崇祯皇帝勤政却亡国的故事结束", segs)
        assert len(out) == 2
        assert out[0]["words"][1]["word"].startswith("崇祯")
        assert out[1]["text"] == "结束"  # untouched passthrough
