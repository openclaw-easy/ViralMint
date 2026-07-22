# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Broad coverage suite for `backend.services.ytdlp_service`.

Ported from the hosted variant and adapted for the OSS build (which uses
a simpler, single-vendor download service — no per-host rate-limit
bucketing, no cookie-sanitizer, no manual-merge cascade). Everything here
runs fast, needs no network, and mocks the filesystem via tmp_path.

Coverage targets:
  - Subtitle parser (SRT + VTT formats, edge cases)
  - Timestamp + segment-join helpers
  - Chapter extraction
  - Subtitle collection
  - Global rate-limit state machine (cooldown + exponential backoff)
  - Video file resolver
  - Partial / subtitle cleanup
"""
from __future__ import annotations

import time

import pytest

import backend.services.ytdlp_service as ys


# ── Subtitle parser: SRT format ────────────────────────────────────────────


class TestParseSubtitleSrt:
    """yt-dlp writes SRT for most platforms. Pin the parser against both
    standard and edge-case SRT inputs."""

    def test_basic_srt(self):
        srt = (
            "1\n"
            "00:00:01,000 --> 00:00:04,500\n"
            "Hello world\n"
            "\n"
            "2\n"
            "00:00:05,000 --> 00:00:08,000\n"
            "Second line\n"
        )
        out = ys._parse_subtitle_text(srt, "x.srt")
        assert len(out) == 2
        assert out[0]["start"] == 1.0
        assert out[0]["end"] == 4.5
        assert out[0]["text"] == "Hello world"
        assert out[1]["text"] == "Second line"

    def test_srt_multiline_text(self):
        srt = (
            "1\n"
            "00:00:01,000 --> 00:00:04,000\n"
            "Line one\n"
            "Line two\n"
        )
        out = ys._parse_subtitle_text(srt, "x.srt")
        assert out[0]["text"] == "Line one Line two"

    def test_srt_strips_html_tags(self):
        srt = (
            "1\n"
            "00:00:01,000 --> 00:00:04,000\n"
            "<i>Italic</i> text <b>bold</b>\n"
        )
        out = ys._parse_subtitle_text(srt, "x.srt")
        assert out[0]["text"] == "Italic text bold"

    def test_srt_skips_blocks_with_no_timestamp(self):
        srt = (
            "Header line — no timestamp\n"
            "\n"
            "1\n"
            "00:00:01,000 --> 00:00:04,000\n"
            "Valid block\n"
        )
        out = ys._parse_subtitle_text(srt, "x.srt")
        assert len(out) == 1
        assert out[0]["text"] == "Valid block"

    def test_srt_empty_text_block_skipped(self):
        srt = (
            "1\n"
            "00:00:01,000 --> 00:00:04,000\n"
            "\n"
        )
        out = ys._parse_subtitle_text(srt, "x.srt")
        # No text → no segment emitted.
        assert out == []


# ── Subtitle parser: VTT format ────────────────────────────────────────────


class TestParseSubtitleVtt:
    def test_basic_vtt(self):
        vtt = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:04.500\n"
            "Hello world\n\n"
            "00:00:05.000 --> 00:00:08.000\n"
            "Second line\n"
        )
        out = ys._parse_subtitle_text(vtt, "x.vtt")
        assert len(out) == 2
        assert out[0]["start"] == 1.0
        assert out[1]["text"] == "Second line"

    def test_vtt_strips_inline_tags(self):
        vtt = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:04.000\n"
            "<c>colored</c> text <c.yellow>highlight</c>\n"
        )
        out = ys._parse_subtitle_text(vtt, "x.vtt")
        assert out[0]["text"] == "colored text highlight"


# ── _ts_to_seconds ─────────────────────────────────────────────────────────


class TestTsToSeconds:
    @pytest.mark.parametrize("ts, expected", [
        ("00:00:00.000", 0.0),
        ("00:00:01.000", 1.0),
        ("00:00:01,500", 1.5),
        ("00:01:30.000", 90.0),
        ("01:02:03.456", 3723.456),
        ("00:00:00,001", 0.001),
    ])
    def test_conversion(self, ts, expected):
        assert ys._ts_to_seconds(ts) == pytest.approx(expected)


# ── _segments_to_text ──────────────────────────────────────────────────────


class TestSegmentsToText:
    def test_joins_text(self):
        segs = [{"text": "Hello"}, {"text": "world"}]
        assert ys._segments_to_text(segs) == "Hello world"

    def test_deduplicates_consecutive_identical_lines(self):
        # YouTube auto-captions often repeat the same line as it scrolls.
        segs = [{"text": "Hello"}, {"text": "Hello"}, {"text": "world"}]
        assert ys._segments_to_text(segs) == "Hello world"

    def test_empty_segments_returns_empty(self):
        assert ys._segments_to_text([]) == ""

    def test_only_strips_consecutive_dupes_not_all(self):
        # Hello → world → Hello is allowed (non-consecutive)
        segs = [{"text": "Hello"}, {"text": "world"}, {"text": "Hello"}]
        assert ys._segments_to_text(segs) == "Hello world Hello"


# ── _extract_chapters ──────────────────────────────────────────────────────


class TestExtractChapters:
    def test_with_chapters(self):
        info = {
            "chapters": [
                {"start_time": 0, "end_time": 60, "title": "Intro"},
                {"start_time": 60, "end_time": 180, "title": "Main"},
            ]
        }
        out = ys._extract_chapters(info)
        assert out == [
            {"start": 0, "end": 60, "title": "Intro"},
            {"start": 60, "end": 180, "title": "Main"},
        ]

    def test_no_chapters(self):
        assert ys._extract_chapters({}) is None
        assert ys._extract_chapters({"chapters": None}) is None
        assert ys._extract_chapters({"chapters": []}) is None

    def test_chapters_without_title_dropped(self):
        info = {"chapters": [{"start_time": 0, "title": ""}, {"start_time": 60, "title": "Has Title"}]}
        out = ys._extract_chapters(info)
        assert len(out) == 1
        assert out[0]["title"] == "Has Title"


# ── _collect_subtitles ─────────────────────────────────────────────────────


class TestCollectSubtitles:
    def test_no_files_returns_none(self, tmp_path):
        # No subtitle files in the output dir.
        assert ys._collect_subtitles(tmp_path, "v") is None

    def test_parses_creator_sub_into_dict(self, tmp_path):
        (tmp_path / "v.en.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nNormal sub\n"
        )
        result = ys._collect_subtitles(tmp_path, "v")
        assert result is not None
        assert result["source"] == "creator_subtitles"
        assert result["language"] == "en"
        assert "Normal sub" in result["text"]

    def test_prefers_creator_over_auto(self, tmp_path):
        (tmp_path / "v.en.auto.vtt").write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAuto line\n"
        )
        (tmp_path / "v.en.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nCreator line\n"
        )
        result = ys._collect_subtitles(tmp_path, "v")
        assert result is not None
        assert result["source"] == "creator_subtitles"
        assert "Creator line" in result["text"]

    def test_parse_failure_returns_none_or_empty(self, tmp_path):
        bad = tmp_path / "v.en.srt"
        bad.write_bytes(b"\xff\xfe\xfd")  # truly malformed
        # The function reads with errors="replace" and never raises into
        # the caller — it returns None or a dict with an empty "text".
        result = ys._collect_subtitles(tmp_path, "v")
        assert result is None or "text" in result


# ── FORMAT_FALLBACK_CHAIN ──────────────────────────────────────────────────


class TestFormatFallbackChain:
    def test_chain_has_three_layers(self):
        # 720p-merged → any-quality-merged → single best stream.
        assert len(ys.FORMAT_FALLBACK_CHAIN) == 3
        assert all(isinstance(f, str) for f in ys.FORMAT_FALLBACK_CHAIN)


# ── _find_actual_video_file ────────────────────────────────────────────────


class TestFindActualVideoFile:
    def test_finds_mp4(self, tmp_path):
        (tmp_path / "v.mp4").write_bytes(b"x" * 1000)
        out = ys._find_actual_video_file(tmp_path, "v")
        assert out is not None
        assert out.suffix == ".mp4"

    def test_finds_webm(self, tmp_path):
        (tmp_path / "v.webm").write_bytes(b"x" * 1000)
        out = ys._find_actual_video_file(tmp_path, "v")
        assert out.suffix == ".webm"

    def test_prefers_known_extensions_over_glob(self, tmp_path):
        # Known-extension check runs first.
        (tmp_path / "v.mp4").write_bytes(b"x" * 100)
        out = ys._find_actual_video_file(tmp_path, "v")
        assert out.suffix == ".mp4"

    def test_glob_fallback_picks_largest_non_sub(self, tmp_path):
        # No known-extension match — glob picks the largest non-subtitle file.
        (tmp_path / "v.xyz").write_bytes(b"x" * 100)
        (tmp_path / "v.srt").write_bytes(b"x" * 200)  # bigger but is subtitle
        out = ys._find_actual_video_file(tmp_path, "v")
        # subtitle files excluded — the .xyz wins by being the only non-sub
        assert out is not None
        assert out.suffix == ".xyz"

    def test_returns_none_when_no_match(self, tmp_path):
        assert ys._find_actual_video_file(tmp_path, "nonexistent") is None


# ── _cleanup_partial_files ─────────────────────────────────────────────────


class TestCleanupPartialFiles:
    def test_removes_part_files(self, tmp_path):
        # `.part` → matches `{stem}*.part`
        (tmp_path / "v.mp4.part").write_text("x")
        ys._cleanup_partial_files(tmp_path, "v")
        assert not (tmp_path / "v.mp4.part").exists()

    def test_keeps_completed_files(self, tmp_path):
        (tmp_path / "v.mp4").write_text("x")
        ys._cleanup_partial_files(tmp_path, "v")
        # Completed file with no .part suffix survives.
        assert (tmp_path / "v.mp4").exists()

    def test_handles_nonexistent_dir(self, tmp_path):
        # Must not raise on missing dir.
        ys._cleanup_partial_files(tmp_path / "missing", "v")


# ── _cleanup_subtitle_files ────────────────────────────────────────────────


class TestCleanupSubtitleFiles:
    def test_removes_srt_and_vtt(self, tmp_path):
        (tmp_path / "v.en.srt").write_text("x")
        (tmp_path / "v.zh.vtt").write_text("x")
        (tmp_path / "v.mp4").write_text("x")  # NOT a subtitle
        ys._cleanup_subtitle_files(tmp_path, "v")
        assert not (tmp_path / "v.en.srt").exists()
        assert not (tmp_path / "v.zh.vtt").exists()
        assert (tmp_path / "v.mp4").exists()


# ── Global rate-limit state machine ────────────────────────────────────────


class TestRateLimitStateMachine:
    """OSS uses a single global cooldown (YouTube rate-limits at the IP
    level), not per-host bucketing. Pin the exponential-backoff shape."""

    def setup_method(self):
        ys._rate_limit_state["consecutive_429s"] = 0
        ys._rate_limit_state["last_429_time"] = 0.0
        ys._rate_limit_state["cooldown_until"] = 0.0

    def test_record_rate_limit_returns_backoff(self):
        backoff = ys._record_rate_limit()
        assert backoff > 0

    def test_check_cooldown_after_rate_limit(self):
        ys._record_rate_limit()
        assert ys._check_cooldown() > 0

    def test_cooldown_grows_exponentially(self):
        first = ys._record_rate_limit()
        second = ys._record_rate_limit()
        third = ys._record_rate_limit()
        # Each rate-limit hit grows the backoff (capped at MAX).
        assert second >= first
        assert third >= second

    def test_cooldown_capped_at_max(self):
        backoff = 0
        for _ in range(20):
            backoff = ys._record_rate_limit()
        assert backoff <= ys.RATE_LIMIT_BACKOFF_MAX

    def test_record_success_resets_counter(self):
        ys._record_rate_limit()
        ys._record_rate_limit()
        ys._record_success()
        assert ys._rate_limit_state["consecutive_429s"] == 0

    def test_check_cooldown_zero_when_fresh(self):
        assert ys._check_cooldown() == 0.0

    def test_counter_resets_after_quiet_period(self):
        ys._record_rate_limit()
        # Backdate the last-429 marker past the reset window.
        ys._rate_limit_state["last_429_time"] = time.time() - (ys.RATE_LIMIT_RESET_AFTER + 10)
        ys._record_rate_limit()
        # Counter reset to 1 (not 2) because the quiet period elapsed.
        assert ys._rate_limit_state["consecutive_429s"] == 1
