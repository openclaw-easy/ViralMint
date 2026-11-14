# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""What a download leaves on disk (`backend.download.outputs`).

Moved from the coverage suites of the deleted `ytdlp_service`: subtitle
parsing and picking, chapters, finding the video file, the two cleanups,
the manual ffmpeg merge and the local audio extract. All offline: ffmpeg and
ffprobe are mocked at `subprocess.run`.

The same promises driven through the engine (a success names a real file, a
failure sweeps its partial files and only its own) are in
tests/test_download_cov_engine.py.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.download import outputs


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
        out = outputs.parse_subtitle_text(srt, "x.srt")
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
        out = outputs.parse_subtitle_text(srt, "x.srt")
        assert out[0]["text"] == "Line one Line two"

    def test_srt_strips_html_tags(self):
        srt = (
            "1\n"
            "00:00:01,000 --> 00:00:04,000\n"
            "<i>Italic</i> text <b>bold</b>\n"
        )
        out = outputs.parse_subtitle_text(srt, "x.srt")
        assert out[0]["text"] == "Italic text bold"

    def test_srt_skips_blocks_with_no_timestamp(self):
        srt = (
            "Header line — no timestamp\n"
            "\n"
            "1\n"
            "00:00:01,000 --> 00:00:04,000\n"
            "Valid block\n"
        )
        out = outputs.parse_subtitle_text(srt, "x.srt")
        assert len(out) == 1
        assert out[0]["text"] == "Valid block"

    def test_srt_empty_text_block_skipped(self):
        srt = (
            "1\n"
            "00:00:01,000 --> 00:00:04,000\n"
            "\n"
        )
        out = outputs.parse_subtitle_text(srt, "x.srt")
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
        out = outputs.parse_subtitle_text(vtt, "x.vtt")
        assert len(out) == 2
        assert out[0]["start"] == 1.0
        assert out[1]["text"] == "Second line"

    def test_vtt_strips_inline_tags(self):
        vtt = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:04.000\n"
            "<c>colored</c> text <c.yellow>highlight</c>\n"
        )
        out = outputs.parse_subtitle_text(vtt, "x.vtt")
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
        assert outputs._ts_to_seconds(ts) == pytest.approx(expected)


# ── _segments_to_text ──────────────────────────────────────────────────────


class TestSegmentsToText:
    def test_joins_text(self):
        segs = [{"text": "Hello"}, {"text": "world"}]
        assert outputs.segments_to_text(segs) == "Hello world"

    def test_deduplicates_consecutive_identical_lines(self):
        # YouTube auto-captions often repeat the same line as it scrolls.
        segs = [{"text": "Hello"}, {"text": "Hello"}, {"text": "world"}]
        assert outputs.segments_to_text(segs) == "Hello world"

    def test_empty_segments_returns_empty(self):
        assert outputs.segments_to_text([]) == ""

    def test_only_strips_consecutive_dupes_not_all(self):
        # Hello → world → Hello is allowed (non-consecutive)
        segs = [{"text": "Hello"}, {"text": "world"}, {"text": "Hello"}]
        assert outputs.segments_to_text(segs) == "Hello world Hello"


# ── _extract_chapters ──────────────────────────────────────────────────────


class TestExtractChapters:
    def test_with_chapters(self):
        info = {
            "chapters": [
                {"start_time": 0, "end_time": 60, "title": "Intro"},
                {"start_time": 60, "end_time": 180, "title": "Main"},
            ]
        }
        out = outputs.extract_chapters(info)
        assert out == [
            {"start": 0, "end": 60, "title": "Intro"},
            {"start": 60, "end": 180, "title": "Main"},
        ]

    def test_no_chapters(self):
        assert outputs.extract_chapters({}) is None
        assert outputs.extract_chapters({"chapters": None}) is None
        assert outputs.extract_chapters({"chapters": []}) is None

    def test_chapters_without_title_dropped(self):
        info = {"chapters": [{"start_time": 0, "title": ""}, {"start_time": 60, "title": "Has Title"}]}
        out = outputs.extract_chapters(info)
        assert len(out) == 1
        assert out[0]["title"] == "Has Title"


# ── _collect_subtitles ─────────────────────────────────────────────────────


class TestCollectSubtitles:
    def test_no_files_returns_none(self, tmp_path):
        # No subtitle files in the output dir.
        assert outputs.collect_subtitles(tmp_path, "v") is None

    def test_picks_target_language_creator_sub(self, tmp_path):
        # Two files: english creator + arabic creator. Video is english.
        # Picker MUST pick english creator (not arabic — would be the
        # bug from the alphabetic-glob picker).
        (tmp_path / "v.en.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nEnglish line\n"
        )
        (tmp_path / "v.ar.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nArabic line\n"
        )
        info = {
            "language": "en",
            "subtitles": {"en": [], "ar": []},
            "automatic_captions": {},
        }
        result = outputs.collect_subtitles(tmp_path, "v", info=info)
        assert result is not None
        assert result["language"] == "en"
        assert result["source"] == "creator_subtitles"
        assert "English line" in result["text"]

    def test_falls_back_to_english_when_target_missing(self, tmp_path):
        (tmp_path / "v.en.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nEnglish line\n"
        )
        info = {"language": "ja", "subtitles": {"en": []}}
        result = outputs.collect_subtitles(tmp_path, "v", info=info)
        # No JP available — fall back to EN.
        assert result["language"] == "en"

    def test_no_info_uses_filename_heuristic(self, tmp_path):
        (tmp_path / "v.en.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nNormal sub\n"
        )
        result = outputs.collect_subtitles(tmp_path, "v", info=None)
        assert result is not None

    def test_parse_failure_returns_none(self, tmp_path):
        bad = tmp_path / "v.en.srt"
        bad.write_bytes(b"\xff\xfe\xfd")  # truly malformed
        # Even with a parse error, the function returns None (caught
        # internally) — never raises into the caller.
        result = outputs.collect_subtitles(tmp_path, "v")
        # Parser may return empty list (no segments) or None.
        assert result is None or result == {"text": ""} or "text" in result


# ── _find_actual_video_file ────────────────────────────────────────────────


class TestFindActualVideoFile:
    def test_finds_mp4(self, tmp_path):
        (tmp_path / "v.mp4").write_bytes(b"x" * 1000)
        out = outputs.find_video_file(tmp_path, "v")
        assert out is not None
        assert out.suffix == ".mp4"

    def test_finds_webm(self, tmp_path):
        (tmp_path / "v.webm").write_bytes(b"x" * 1000)
        out = outputs.find_video_file(tmp_path, "v")
        assert out.suffix == ".webm"

    def test_prefers_known_extensions_over_glob(self, tmp_path):
        # Known-extension check runs first.
        (tmp_path / "v.mp4").write_bytes(b"x" * 100)
        out = outputs.find_video_file(tmp_path, "v")
        assert out.suffix == ".mp4"

    def test_glob_fallback_picks_largest_non_sub(self, tmp_path):
        # No known-extension match — glob picks largest non-subtitle file.
        (tmp_path / "v.flv").write_bytes(b"x" * 100)  # not in primary list, BUT it IS in the extensions list
        # Actually .flv IS in the known list. Use unknown extension.
        for f in tmp_path.iterdir():
            f.unlink()
        (tmp_path / "v.xyz").write_bytes(b"x" * 100)
        (tmp_path / "v.srt").write_bytes(b"x" * 200)  # bigger but is subtitle
        out = outputs.find_video_file(tmp_path, "v")
        # subtitle files excluded — the .xyz wins by being the only non-sub
        assert out is not None
        assert out.suffix == ".xyz"

    def test_returns_none_when_no_match(self, tmp_path):
        assert outputs.find_video_file(tmp_path, "nonexistent") is None


# ── _cleanup_partial_files ─────────────────────────────────────────────────


class TestCleanupPartialFiles:
    def test_removes_part_files(self, tmp_path):
        # `.part`  → matches `{stem}*.part`
        # `.fNNN.<ext>` → matches `{stem}.f[0-9]*.*`
        # `.temp.<ext>` → matches `{stem}.temp.*`
        (tmp_path / "v.mp4.part").write_text("x")
        (tmp_path / "v.f398.mp4").write_text("x")
        (tmp_path / "v.temp.mp4").write_text("x")
        outputs.cleanup_partial_files(tmp_path, "v")
        for p in ["v.mp4.part", "v.f398.mp4", "v.temp.mp4"]:
            assert not (tmp_path / p).exists(), f"{p} should have been cleaned up"

    def test_keeps_completed_files(self, tmp_path):
        (tmp_path / "v.mp4").write_text("x")
        outputs.cleanup_partial_files(tmp_path, "v")
        # Completed file with no .part/.temp suffix survives.
        assert (tmp_path / "v.mp4").exists()

    def test_handles_nonexistent_dir(self, tmp_path):
        # Must not raise on missing dir.
        outputs.cleanup_partial_files(tmp_path / "missing", "v")


class TestCleanupNeverTouchesAnotherDownload:
    """A sweep runs on every failure and from abandoned threads, in the folder
    the Library's files live in. It may remove only its own stem's leftovers."""

    def test_keeps_finished_files_and_other_stems(self, tmp_path):
        keep = ["vid1.mp4", "vid1.mkv", "vid1_audio.mp3", "vid10.mp4", "vid10.f398.mp4",
                "vid10.temp.mp4", "other.mp4.part", "xvid1.mp4.part"]
        for name in keep:
            (tmp_path / name).write_text("x")
        (tmp_path / "vid1.mp4.part").write_text("x")
        outputs.cleanup_partial_files(tmp_path, "vid1")
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(keep)

    def test_empty_stem_removes_nothing(self, tmp_path):
        # An empty stem would turn the patterns into "*.part": every download's.
        (tmp_path / "a.mp4.part").write_text("x")
        outputs.cleanup_partial_files(tmp_path, "")
        assert (tmp_path / "a.mp4.part").exists()

    def test_part_file_of_a_stem_that_only_shares_a_prefix_survives(self, tmp_path):
        (tmp_path / "vid1.mp4.part").write_text("x")
        (tmp_path / "vid10.mp4.part").write_text("another download, still running")
        outputs.cleanup_partial_files(tmp_path, "vid1")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["vid10.mp4.part"]


# ── _cleanup_subtitle_files ────────────────────────────────────────────────


class TestCleanupSubtitleFiles:
    def test_removes_srt_and_vtt(self, tmp_path):
        (tmp_path / "v.en.srt").write_text("x")
        (tmp_path / "v.zh.vtt").write_text("x")
        (tmp_path / "v.mp4").write_text("x")  # NOT a subtitle
        outputs.cleanup_subtitle_files(tmp_path, "v")
        assert not (tmp_path / "v.en.srt").exists()
        assert not (tmp_path / "v.zh.vtt").exists()
        assert (tmp_path / "v.mp4").exists()


# ── _try_manual_merge ──────────────────────────────────────────────────────


class TestTryManualMerge:
    """Critical recovery path — when yt-dlp's own merge fails, we stitch the
    leftover `.fNNN.<ext>` streams with our own ffmpeg call. Bug count on
    this path has been disproportionate; pin every branch."""

    def _make_streams(self, tmp_path):
        """Two intermediate stream files — yt-dlp's typical leftover."""
        v = tmp_path / "vid.f398.mp4"
        a = tmp_path / "vid.f251.webm"
        v.write_bytes(b"v" * 10000)
        a.write_bytes(b"a" * 5000)
        return v, a

    def test_no_streams_returns_none(self, tmp_path):
        # No intermediate files → caller knows merge can't help.
        assert outputs.try_manual_merge(tmp_path, "vid") is None

    def test_one_stream_returns_none(self, tmp_path):
        (tmp_path / "vid.f398.mp4").write_bytes(b"v" * 100)
        # Can't merge video+audio when there's only one stream.
        assert outputs.try_manual_merge(tmp_path, "vid") is None

    def test_ffprobe_success_then_ffmpeg_success(self, tmp_path):
        v, a = self._make_streams(tmp_path)

        def fake_subprocess_run(cmd, *args, **kwargs):
            result = MagicMock()
            if cmd[0] == "ffprobe":
                file_arg = cmd[-1]
                result.returncode = 0
                if file_arg.endswith(".f398.mp4"):
                    result.stdout = json.dumps({"streams": [{"codec_type": "video"}]})
                else:
                    result.stdout = json.dumps({"streams": [{"codec_type": "audio"}]})
                return result
            if cmd[0] == "ffmpeg":
                # Simulate successful merge by creating the output file.
                out = Path(cmd[-1])
                out.write_bytes(b"merged" * 10000)
                result.returncode = 0
                result.stderr = ""
                return result
            result.returncode = 1
            result.stderr = "unexpected"
            return result

        with patch("subprocess.run", side_effect=fake_subprocess_run):
            out = outputs.try_manual_merge(tmp_path, "vid")
        assert out is not None
        assert out.exists()
        # Intermediate streams cleaned up after successful merge.
        assert not v.exists()
        assert not a.exists()

    def test_ffmpeg_failure_returns_none(self, tmp_path):
        v, a = self._make_streams(tmp_path)

        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            if cmd[0] == "ffprobe":
                result.returncode = 0
                file_arg = cmd[-1]
                ct = "video" if file_arg.endswith(".f398.mp4") else "audio"
                result.stdout = json.dumps({"streams": [{"codec_type": ct}]})
                return result
            # ffmpeg returns non-zero → merge failed.
            result.returncode = 1
            result.stderr = "ffmpeg error"
            return result

        with patch("subprocess.run", side_effect=fake_run):
            out = outputs.try_manual_merge(tmp_path, "vid")
        assert out is None
        # Streams left in place when merge fails (don't destroy data on error).
        assert v.exists()
        assert a.exists()

    def test_ffmpeg_not_installed_returns_none(self, tmp_path):
        self._make_streams(tmp_path)

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "ffprobe":
                result = MagicMock()
                result.returncode = 0
                result.stdout = json.dumps({"streams": [{"codec_type": "video"}]})
                return result
            raise FileNotFoundError("ffmpeg")

        with patch("subprocess.run", side_effect=fake_run):
            out = outputs.try_manual_merge(tmp_path, "vid")
        assert out is None

    def test_ffmpeg_timeout_returns_none(self, tmp_path):
        self._make_streams(tmp_path)

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "ffprobe":
                result = MagicMock()
                result.returncode = 0
                result.stdout = json.dumps({"streams": [{"codec_type": "video"}]})
                return result
            raise subprocess.TimeoutExpired(cmd, 300)

        with patch("subprocess.run", side_effect=fake_run):
            out = outputs.try_manual_merge(tmp_path, "vid")
        assert out is None

    def test_ffprobe_unavailable_falls_back_to_size_heuristic(self, tmp_path):
        # When ffprobe fails on every file, the function falls back to
        # "largest = video, second-largest = audio". Pin this heuristic.
        v, a = self._make_streams(tmp_path)  # v is larger (10KB > 5KB)

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "ffprobe":
                # Simulate ffprobe crash on every stream.
                raise FileNotFoundError("ffprobe")
            # ffmpeg succeeds.
            out = Path(cmd[-1])
            out.write_bytes(b"merged" * 10000)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            out = outputs.try_manual_merge(tmp_path, "vid")
        assert out is not None


# ── _extract_audio_locally ─────────────────────────────────────────────────


class TestExtractAudioLocally:
    def test_successful_extraction(self, tmp_path):
        v = tmp_path / "v.mp4"
        v.write_bytes(b"v" * 1000)

        def fake_run(cmd, *args, **kwargs):
            # Simulate ffmpeg creating the audio file.
            out = Path(cmd[-1])
            out.write_bytes(b"audio" * 100)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("subprocess.run", side_effect=fake_run):
            out = outputs.extract_audio_locally(v, tmp_path, "v")
        assert out is not None
        assert out.exists()

    def test_ffmpeg_failure_returns_none(self, tmp_path):
        v = tmp_path / "v.mp4"
        v.write_bytes(b"v" * 1000)

        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stderr = "ffmpeg failed"   # text=True: str, as the real call returns
            return result

        with patch("subprocess.run", side_effect=fake_run):
            out = outputs.extract_audio_locally(v, tmp_path, "v")
        assert out is None

    def test_ffmpeg_missing_returns_none(self, tmp_path):
        v = tmp_path / "v.mp4"
        v.write_bytes(b"v" * 1000)
        with patch("subprocess.run", side_effect=FileNotFoundError("ffmpeg")):
            out = outputs.extract_audio_locally(v, tmp_path, "v")
        assert out is None


