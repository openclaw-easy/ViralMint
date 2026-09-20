# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The deferred half of the Clipper audit: leaks, constants, partials, silence.

Each fix follows a pattern that already existed in the codebase rather than
inventing one — temp-then-replace from `extract_thumbnail`, a scaled timeout from
the caption burn and the silence pass, a constraint warning from rule #14.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from backend.core.exceptions import VideoGenerationError
from backend.services import ffmpeg_service as fs


class TestClipTimeoutScalesWithTheClip:
    def test_short_clips_keep_the_old_budget(self):
        """The 15-60s clips the Clipper normally cuts must behave exactly as
        before — the floor is the old constant."""
        assert fs._clip_timeout_for_duration(15) == 600
        assert fs._clip_timeout_for_duration(60) == 600
        assert fs._clip_timeout_for_duration(149) == 600

    def test_a_long_range_gets_proportional_time(self):
        """Manual mode caps the COUNT of ranges and their MINIMUM length, never
        their maximum — six blocks out of a 6-hour recording is a valid submit
        that hit a flat 600s wall on every window."""
        assert fs._clip_timeout_for_duration(600) == 2400
        assert fs._clip_timeout_for_duration(3600) == 7200      # ceilinged

    def test_the_ceiling_is_a_backstop(self):
        assert fs._clip_timeout_for_duration(100_000) == 7200

    def test_an_unknown_duration_falls_back_to_the_floor(self):
        assert fs._clip_timeout_for_duration(0) == 600
        assert fs._clip_timeout_for_duration(None) == 600
        assert fs._clip_timeout_for_duration(-5) == 600


class TestAspectConversionCannotCacheAPartial:
    """The output name is derived and `if output_path.exists(): return` is the
    cache — so a truncated file at that name is served forever. For the export
    bundle it was then persisted as the landscape version of the video."""

    def _src(self, tmp_path: Path) -> Path:
        out = tmp_path / "src.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "testsrc=s=128x64:d=1", str(out)],
            check=True, capture_output=True, timeout=60,
        )
        return out

    async def test_a_timeout_leaves_no_file_at_the_cached_name(self, tmp_path: Path):
        src = tmp_path / "src.mp4"
        src.write_bytes(b"\x00" * 4096)
        out = tmp_path / "out.mp4"

        def _boom(cmd, **kw):
            # Write a partial where the command was told to write, then blow up
            # exactly as a timeout does.
            Path(cmd[-1]).write_bytes(b"\x00" * 64)
            raise subprocess.TimeoutExpired(cmd, 1)

        with patch.object(fs.subprocess, "run", side_effect=_boom), \
             patch.object(fs, "probe_duration", return_value=30.0):
            with pytest.raises(subprocess.TimeoutExpired):
                await fs.convert_aspect_ratio(src, "16:9", "letterbox", out)

        assert not out.exists(), "a timed-out pass left a partial at the cached name"
        assert not list(tmp_path.glob("*.part*")), "the temp file leaked"

    async def test_exit_zero_with_no_bytes_is_a_failure(self, tmp_path: Path):
        src = tmp_path / "src.mp4"
        src.write_bytes(b"\x00" * 4096)
        out = tmp_path / "out.mp4"

        def _empty(cmd, **kw):
            Path(cmd[-1]).write_bytes(b"")
            return MagicMock(returncode=0, stderr="")

        with patch.object(fs.subprocess, "run", side_effect=_empty), \
             patch.object(fs, "probe_duration", return_value=5.0):
            with pytest.raises(VideoGenerationError):
                await fs.convert_aspect_ratio(src, "16:9", "letterbox", out)
        assert not out.exists()

    @pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg not installed")
    async def test_a_real_conversion_still_lands_at_the_target(self, tmp_path: Path):
        src = self._src(tmp_path)
        out = tmp_path / "wide.mp4"
        got = await fs.convert_aspect_ratio(src, "16:9", "letterbox", out)
        assert Path(got) == out and out.stat().st_size > 1000
        assert not list(tmp_path.glob("*.part*")), "the temp file was not renamed away"
        # Playable everywhere: yuv420p, not whatever the source carried.
        pix = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        assert pix == "yuv420p", pix


class TestAnAbortedUploadLeavesNoBytes:
    async def test_a_disconnect_mid_upload_removes_the_partial(self, tmp_path: Path):
        """`await file.read()` raises when the user navigates away, and the DB
        rows are created further down — so nothing referenced the bytes, and
        VIDEOS_DIR has no sweeper that would ever find them."""
        from backend.api import downloaded as dl

        class _Disconnecting:
            filename = "big.mp4"
            def __init__(self):
                self._n = 0
            async def read(self, n=-1):
                self._n += 1
                if self._n == 1:
                    return b"\x00" * 1024
                raise BaseException("client went away")   # noqa: TRY002

        written: list[Path] = []
        real_open = open

        def _tracking_open(p, *a, **kw):
            if str(p).endswith(".mp4"):
                written.append(Path(p))
            return real_open(p, *a, **kw)

        # VIDEOS_DIR is a computed property on Settings, so patch the type.
        from backend.config import settings as app_settings
        with patch.object(type(app_settings), "VIDEOS_DIR",
                          property(lambda self: tmp_path)), \
             patch("builtins.open", _tracking_open):
            with pytest.raises(BaseException):
                await dl.import_local_video(file=_Disconnecting(), title="")

        assert written, "the upload never opened a destination"
        for p in written:
            assert not p.exists(), f"{p} leaked after the disconnect"


class TestADrasticTrimSaysSo:
    async def test_a_mostly_silent_clip_warns(self):
        """Whisper emits no words for music, effects or room tone, and `select`
        has no upper bound on what it discards — so the clip can come back a
        fraction of its length with only a log line to show for it."""
        from backend.services import clip_extractor as ce

        # Two words a long way apart: the silence between them IS the trim,
        # and the clip is 60s long while only ~2s of it carries speech.
        segments = [
            {"start": 0.0, "end": 1.0, "text": "hello",
             "words": [{"word": "hello", "start": 0.0, "end": 1.0}]},
            {"start": 30.0, "end": 31.0, "text": "there",
             "words": [{"word": "there", "start": 30.0, "end": 31.0}]},
        ]

        # `to_thread` is the seam for BOTH the video-stream probe and the
        # duration probe — dispatch on the function so each gets its own answer.
        async def _to_thread(fn, *a, **kw):
            name = getattr(fn, "__name__", "")
            if name == "_has_video_stream":
                return True
            if name == "_run":
                return None          # the ffmpeg pass itself, stubbed out
            return 60.0     # probe_duration: a 60s clip whose speech is 2s

        with patch("backend.services.clip_extractor.asyncio.to_thread", new=_to_thread), \
             patch("backend.core.ws_manager.ws_manager.send_constraint_warning",
                   new=AsyncMock()) as warn, \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.stat", return_value=MagicMock(st_size=50_000)), \
             patch("pathlib.Path.unlink"):
            await ce._remove_silence_and_fillers(
                Path("/tmp/clip.mp4"), segments, warn_user_id="local")

        drastic = [c for c in warn.await_args_list
                   if c.kwargs.get("constraint") == "silence_removal_drastic"]
        assert drastic, "a 60s clip trimmed to ~2s said nothing"
        assert "60s" in drastic[0].kwargs["message"]

    async def test_the_non_clipper_callers_are_unchanged(self):
        """`warn_user_id` defaults to None so the tool-page callers behave
        exactly as before."""
        import inspect
        from backend.services.clip_extractor import _remove_silence_and_fillers
        sig = inspect.signature(_remove_silence_and_fillers)
        assert sig.parameters["warn_user_id"].default is None
