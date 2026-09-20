# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A failed caption burn must be visible — to the caller and to the user.

Three seams, one class of bug: a step fails, the helper hands back its input,
and the job reports success. `burn_captions` returns its INPUT on failure,
which is the signal every caller keys on — so it has to actually return the
input when ffmpeg writes nothing, and the extract-clips runner has to say so
instead of leaving the news in a log file.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core import task_runner as tr


class TestBurnCaptionsVerifiesItsOutput:
    async def test_zero_byte_output_returns_the_input(self, tmp_path: Path):
        """ffmpeg can exit 0 and write nothing. Its siblings extract_clip and
        extract_thumbnail both size-check; this one trusted the exit code, so a
        header-only file was reported as captioned."""
        from backend.services import caption_service

        src = tmp_path / "in.mp4"
        src.write_bytes(b"x" * 1024)
        out = tmp_path / "out.mp4"
        ass = tmp_path / "s.ass"
        ass.write_text("[Script Info]\nScriptType: v4.00+\n")

        def _fake_run(cmd, **kw):
            # Exactly what a broken libass build does: success, no bytes.
            Path(cmd[-1]).write_bytes(b"")
            return MagicMock(returncode=0, stderr="")

        with patch.object(caption_service, "_ffmpeg_has_ass", True), \
             patch("subprocess.run", side_effect=_fake_run), \
             patch("backend.services.video_utils.probe_duration", return_value=10.0):
            result = await caption_service.burn_captions(src, ass, out)
        assert Path(result) == src, "a zero-byte burn must report as failure"

    async def test_nonzero_exit_returns_the_input(self, tmp_path: Path):
        from backend.services import caption_service

        src = tmp_path / "in.mp4"
        src.write_bytes(b"x" * 1024)
        ass = tmp_path / "s.ass"
        ass.write_text("[Script Info]\nScriptType: v4.00+\n")

        with patch.object(caption_service, "_ffmpeg_has_ass", True), \
             patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="boom")), \
             patch("backend.services.video_utils.probe_duration", return_value=10.0):
            result = await caption_service.burn_captions(src, ass, tmp_path / "out.mp4")
        assert Path(result) == src


def _fake_session_cm(scalar_return=None):
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_return)
    result.fetchall = MagicMock(return_value=[])
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    class _CM:
        async def __aenter__(self_inner):
            return session
        async def __aexit__(self_inner, *a):
            return False
    return (lambda: _CM()), session


class TestExtractClipsTellsTheUser:
    """The clips exist and play, so the job is a success — but the user asked
    for captions and did not get them on some clips. Rule #14: warn, never
    fail silently."""

    async def _run(self, clips, ws_spy):
        from backend.services.clip_options import ExtractOptions
        video = MagicMock(); video.title = "Src"
        cm, _ = _fake_session_cm(scalar_return=video)
        with patch("backend.database.AsyncSessionLocal", return_value=cm()), \
             patch("backend.services.clip_extractor.extract_viral_clips",
                   new=AsyncMock(return_value=clips)), \
             patch("backend.agents.job_helper.update_job_status", new=AsyncMock()):
            await tr.run_extract_clips("job1", "dv1", ExtractOptions())

    async def test_failed_burn_raises_a_constraint_warning(self):
        clips = [
            {"video_path": "/tmp/a.mp4", "caption_status": "applied", "start": 0, "end": 5},
            {"video_path": "/tmp/b.mp4", "caption_status": "failed", "start": 6, "end": 11},
            {"video_path": "/tmp/c.mp4", "caption_status": "failed", "start": 12, "end": 17},
        ]
        with patch("backend.core.ws_manager.ws_manager.send_constraint_warning",
                   new=AsyncMock()) as warn, \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()):
            await self._run(clips, warn)
        warn.assert_awaited()
        call = warn.await_args_list[0]
        assert call.args[0] == "clip_captions_failed"
        assert "2 of 3" in call.args[1]

    async def test_all_captioned_stays_quiet(self):
        clips = [{"video_path": "/tmp/a.mp4", "caption_status": "applied", "start": 0, "end": 5}]
        with patch("backend.core.ws_manager.ws_manager.send_constraint_warning",
                   new=AsyncMock()) as warn, \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()):
            await self._run(clips, warn)
        assert not [c for c in warn.await_args_list if c.args[0] == "clip_captions_failed"]

    async def test_captions_off_is_not_a_failure(self):
        """"skipped" (caption_style="none") and "no_segments" (a silent clip)
        are outcomes the user chose or the source caused — not failures."""
        clips = [
            {"video_path": "/tmp/a.mp4", "caption_status": "skipped", "start": 0, "end": 5},
            {"video_path": "/tmp/b.mp4", "caption_status": "no_segments", "start": 6, "end": 11},
        ]
        with patch("backend.core.ws_manager.ws_manager.send_constraint_warning",
                   new=AsyncMock()) as warn, \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()):
            await self._run(clips, warn)
        assert not [c for c in warn.await_args_list if c.args[0] == "clip_captions_failed"]
