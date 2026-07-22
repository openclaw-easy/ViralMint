# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Behavioral tests for backend/core/tool_runners.py (OSS variant).

Two layers:

  1. Pure helpers — silence parsing, chapter/subtitle rendering, merge-target
     resolution, aspect probing, scratch-file cleanup. No ffmpeg / whisper / AI.

  2. Runner orchestration — each `run_tool_*` follows the pattern
     `_tool_progress → work → _tool_success | _tool_fail`. We patch the three
     terminal helpers on the module plus the service layer (whisper / ffmpeg /
     caption / edge-tts / ai) and assert the runner routes to success on the
     happy path, routes to _tool_fail on error, and stays a (billing-free) no-op
     where the OSS design intends. There is NO billing in the OSS runners, so
     unlike the SaaS suite there are no `require_balance` / `bill` assertions.

All I/O is mocked; the suite is fast and hermetic.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core import tool_runners as trun


# ══════════════════════════════════════════════════════════════════════════
# Pure helpers
# ══════════════════════════════════════════════════════════════════════════

class TestParseSilences:
    def test_extracts_start_end_pairs(self):
        stderr = (
            "[silencedetect @ 0x1] silence_start: 1.5\n"
            "[silencedetect @ 0x1] silence_end: 3.0 | silence_duration: 1.5\n"
        )
        assert trun._parse_silences(stderr) == [(1.5, 3.0)]

    def test_multiple_ranges(self):
        stderr = (
            "silence_start: 0.0\nsilence_end: 1.0 | silence_duration: 1.0\n"
            "silence_start: 5.5\nsilence_end: 7.25\n"
        )
        assert trun._parse_silences(stderr) == [(0.0, 1.0), (5.5, 7.25)]

    def test_no_silence_lines_returns_empty(self):
        assert trun._parse_silences("frame= 100 fps=25\nnothing here") == []

    def test_end_without_start_is_ignored(self):
        assert trun._parse_silences("silence_end: 3.0") == []

    def test_malformed_number_is_skipped(self):
        assert trun._parse_silences("silence_start: not_a_num\n") == []


class TestFormatChaptersText:
    def test_mm_ss_format_under_an_hour(self):
        out = trun._format_chapters_text([
            {"start_seconds": 0, "title": "Intro"},
            {"start_seconds": 47, "title": "The Hook"},
            {"start_seconds": 195, "title": "Main story"},
        ])
        assert out == "00:00 Intro\n00:47 The Hook\n03:15 Main story\n"

    def test_h_mm_ss_over_an_hour(self):
        out = trun._format_chapters_text([{"start_seconds": 5025, "title": "Twist"}])
        assert "1:23:45 Twist" in out
        assert "83:45" not in out

    def test_empty_input_returns_empty_string(self):
        assert trun._format_chapters_text([]) == ""

    def test_falsy_title_falls_back_to_chapter(self):
        out = trun._format_chapters_text([{"start_seconds": 0, "title": "  "}])
        assert "Chapter" in out

    def test_negative_seconds_clamped_to_zero(self):
        out = trun._format_chapters_text([{"start_seconds": -5, "title": "Intro"}])
        assert out.startswith("00:00 ")

    def test_float_seconds_floored(self):
        assert trun._format_chapters_text([{"start_seconds": 7.9, "title": "x"}]) == "00:07 x\n"


class TestFormatTs:
    def test_srt_uses_comma(self):
        assert trun._format_ts(65.5) == "00:01:05,500"

    def test_vtt_uses_dot(self):
        assert trun._format_ts(65.5, vtt=True) == "00:01:05.500"

    def test_negative_clamped(self):
        assert trun._format_ts(-3) == "00:00:00,000"


class TestBuildSubtitleFile:
    def test_srt_shape(self, tmp_path):
        out = tmp_path / "o.srt"
        trun._build_subtitle_file([{"start": 0, "end": 1.5, "text": "hi"}], "srt", out)
        txt = out.read_text()
        assert "1\n" in txt
        assert "00:00:00,000 --> 00:00:01,500" in txt
        assert "hi" in txt

    def test_vtt_shape(self, tmp_path):
        out = tmp_path / "o.vtt"
        trun._build_subtitle_file([{"start": 0, "end": 1, "text": "hi"}], "vtt", out)
        txt = out.read_text()
        assert txt.startswith("WEBVTT")
        assert "00:00:00.000 --> 00:00:01.000" in txt

    def test_txt_joins_text(self, tmp_path):
        out = tmp_path / "o.txt"
        trun._build_subtitle_file(
            [{"start": 0, "end": 1, "text": "hi"}, {"start": 1, "end": 2, "text": "there"}],
            "txt", out,
        )
        assert out.read_text().strip() == "hi there"


class TestCleanupPaths:
    def test_deletes_existing_file(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        trun._cleanup_paths(f)
        assert not f.exists()

    def test_ignores_none_and_missing_never_raises(self, tmp_path):
        # Must not raise on None or a nonexistent path.
        trun._cleanup_paths(None, tmp_path / "missing.txt")


class TestResolveMergeTarget:
    def test_known_aspect_is_pure_lookup(self):
        assert trun._resolve_merge_target(Path("/x.mp4"), "16:9") == (1920, 1080, "16:9")
        assert trun._resolve_merge_target(Path("/x.mp4"), "9:16") == (1080, 1920, "9:16")
        assert trun._resolve_merge_target(Path("/x.mp4"), "1:1") == (1080, 1080, "1:1")

    def test_auto_snaps_landscape_to_16_9(self):
        with patch.object(trun, "_probe_dims_sync", return_value=(1920, 1080)):
            w, h, label = trun._resolve_merge_target(Path("/x.mp4"), "auto")
        assert (w, h, label) == (1920, 1080, "16:9")

    def test_auto_snaps_portrait_to_9_16(self):
        with patch.object(trun, "_probe_dims_sync", return_value=(1080, 1920)):
            _, _, label = trun._resolve_merge_target(Path("/x.mp4"), "auto")
        assert label == "9:16"

    def test_auto_probe_error_defaults_to_portrait(self):
        with patch.object(trun, "_probe_dims_sync", side_effect=RuntimeError("no ffprobe")):
            _, _, label = trun._resolve_merge_target(Path("/x.mp4"), "auto")
        assert label == "9:16"


class TestProbeAspectRatioSync:
    def test_landscape(self):
        with patch("subprocess.run", return_value=MagicMock(stdout="1920,1080\n")):
            assert trun._probe_aspect_ratio_sync(Path("/x.mp4")) == "16:9"

    def test_portrait(self):
        with patch("subprocess.run", return_value=MagicMock(stdout="1080,1920\n")):
            assert trun._probe_aspect_ratio_sync(Path("/x.mp4")) == "9:16"

    def test_error_defaults_to_portrait(self):
        with patch("subprocess.run", side_effect=RuntimeError("boom")):
            assert trun._probe_aspect_ratio_sync(Path("/x.mp4")) == "9:16"


# ══════════════════════════════════════════════════════════════════════════
# Runner orchestration
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def terminal_spies():
    """Patch the three terminal/progress helpers so we can assert the outcome
    without touching the DB / WS layer."""
    with patch.object(trun, "_tool_progress", new=AsyncMock()) as prog, \
         patch.object(trun, "_tool_success", new=AsyncMock()) as ok, \
         patch.object(trun, "_tool_fail", new=AsyncMock()) as fail:
        yield {"progress": prog, "success": ok, "fail": fail}


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """Redirect tool_out_path into a tmp dir → `<tmp>/out<ext>`."""
    monkeypatch.setattr(
        "backend.api.tools.tool_out_path",
        lambda job_id, ext=".mp4": tmp_path / f"out{ext}",
    )
    return tmp_path


IN = Path("/nonexistent/in.mp4")


# ── captions ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestCaptions:
    async def test_no_speech_routes_to_fail(self, terminal_spies, out_dir):
        with patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock(return_value={"segments": []})):
            await trun.run_tool_captions("j1", IN, "viral", "auto")
        terminal_spies["fail"].assert_awaited()
        terminal_spies["success"].assert_not_awaited()

    async def test_happy_path_burns_and_succeeds(self, terminal_spies, out_dir):
        async def _burn(inp, ass, out):
            Path(out).write_bytes(b"x")

        with patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock(return_value={"segments": [{"start": 0, "end": 1, "text": "hi"}]})), \
             patch.object(trun, "_probe_aspect_ratio", new=AsyncMock(return_value="9:16")), \
             patch("backend.services.caption_service.generate_captions_ass",
                   new=AsyncMock(return_value=out_dir / "x.ass")), \
             patch("backend.services.caption_service.burn_captions",
                   new=AsyncMock(side_effect=_burn)):
            await trun.run_tool_captions("j1", IN, "viral", "auto")
        terminal_spies["success"].assert_awaited()
        terminal_spies["fail"].assert_not_awaited()


# ── reframe ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestReframe:
    async def test_noop_when_already_vertical(self, terminal_spies, out_dir):
        with patch.object(trun, "_probe_aspect_ratio", new=AsyncMock(return_value="9:16")), \
             patch("backend.core.ws_manager.ws_manager.send_constraint_warning",
                   new=AsyncMock()) as warn, \
             patch("shutil.copy2") as cp:
            await trun.run_tool_reframe("j1", IN)
        cp.assert_called_once()          # returned unchanged
        warn.assert_awaited_once()       # info constraint emitted
        terminal_spies["success"].assert_awaited()

    async def test_converts_when_landscape(self, terminal_spies, out_dir):
        async def _conv(inp, target_aspect, method, output_path):
            Path(output_path).write_bytes(b"x")
            return output_path

        with patch.object(trun, "_probe_aspect_ratio", new=AsyncMock(return_value="16:9")), \
             patch("backend.services.ffmpeg_service.convert_aspect_ratio",
                   new=AsyncMock(side_effect=_conv)) as conv:
            await trun.run_tool_reframe("j1", IN)
        conv.assert_awaited_once()
        terminal_spies["success"].assert_awaited()

    async def test_probe_failure_routes_to_fail(self, terminal_spies, out_dir):
        with patch.object(trun, "_probe_aspect_ratio",
                          new=AsyncMock(side_effect=RuntimeError("probe boom"))):
            await trun.run_tool_reframe("j1", IN)
        terminal_spies["fail"].assert_awaited()


# ── voiceover ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestVoiceover:
    async def test_audio_only_success(self, terminal_spies, out_dir):
        async def _gen(text, voice_id=None, output_path=None):
            Path(output_path).write_bytes(b"x")

        with patch("backend.services.edge_tts_service.generate_voice",
                   new=AsyncMock(side_effect=_gen)) as gen:
            await trun.run_tool_voiceover("j1", "hello world", "en-US-Guy")
        gen.assert_awaited_once()
        terminal_spies["success"].assert_awaited()

    async def test_tts_failure_routes_to_fail(self, terminal_spies, out_dir):
        with patch("backend.services.edge_tts_service.generate_voice",
                   new=AsyncMock(side_effect=RuntimeError("tts down"))):
            await trun.run_tool_voiceover("j1", "hi", "en-US-Guy")
        terminal_spies["fail"].assert_awaited()


# ── transform ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestTransform:
    async def test_flip_h_success(self, terminal_spies, out_dir):
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            await trun.run_tool_transform("j1", IN, "flip_h")
        terminal_spies["success"].assert_awaited()

    async def test_unknown_operation_routes_to_fail(self, terminal_spies, out_dir):
        # Unknown op raises before ffmpeg is ever invoked.
        await trun.run_tool_transform("j1", IN, "not_a_real_op")
        terminal_spies["fail"].assert_awaited()

    async def test_ffmpeg_nonzero_routes_to_fail(self, terminal_spies, out_dir):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="ffmpeg boom")):
            await trun.run_tool_transform("j1", IN, "flip_h")
        terminal_spies["fail"].assert_awaited()


# ── speed ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestSpeed:
    async def test_success(self, terminal_spies, out_dir):
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            await trun.run_tool_speed("j1", IN, speed=2.0)
        terminal_spies["success"].assert_awaited()

    async def test_ffmpeg_error_routes_to_fail(self, terminal_spies, out_dir):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="boom")):
            await trun.run_tool_speed("j1", IN, speed=2.0)
        terminal_spies["fail"].assert_awaited()


# ── audio enhance ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestAudioEnhance:
    async def test_success(self, terminal_spies, out_dir):
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            await trun.run_tool_audio_enhance("j1", IN)
        terminal_spies["success"].assert_awaited()

    async def test_error_routes_to_fail(self, terminal_spies, out_dir):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="boom")):
            await trun.run_tool_audio_enhance("j1", IN)
        terminal_spies["fail"].assert_awaited()


# ── gif ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestGif:
    async def test_success_two_pass(self, terminal_spies, out_dir):
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            await trun.run_tool_gif("j1", IN)
        terminal_spies["success"].assert_awaited()

    async def test_palette_error_routes_to_fail(self, terminal_spies, out_dir):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="palette boom")):
            await trun.run_tool_gif("j1", IN)
        terminal_spies["fail"].assert_awaited()


# ── metadata (AI) ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestMetadata:
    async def test_topic_mode_success(self, terminal_spies, out_dir):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value=(
            '{"youtube_title":"T","youtube_description":"D","tags":[],"tiktok_caption":"c"}'
        ))
        with patch("backend.core.ai_provider.get_ai_client", return_value=ai), \
             patch.object(trun, "_load_user_settings", new=AsyncMock(return_value=None)):
            await trun.run_tool_metadata("j1", topic="cats")
        terminal_spies["success"].assert_awaited()

    async def test_malformed_json_routes_to_fail(self, terminal_spies, out_dir):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="not json at all")
        with patch("backend.core.ai_provider.get_ai_client", return_value=ai), \
             patch.object(trun, "_load_user_settings", new=AsyncMock(return_value=None)):
            await trun.run_tool_metadata("j1", topic="cats")
        terminal_spies["fail"].assert_awaited()


# ── subtitles ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestSubtitles:
    async def test_happy_writes_file(self, terminal_spies, out_dir):
        with patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock(return_value={"segments": [{"start": 0, "end": 1, "text": "hi"}]})):
            await trun.run_tool_subtitles("j1", IN, fmt="srt")
        terminal_spies["success"].assert_awaited()
        assert (out_dir / "out.srt").exists()

    async def test_no_speech_routes_to_fail(self, terminal_spies, out_dir):
        with patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock(return_value={"segments": []})):
            await trun.run_tool_subtitles("j1", IN, fmt="srt")
        terminal_spies["fail"].assert_awaited()


# ── auto chapters (AI) ──────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestAutoChapters:
    async def test_no_speech_routes_to_fail(self, terminal_spies, out_dir):
        with patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock(return_value={"segments": []})):
            await trun.run_tool_auto_chapters("j1", IN)
        terminal_spies["fail"].assert_awaited()

    async def test_happy_writes_chapter_file(self, terminal_spies, out_dir):
        segs = {"segments": [{"start": 0, "end": 40, "text": "hello world"}]}
        ai = MagicMock()
        ai.chat = AsyncMock(return_value='{"chapters":[{"start_seconds":0,"title":"Intro"}]}')
        with patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock(return_value=segs)), \
             patch("backend.core.ai_provider.get_ai_client", return_value=ai), \
             patch.object(trun, "_load_user_settings", new=AsyncMock(return_value=None)):
            await trun.run_tool_auto_chapters("j1", IN)
        terminal_spies["success"].assert_awaited()
        assert (out_dir / "out.txt").exists()


# ── trim ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestTrim:
    async def test_happy_calls_extract_clip_non_vertical(self, terminal_spies, out_dir):
        async def _extract(inp, s, e, out, vertical):
            Path(out).write_bytes(b"x")

        extract = AsyncMock(side_effect=_extract)
        with patch("backend.services.ffmpeg_service.extract_clip", new=extract), \
             patch("backend.services.video_utils.probe_duration", return_value=30.0):
            await trun.run_tool_trim("j1", IN, 2.0, 6.0)
        terminal_spies["success"].assert_awaited()
        # vertical=False preserves the source aspect
        assert extract.call_args.kwargs.get("vertical") is False

    async def test_end_clamped_to_duration(self, terminal_spies, out_dir):
        async def _extract(inp, s, e, out, vertical):
            Path(out).write_bytes(b"x")

        extract = AsyncMock(side_effect=_extract)
        with patch("backend.services.ffmpeg_service.extract_clip", new=extract), \
             patch("backend.services.video_utils.probe_duration", return_value=5.0):
            await trun.run_tool_trim("j1", IN, 0.0, 999.0)
        # end arg clamped to the probed duration
        assert extract.call_args.args[2] == 5.0

    async def test_too_short_after_clamp_routes_to_fail(self, terminal_spies, out_dir):
        with patch("backend.services.ffmpeg_service.extract_clip", new=AsyncMock()), \
             patch("backend.services.video_utils.probe_duration", return_value=2.2):
            await trun.run_tool_trim("j1", IN, 2.0, 999.0)  # 0.2s window
        terminal_spies["fail"].assert_awaited()
        terminal_spies["success"].assert_not_awaited()


# ── auto zoom (no-op aware) ─────────────────────────────────────────────────

@pytest.mark.asyncio
class TestAutoZoom:
    async def test_no_words_is_noop_success(self, terminal_spies, out_dir):
        with patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock(return_value={"segments": []})), \
             patch("backend.core.ws_manager.ws_manager.send_constraint_warning",
                   new=AsyncMock()) as warn, \
             patch("backend.services.ffmpeg_service.apply_auto_zoom", new=AsyncMock()) as zoom, \
             patch("shutil.copy2"):
            await trun.run_tool_auto_zoom("j1", IN)
        warn.assert_awaited_once()
        zoom.assert_not_awaited()          # no words → zoom never applied
        terminal_spies["success"].assert_awaited()

    async def test_words_present_applies_zoom(self, terminal_spies, out_dir):
        segs = {"segments": [{"words": [{"word": "hi", "start": 1.0, "end": 1.2}]}]}

        async def _zoom(inp, words, output_path, zoom_factor, words_per_group):
            Path(output_path).write_bytes(b"x")
            return output_path

        with patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock(return_value=segs)), \
             patch("backend.services.ffmpeg_service.apply_auto_zoom",
                   new=AsyncMock(side_effect=_zoom)) as zoom:
            await trun.run_tool_auto_zoom("j1", IN)
        zoom.assert_awaited_once()
        terminal_spies["success"].assert_awaited()


# ── translate ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestTranslate:
    async def test_no_speech_routes_to_fail(self, terminal_spies, out_dir):
        with patch.object(trun, "_load_user_settings", new=AsyncMock(return_value=None)), \
             patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock(return_value={"segments": []})):
            await trun.run_tool_translate("j1", IN, "Spanish", "captions_only", "", "viral", "auto")
        terminal_spies["fail"].assert_awaited()

    async def test_captions_only_happy(self, terminal_spies, out_dir):
        async def _burn(inp, ass, out):
            Path(out).write_bytes(b"x")

        with patch.object(trun, "_load_user_settings", new=AsyncMock(return_value=None)), \
             patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock(return_value={"segments": [{"start": 0, "end": 1, "text": "hi"}]})), \
             patch.object(trun, "_translate_segments",
                          new=AsyncMock(return_value=[{"start": 0, "end": 1, "text": "hola"}])), \
             patch.object(trun, "_probe_aspect_ratio", new=AsyncMock(return_value="9:16")), \
             patch("backend.services.caption_service.generate_captions_ass",
                   new=AsyncMock(return_value=out_dir / "x.ass")), \
             patch("backend.services.caption_service.burn_captions",
                   new=AsyncMock(side_effect=_burn)):
            await trun.run_tool_translate("j1", IN, "Spanish", "captions_only", "", "viral", "auto")
        terminal_spies["success"].assert_awaited()


# ── hook analysis (writes to job output, not a file) ────────────────────────

@pytest.mark.asyncio
class TestHookAnalysis:
    async def test_no_speech_routes_to_fail(self, terminal_spies, out_dir):
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")), \
             patch.object(trun, "_load_user_settings", new=AsyncMock(return_value=None)), \
             patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock(return_value={"text": ""})):
            await trun.run_tool_hook_analysis("j1", IN)
        terminal_spies["fail"].assert_awaited()

    async def test_happy_updates_job_and_broadcasts(self, terminal_spies, out_dir):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value=(
            '{"score":8,"verdict":"strong","issues":[],"suggested_hooks":[]}'
        ))
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")), \
             patch.object(trun, "_load_user_settings", new=AsyncMock(return_value=None)), \
             patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock(return_value={"text": "hello there viewers"})), \
             patch("backend.core.ai_provider.get_ai_client", return_value=ai), \
             patch("backend.agents.job_helper.update_job_status", new=AsyncMock()) as upd, \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()) as send:
            await trun.run_tool_hook_analysis("j1", IN)
        # success path uses update_job_status + ws.send directly (no _tool_success)
        assert any(c.args[1] == "success" for c in upd.call_args_list if len(c.args) >= 2)
        send.assert_awaited()
        terminal_spies["fail"].assert_not_awaited()
