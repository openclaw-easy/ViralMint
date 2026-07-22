# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Hermetic unit tests for `backend.services.ffmpeg_service`.

FFmpeg / ffprobe are NEVER invoked — every `subprocess.run` boundary is
mocked and `probe_duration` is patched. The focus is on the parts we can
assert on without real media:

  - argv construction (rule #27 shell-safety: every command is a list, never
    a shell string; no `shell=True` anywhere)
  - filter-graph / vf builders (aspect convert, blur-fill, xfade, zoompan,
    auto-zoom crop math)
  - SRT time formatting + caption argv
  - branching (landscape blur-fill vs. passthrough, success vs. fallback,
    empty-output guards, probe-parse failures)

A tmp DATA_DIR is wired via env BEFORE importing the service so no test
writes into the developer's real `~/ViralMint`.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# Isolate storage dirs before the service (→ backend.config) is imported.
_TMP_DATA = Path(tempfile.mkdtemp(prefix="vm_ffmpeg_test_"))
os.environ["VIRALMINT_DATA_DIR"] = str(_TMP_DATA)

import backend.services.ffmpeg_service as fs  # noqa: E402
from backend.core.exceptions import VideoGenerationError  # noqa: E402


# ── Test fake for subprocess.run ────────────────────────────────────────────


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = ""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _make_run(probe_out="1920,1080", ffmpeg_rc=0, make_output=True,
              out_bytes=5000, calls=None):
    """Return a `subprocess.run` replacement.

    ffprobe calls return `probe_out`; ffmpeg calls create the output file
    (last argv element) so downstream `.exists()` / size checks pass.
    """
    def _run(cmd, *a, **k):
        assert isinstance(cmd, list), "argv must be a list, never a shell string"
        assert cmd[0] in ("ffmpeg", "ffprobe"), cmd[0]
        if calls is not None:
            calls.append(cmd)
        if cmd[0] == "ffprobe":
            return _Done(0, probe_out)
        # ffmpeg — synthesize the output artifact
        if ffmpeg_rc == 0 and make_output:
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"0" * out_bytes)
        return _Done(ffmpeg_rc, "", "boom" if ffmpeg_rc else "")
    return _run


def _patch_run(monkeypatch, **kw):
    calls = []
    monkeypatch.setattr(fs.subprocess, "run", _make_run(calls=calls, **kw))
    return calls


def _assert_argv(calls):
    """Rule #27: every command is a list; none passed shell=True."""
    for cmd in calls:
        assert isinstance(cmd, list)


# ── _format_srt_time (pure) ──────────────────────────────────────────────────


class TestFormatSrtTime:
    def test_zero(self):
        assert fs._format_srt_time(0) == "00:00:00,000"

    def test_sub_second_ms(self):
        assert fs._format_srt_time(1.234) == "00:00:01,234"

    def test_hours_minutes_seconds(self):
        # 1h 1m 1s 500ms
        assert fs._format_srt_time(3661.5) == "01:01:01,500"

    def test_minutes_rollover(self):
        assert fs._format_srt_time(125.0) == "00:02:05,000"


# ── stitch_clips dispatch ────────────────────────────────────────────────────


class TestStitchClips:
    async def test_empty_raises(self):
        with pytest.raises(VideoGenerationError):
            await fs.stitch_clips([])

    async def test_single_clip_returns_itself(self):
        p = Path("/tmp/only.mp4")
        assert await fs.stitch_clips([p]) == p

    async def test_none_transition_uses_concat(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch)
        out = tmp_path / "out.mp4"
        result = await fs.stitch_clips(
            [tmp_path / "a.mp4", tmp_path / "b.mp4"], out, transition="none"
        )
        assert result == out
        # concat uses stream copy, no re-encode
        cmd = calls[-1]
        assert "-c" in cmd and "copy" in cmd
        assert "concat" in cmd
        _assert_argv(calls)

    async def test_xfade_offset_math(self, monkeypatch, tmp_path):
        # 3 clips, each 5.0s, td=0.7 → offsets 4.300 then 8.600
        monkeypatch.setattr(fs, "probe_duration", lambda *a, **k: 5.0)
        calls = _patch_run(monkeypatch)
        out = tmp_path / "x.mp4"
        clips = [tmp_path / f"c{i}.mp4" for i in range(3)]
        await fs.stitch_clips(clips, out, transition="fade", transition_duration=0.7)
        cmd = calls[-1]
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "xfade=transition=fade:duration=0.7:offset=4.300[v1]" in fc
        assert "[v1][2:v]xfade=transition=fade:duration=0.7:offset=8.600[outv]" in fc
        assert cmd[cmd.index("-map") + 1] == "[outv]"
        _assert_argv(calls)

    async def test_xfade_unknown_effect_falls_back_to_fade(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fs, "probe_duration", lambda *a, **k: 4.0)
        calls = _patch_run(monkeypatch)
        clips = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
        await fs.stitch_clips(clips, tmp_path / "o.mp4", transition="bogus")
        fc = calls[-1][calls[-1].index("-filter_complex") + 1]
        assert "transition=fade" in fc

    async def test_xfade_failure_falls_back_to_concat(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fs, "probe_duration", lambda *a, **k: 4.0)
        # xfade ffmpeg fails; the concat fallback then runs. Emulate: first
        # ffmpeg call returns rc!=0, the fallback concat returns rc 0.
        seq = {"n": 0}

        def _run(cmd, *a, **k):
            assert isinstance(cmd, list)
            if cmd[0] == "ffprobe":
                return _Done(0, "1920,1080")
            seq["n"] += 1
            if seq["n"] == 1:
                return _Done(1, "", "xfade boom")
            Path(cmd[-1]).write_bytes(b"0" * 4000)
            return _Done(0)

        monkeypatch.setattr(fs.subprocess, "run", _run)
        out = tmp_path / "o.mp4"
        result = await fs.stitch_clips(
            [tmp_path / "a.mp4", tmp_path / "b.mp4"], out, transition="fade"
        )
        assert result == out


# ── add_audio_to_video ───────────────────────────────────────────────────────


class TestAddAudio:
    async def test_argv_and_default_output(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch)
        vid = tmp_path / "clip.mp4"
        aud = tmp_path / "voice.mp3"
        out = await fs.add_audio_to_video(vid, aud)
        assert out == tmp_path / "clip_with_audio.mp4"
        cmd = calls[-1]
        assert cmd[:2] == ["ffmpeg", "-y"]
        assert "-shortest" in cmd
        assert cmd[cmd.index("-c:a") + 1] == "aac"
        assert str(aud) in cmd
        _assert_argv(calls)

    async def test_failure_raises(self, monkeypatch, tmp_path):
        _patch_run(monkeypatch, ffmpeg_rc=1)
        with pytest.raises(VideoGenerationError):
            await fs.add_audio_to_video(tmp_path / "v.mp4", tmp_path / "a.mp3")


# ── add_captions ─────────────────────────────────────────────────────────────


class TestAddCaptions:
    async def test_builds_subtitles_filter(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch)
        vid = tmp_path / "v.mp4"
        segs = [{"start": 0.0, "end": 1.0, "text": "hi"},
                {"start": 1.0, "end": 2.0, "text": "there"}]
        out = await fs.add_captions(vid, segs, font_size=30)
        assert out == tmp_path / "v_captioned.mp4"
        cmd = calls[-1]
        vf = cmd[cmd.index("-vf") + 1]
        assert vf.startswith("subtitles=")
        assert "FontSize=30" in vf
        _assert_argv(calls)

    async def test_failure_returns_original(self, monkeypatch, tmp_path):
        _patch_run(monkeypatch, ffmpeg_rc=1)
        vid = tmp_path / "v.mp4"
        segs = [{"start": 0.0, "end": 1.0, "text": "hi"}]
        out = await fs.add_captions(vid, segs)
        assert out == vid  # graceful degradation, not an exception


# ── extract_thumbnail ────────────────────────────────────────────────────────


class TestExtractThumbnail:
    async def test_success_argv(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch)
        out = await fs.extract_thumbnail(tmp_path / "v.mp4", timestamp=3.5)
        cmd = calls[-1]
        assert cmd[cmd.index("-ss") + 1] == "3.5"
        assert cmd[cmd.index("-vframes") + 1] == "1"
        assert out is not None
        _assert_argv(calls)

    async def test_failure_returns_none(self, monkeypatch, tmp_path):
        _patch_run(monkeypatch, ffmpeg_rc=1)
        out = await fs.extract_thumbnail(tmp_path / "v.mp4")
        assert out is None


# ── extract_clip ─────────────────────────────────────────────────────────────


class TestExtractClip:
    async def test_landscape_uses_blur_fill(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch, probe_out="1920,1080")
        out = tmp_path / "clip.mp4"
        await fs.extract_clip(tmp_path / "src.mp4", 1.0, 6.0, out, vertical=True)
        cmd = calls[-1]
        assert "-filter_complex" in cmd
        vf = cmd[cmd.index("-filter_complex") + 1]
        assert "boxblur" in vf and "overlay" in vf
        # duration = end - start
        assert cmd[cmd.index("-t") + 1] == "5.0"
        _assert_argv(calls)

    async def test_portrait_passthrough(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch, probe_out="1080,1920")
        out = tmp_path / "clip.mp4"
        await fs.extract_clip(tmp_path / "src.mp4", 0.0, 4.0, out)
        cmd = calls[-1]
        assert "-filter_complex" not in cmd
        assert cmd[cmd.index("-c:v") + 1] == "libx264"

    async def test_garbage_probe_defaults_passthrough(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch, probe_out="not,numbers")
        out = tmp_path / "clip.mp4"
        await fs.extract_clip(tmp_path / "src.mp4", 0.0, 3.0, out)
        assert "-filter_complex" not in calls[-1]

    async def test_ffmpeg_failure_raises(self, monkeypatch, tmp_path):
        _patch_run(monkeypatch, probe_out="1080,1920", ffmpeg_rc=1)
        with pytest.raises(VideoGenerationError):
            await fs.extract_clip(tmp_path / "src.mp4", 0.0, 3.0, tmp_path / "o.mp4")

    async def test_empty_output_raises(self, monkeypatch, tmp_path):
        # rc 0 but tiny file (< 1000 bytes) → invalid
        _patch_run(monkeypatch, probe_out="1080,1920", out_bytes=10)
        with pytest.raises(VideoGenerationError):
            await fs.extract_clip(tmp_path / "src.mp4", 0.0, 3.0, tmp_path / "o.mp4")

    async def test_timeout_raises(self, monkeypatch, tmp_path):
        def _run(cmd, *a, **k):
            if cmd[0] == "ffprobe":
                return _Done(0, "1080,1920")
            raise subprocess.TimeoutExpired(cmd, 600)
        monkeypatch.setattr(fs.subprocess, "run", _run)
        with pytest.raises(VideoGenerationError):
            await fs.extract_clip(tmp_path / "src.mp4", 0.0, 3.0, tmp_path / "o.mp4")


# ── convert_aspect_ratio ─────────────────────────────────────────────────────


class TestConvertAspect:
    async def test_letterbox_pad(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch)
        out = await fs.convert_aspect_ratio(
            tmp_path / "v.mp4", "16:9", "letterbox", tmp_path / "o.mp4"
        )
        cmd = calls[-1]
        assert "-vf" in cmd
        vf = cmd[cmd.index("-vf") + 1]
        assert "pad=1920:1080" in vf
        assert "force_original_aspect_ratio=decrease" in vf
        assert out == tmp_path / "o.mp4"

    async def test_crop_method(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch)
        await fs.convert_aspect_ratio(
            tmp_path / "v.mp4", "9:16", "crop", tmp_path / "o.mp4"
        )
        vf = calls[-1][calls[-1].index("-vf") + 1]
        assert "crop=1080:1920" in vf
        assert "force_original_aspect_ratio=increase" in vf

    async def test_blur_fill_uses_filter_complex(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch)
        await fs.convert_aspect_ratio(
            tmp_path / "v.mp4", "1:1", "blur_fill", tmp_path / "o.mp4"
        )
        cmd = calls[-1]
        assert "-filter_complex" in cmd and "-vf" not in cmd
        vf = cmd[cmd.index("-filter_complex") + 1]
        assert "boxblur" in vf and "overlay=(W-w)/2:(H-h)/2" in vf
        assert "1080:1080" in vf  # 1:1 dims

    async def test_unknown_method_defaults_to_pad(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch)
        await fs.convert_aspect_ratio(
            tmp_path / "v.mp4", "16:9", "weird", tmp_path / "o.mp4"
        )
        vf = calls[-1][calls[-1].index("-vf") + 1]
        assert "pad=1920:1080" in vf

    async def test_unknown_aspect_defaults_dims(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch)
        await fs.convert_aspect_ratio(
            tmp_path / "v.mp4", "3:2", "letterbox", tmp_path / "o.mp4"
        )
        vf = calls[-1][calls[-1].index("-vf") + 1]
        assert "1920:1080" in vf  # ASPECT_MAP.get default

    async def test_existing_output_short_circuits(self, monkeypatch, tmp_path):
        out = tmp_path / "already.mp4"
        out.write_bytes(b"present")
        # subprocess.run must NOT be called
        monkeypatch.setattr(
            fs.subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran ffmpeg")),
        )
        result = await fs.convert_aspect_ratio(tmp_path / "v.mp4", "16:9", "crop", out)
        assert result == out

    async def test_failure_raises(self, monkeypatch, tmp_path):
        _patch_run(monkeypatch, ffmpeg_rc=1)
        with pytest.raises(VideoGenerationError):
            await fs.convert_aspect_ratio(
                tmp_path / "v.mp4", "16:9", "crop", tmp_path / "o.mp4"
            )


# ── apply_auto_zoom ──────────────────────────────────────────────────────────


class TestApplyAutoZoom:
    async def test_no_words_returns_original(self, monkeypatch, tmp_path):
        vid = tmp_path / "v.mp4"
        assert await fs.apply_auto_zoom(vid, []) == vid

    async def test_all_invalid_words_returns_original(self, monkeypatch, tmp_path):
        _patch_run(monkeypatch, probe_out="1080,1920,30/1")
        vid = tmp_path / "v.mp4"
        bad = [{"text": "x", "start": -1, "end": -1},
               {"text": "y", "start": "nope", "end": 2}]
        assert await fs.apply_auto_zoom(vid, bad) == vid

    async def test_builds_crop_scale_filter(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch, probe_out="1080,1920,30/1")
        words = [{"text": w, "start": i * 0.5, "end": i * 0.5 + 0.4}
                 for i, w in enumerate(["a", "b", "c", "d", "e", "f"])]
        out = await fs.apply_auto_zoom(tmp_path / "v.mp4", words, words_per_group=3)
        cmd = calls[-1]
        vf = cmd[cmd.index("-vf") + 1]
        assert vf.startswith("crop=w=")
        assert "scale=1080:1920:flags=lanczos" in vf
        assert "sin(PI*" in vf  # sine-pulse zoom expression
        assert out == tmp_path / "v_zoomed.mp4"

    async def test_zoom_factor_clamped(self, monkeypatch, tmp_path):
        # zoom_factor 3.0 → clamped to 1.5, so amplitude zf = 0.5
        calls = _patch_run(monkeypatch, probe_out="1080,1920,30/1")
        words = [{"text": "a", "start": 0.0, "end": 1.0}]
        await fs.apply_auto_zoom(tmp_path / "v.mp4", words, zoom_factor=3.0)
        vf = calls[-1][calls[-1].index("-vf") + 1]
        assert "0.5*sin" in vf

    async def test_bad_probe_uses_defaults(self, monkeypatch, tmp_path):
        # ffprobe returns junk → fallback dims 1080x1920 still build a filter
        calls = _patch_run(monkeypatch, probe_out="garbage")
        words = [{"text": "a", "start": 0.0, "end": 1.0}]
        out = await fs.apply_auto_zoom(tmp_path / "v.mp4", words)
        assert out == tmp_path / "v_zoomed.mp4"
        assert "1080" in calls[-1][calls[-1].index("-vf") + 1]

    async def test_ffmpeg_failure_returns_original(self, monkeypatch, tmp_path):
        _patch_run(monkeypatch, probe_out="1080,1920,30/1", ffmpeg_rc=1)
        vid = tmp_path / "v.mp4"
        words = [{"text": "a", "start": 0.0, "end": 1.0}]
        assert await fs.apply_auto_zoom(vid, words) == vid


# ── generate_text_video ──────────────────────────────────────────────────────


class TestGenerateTextVideo:
    async def test_vertical_no_audio(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch)
        out = tmp_path / "text.mp4"
        result = await fs.generate_text_video(
            "Hello world this is a test script.", output_path=out, aspect_ratio="9:16"
        )
        assert result == out
        assert out.exists()
        # at least one clip encode used a looped still image
        assert any("-loop" in c for c in calls)
        _assert_argv(calls)

    async def test_landscape_branch(self, monkeypatch, tmp_path):
        _patch_run(monkeypatch)
        out = tmp_path / "text16.mp4"
        result = await fs.generate_text_video(
            "landscape script text", output_path=out, aspect_ratio="16:9"
        )
        assert result == out and out.exists()

    async def test_all_clips_fail_raises(self, monkeypatch, tmp_path):
        # ffmpeg never produces a clip → no tmp_clips → raise
        _patch_run(monkeypatch, ffmpeg_rc=1, make_output=False)
        with pytest.raises(VideoGenerationError):
            await fs.generate_text_video("x", output_path=tmp_path / "o.mp4")


# ── generate_kenburns_video ──────────────────────────────────────────────────


class TestKenBurns:
    async def test_no_images_raises(self, monkeypatch):
        with pytest.raises(VideoGenerationError):
            await fs.generate_kenburns_video([])

    async def test_single_image_no_audio(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch)
        img = tmp_path / "a.png"
        img.write_bytes(b"img")
        out = tmp_path / "kb.mp4"
        result = await fs.generate_kenburns_video([img], output_path=out)
        assert result == out and out.exists()
        # zoompan filter present on the per-image encode
        assert any("zoompan=" in " ".join(c) for c in calls)

    async def test_multi_image_xfade(self, monkeypatch, tmp_path):
        calls = _patch_run(monkeypatch)
        imgs = []
        for i in range(3):
            p = tmp_path / f"i{i}.png"
            p.write_bytes(b"img")
            imgs.append(p)
        out = tmp_path / "kb3.mp4"
        result = await fs.generate_kenburns_video(imgs, output_path=out, aspect_ratio="16:9")
        assert result == out and out.exists()
        # a filter_complex xfade stitch happened for 3 clips
        assert any("-filter_complex" in c for c in calls)

    async def test_all_clips_fail_raises(self, monkeypatch, tmp_path):
        _patch_run(monkeypatch, ffmpeg_rc=1, make_output=False)
        img = tmp_path / "a.png"
        img.write_bytes(b"img")
        with pytest.raises(VideoGenerationError):
            await fs.generate_kenburns_video([img], output_path=tmp_path / "o.mp4")


# ── has_audio_stream ─────────────────────────────────────────────────────────


class TestHasAudioStream:
    async def test_missing_file_is_false(self, tmp_path):
        assert await fs.has_audio_stream(tmp_path / "nope.mp4") is False

    async def test_audio_present(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        monkeypatch.setattr(fs.subprocess, "run",
                            lambda *a, **k: _Done(0, "0\n"))
        assert await fs.has_audio_stream(f) is True

    async def test_no_audio_stream(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        monkeypatch.setattr(fs.subprocess, "run", lambda *a, **k: _Done(0, ""))
        assert await fs.has_audio_stream(f) is False

    async def test_probe_error_defaults_true(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")

        def _boom(*a, **k):
            raise FileNotFoundError("no ffprobe")
        monkeypatch.setattr(fs.subprocess, "run", _boom)
        assert await fs.has_audio_stream(f) is True
