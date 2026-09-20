# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The 2026-09-17 Clipper audit, ported: cancel, stacking, starvation, trust.

Each test here fails against the unfixed tree. The themes:

  - a cancel that did not cancel — the download family was the one runner group
    that never polled `job_cancelled()`, and the final unconditional "success"
    write overwrote the user's "cancelled";
  - "nothing may stack", which was enforced only in the bench's add paths, so a
    drag, a typed timecode or any API client posting `time_ranges` could cut two
    clips over the same seconds;
  - a run authorised for one clip that could ask the model for two;
  - ffprobe fan-out and an ffprobe awaited inside SQLite's write lock;
  - model output reaching a comma-delimited ASS `Style:` line unchecked.
"""
from __future__ import annotations

import asyncio
import math
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_TMP = Path(tempfile.mkdtemp(prefix="vm-clipaudit-"))
os.environ["VIRALMINT_DATA_DIR"] = str(_TMP)

import pytest

from backend.core import task_runner as tr


# ── Cancellation ───────────────────────────────────────────────────────────

class TestDownloadHonoursCancel:
    """yt-dlp runs in a thread we cannot interrupt, so the video in flight
    still lands and is KEPT — deleting the user's file is the one irreversible
    move. Everything after it must not happen."""

    async def test_batch_stops_before_the_next_transfer(self):
        urls = [{"url": f"https://y.test/{i}", "title": f"v{i}"} for i in range(5)]
        got: list[str] = []

        async def _fake_dl(job_id, url, title, user_id, options=None):
            got.append(url)
            return {"id": f"dv{len(got)}", "title": title}

        # Cancelled from the very first poll.
        with patch("backend.agents.job_helper.job_cancelled", new=AsyncMock(return_value=True)), \
             patch.object(tr, "_download_single_video_to_db", new=_fake_dl), \
             patch("backend.agents.job_helper.update_job_status", new=AsyncMock()) as upd, \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()), \
             patch("backend.core.ws_manager.ws_manager.send_progress", new=AsyncMock()):
            await tr.run_batch_download_urls("job1", urls)

        assert got == [], "a cancelled batch must not start another transfer"
        statuses = [c.args[1] for c in upd.await_args_list if len(c.args) > 1]
        assert "cancelled" in statuses
        assert "success" not in statuses

    async def test_a_cancel_during_the_last_transfer_is_still_honoured(self):
        """The loop gate is polled BEFORE each transfer, so a cancel landing
        during the final (or only) video is never seen by it. A one-URL batch is
        the common case."""
        polls = {"n": 0}

        async def _cancelled(job_id):
            # False for the pre-transfer poll, True for the post-loop one.
            polls["n"] += 1
            return polls["n"] > 1

        async def _fake_dl(job_id, url, title, user_id, options=None):
            return {"id": "dv1", "title": title}

        with patch("backend.agents.job_helper.job_cancelled", new=_cancelled), \
             patch.object(tr, "_download_single_video_to_db", new=_fake_dl), \
             patch("backend.agents.job_helper.update_job_status", new=AsyncMock()) as upd, \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()) as ws, \
             patch("backend.core.ws_manager.ws_manager.send_progress", new=AsyncMock()):
            await tr.run_batch_download_urls("job1", [{"url": "https://y.test/1", "title": "v"}])

        statuses = [c.args[1] for c in upd.await_args_list if len(c.args) > 1]
        assert "cancelled" in statuses and "success" not in statuses
        # And no completion signal for something the user stopped.
        assert "job_complete" not in [c.args[0].get("type") for c in ws.await_args_list]
        # The file that DID land is still reported, so it is not lost.
        cancel_call = [c for c in upd.await_args_list if c.args[1:2] == ("cancelled",)][-1]
        assert cancel_call.kwargs["output_data"]["downloaded_ids"] == ["dv1"]

    async def test_a_cancel_skips_the_analysis_pass(self):
        """Whisper over a video the user told us to stop fetching is minutes of
        work nobody asked for."""
        async def _fake_dl(job_id, url, title, user_id, options=None):
            return {"id": "dv1", "title": title}

        analyzer = AsyncMock()
        with patch("backend.agents.job_helper.job_cancelled", new=AsyncMock(return_value=True)), \
             patch.object(tr, "_download_single_video_to_db", new=_fake_dl), \
             patch("backend.agents.job_helper.update_job_status", new=AsyncMock()), \
             patch("backend.agents.analyzer.AnalyzerAgent.run", new=analyzer), \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()), \
             patch("backend.core.ws_manager.ws_manager.send_progress", new=AsyncMock()):
            await tr.run_batch_download_urls("job1", [{"url": "https://y.test/1", "title": "v"}])
        analyzer.assert_not_awaited()


class TestCancelledNeverBecomesSuccess:
    """The backstop for runners that poll too late, or not at all."""

    def _job(self, status):
        job = MagicMock()
        job.status = status
        job.job_type = "batch_download"
        job.progress_pct = 50
        return job

    async def _write(self, job, status):
        from backend.agents import job_helper
        session = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=job)
        session.execute = AsyncMock(return_value=result)
        session.commit = AsyncMock()

        class _CM:
            async def __aenter__(s):
                return session
            async def __aexit__(s, *a):
                return False

        with patch("backend.agents.job_helper.AsyncSessionLocal", return_value=_CM()):
            await job_helper.update_job_status("j" * 8, status)
        return job

    async def test_cancelled_to_success_is_refused(self):
        job = await self._write(self._job("cancelled"), "success")
        assert job.status == "cancelled"

    async def test_cancelled_to_failed_still_lands(self):
        """A job that cancels and then errors must not look clean."""
        job = await self._write(self._job("cancelled"), "failed")
        assert job.status == "failed"

    async def test_failed_to_success_still_lands(self):
        """The zombie sweep depends on exactly this to self-heal a mis-swept job
        whose draining predecessor later finishes."""
        job = await self._write(self._job("failed"), "success")
        assert job.status == "success"


# ── Nothing may stack ──────────────────────────────────────────────────────

class TestManualRangesRejectOverlaps:
    def _validate(self, ranges, duration=600.0):
        from backend.api.downloaded import _validate_manual_time_ranges
        return _validate_manual_time_ranges(ranges, duration)

    def test_identical_ranges_are_refused(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as e:
            self._validate([{"start": 10, "end": 40}] * 3)
        assert e.value.status_code == 400
        assert "overlap" in str(e.value.detail).lower()

    def test_partial_overlap_is_refused(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            self._validate([{"start": 10, "end": 40}, {"start": 30, "end": 60}])

    def test_touching_ranges_are_legitimate(self):
        """Back-to-back cuts are a real thing users do."""
        out = self._validate([{"start": 10, "end": 40}, {"start": 40, "end": 70}])
        assert [(r["start"], r["end"]) for r in out] == [(10, 40), (40, 70)]

    def test_float_noise_is_not_an_overlap(self):
        """The UI rounds bounds; a real overlap is seconds, not milliseconds."""
        out = self._validate([{"start": 10, "end": 40.004}, {"start": 40, "end": 70}])
        assert len(out) == 2

    def test_nan_is_refused_before_it_reaches_ffmpeg(self):
        """Every comparison against NaN is False, so `end <= start` passed it
        straight through as `-ss nan`."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as e:
            self._validate([{"start": float("nan"), "end": 40}])
        assert "finite" in str(e.value.detail)
        with pytest.raises(HTTPException):
            self._validate([{"start": 0, "end": float("inf")}])

    def test_the_returned_shape_carries_no_internal_keys(self):
        out = self._validate([{"start": 1, "end": 5}])
        assert set(out[0]) == {"start", "end"}


class TestOneClipAuthorisedIsOneClipRequested:
    async def test_the_permissive_retry_never_exceeds_max_clips(self):
        """`max(2, max_clips // 2)` asked for TWO at max_clips=1, and nothing
        downstream re-clamps."""
        from backend.services import clip_extractor as ce
        asked: list[int] = []

        async def _fake_select(segments, title, duration, n, user_settings, **kw):
            asked.append(n)
            return []

        with patch.object(ce, "_select_clip_windows", new=_fake_select), \
             patch("backend.core.ws_manager.ws_manager.send_progress", new=AsyncMock()):
            await ce._select_clip_windows_with_retries(
                [{"start": 0, "end": 5, "text": "hi"}], "T", 300, 1, None,
            )
        assert asked, "no selection attempt was made"
        assert max(asked) <= 1, f"asked the model for {max(asked)} clips when 1 was authorised"


# ── Responsiveness ─────────────────────────────────────────────────────────

class TestProbeFanOutIsBounded:
    def test_the_probe_phase_goes_through_the_ffmpeg_semaphore(self):
        """ffprobe is a blocking subprocess on the loop's default thread pool,
        which every ffmpeg call in the backend shares. 50 unbounded probes
        starved the bench's own filmstrip requests."""
        src = Path("backend/services/clip_extractor.py").read_text()
        i = src.index("clip_probes = await asyncio.gather")
        window = src[i:i + 200]
        assert "_ffmpeg_limited(_probe_one(" in window, window


class TestAspectProbeIsOutsideTheWriteLock:
    def test_no_probe_is_awaited_inside_the_save_transaction(self):
        """A 15s ffprobe awaited while the session held SQLite's write lock gave
        every other writer "database is locked" (busy_timeout is 5s)."""
        src = Path("backend/core/task_runner.py").read_text()
        assert "resolved_aspects" in src
        assert "or await _fallback_aspect(clip)" not in src


# ── Trust: model output into a comma-delimited format ──────────────────────

class TestAssStyleFieldsAreSanitised:
    def test_a_font_stack_degrades_to_one_family(self):
        from backend.services.caption_service import _ass_font_name
        assert _ass_font_name("Arial Bold, Helvetica, sans-serif") == "Arial Bold"

    def test_newlines_and_braces_cannot_reach_the_style_line(self):
        from backend.services.caption_service import _ass_font_name
        got = _ass_font_name("Impact\nStyle: Evil,Arial,0")
        assert "\n" not in got and "," not in got
        # Braces are the ASS override-block delimiters; without them the rest
        # is only an unresolvable font name, which libass falls back from.
        assert "{" not in _ass_font_name("{\\b1}Impact")
        assert "}" not in _ass_font_name("Impact{\\an8}")

    def test_empty_falls_back(self):
        from backend.services.caption_service import _ass_font_name
        assert _ass_font_name("") == "Arial Bold"
        assert _ass_font_name(None) == "Arial Bold"

    def test_web_hex_is_converted_not_dropped(self):
        """A model emitting #RRGGBB means a real colour; ASS wants BGR."""
        from backend.services.caption_service import _ass_colour
        assert _ass_colour("#FF8000", "&H00FFFFFF") == "&H000080FF"
        assert _ass_colour("00FF00", "&H00FFFFFF") == "&H0000FF00"

    def test_a_valid_ass_colour_passes_through(self):
        from backend.services.caption_service import _ass_colour
        assert _ass_colour("&H00FFFFFF", "&H00000000") == "&H00FFFFFF"

    def test_garbage_falls_back(self):
        from backend.services.caption_service import _ass_colour
        assert _ass_colour("bright red", "&H00FFFFFF") == "&H00FFFFFF"
        assert _ass_colour("&H00FFFFFF,Arial,0", "&H00000000") == "&H00000000"


# ── Data safety ────────────────────────────────────────────────────────────

class TestCleanupNeedsTheVolume:
    async def test_an_unreachable_storage_root_deletes_nothing(self):
        """A missing FILE is not a missing VOLUME. On a sleeping external drive
        every row looks stale, and this endpoint would have deleted the user's
        whole Clipper library."""
        from backend.api import downloaded as dl
        from backend.config import settings

        with patch.object(type(settings), "STORAGE_ROOT",
                          property(lambda self: Path("/nonexistent-volume-xyz"))):
            out = await dl.cleanup_stale()
        assert out["removed"] == 0
        assert out.get("skipped") == "storage_unavailable"


# ── Provenance ─────────────────────────────────────────────────────────────

class TestClipProvenance:
    def test_manual_windows_say_they_are_manual(self):
        """The downstream default is "ai", so an untagged manual window reported
        every bench cut as an AI pick — to the caller least likely to believe
        it."""
        from backend.services.clip_extractor import _build_manual_clip_windows
        windows = _build_manual_clip_windows([{"start": 0, "end": 10}], 600, "Src")
        assert windows[0]["selection"] == "manual"

    async def test_the_fallback_can_be_refused(self):
        """A client doing its own framing would rather fail than receive time
        slices dressed as curated clips."""
        from backend.services.clip_options import ExtractOptions
        assert ExtractOptions().allow_duration_fallback is True
        assert ExtractOptions(allow_duration_fallback=False).allow_duration_fallback is False

    async def test_the_job_output_carries_the_fallback_count(self):
        from backend.services.clip_options import ExtractOptions
        video = MagicMock(); video.title = "Src"

        session = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=video)
        session.execute = AsyncMock(return_value=result)
        session.add = MagicMock(); session.flush = AsyncMock(); session.commit = AsyncMock()

        class _CM:
            async def __aenter__(s):
                return session
            async def __aexit__(s, *a):
                return False

        clips = [
            {"video_path": "/tmp/a.mp4", "selection": "duration_fallback",
             "start": 0, "end": 5, "aspect_ratio": "9:16"},
            {"video_path": "/tmp/b.mp4", "selection": "duration_fallback",
             "start": 6, "end": 11, "aspect_ratio": "9:16"},
        ]
        with patch("backend.database.AsyncSessionLocal", return_value=_CM()), \
             patch("backend.services.clip_extractor.extract_viral_clips",
                   new=AsyncMock(return_value=clips)), \
             patch("backend.agents.job_helper.update_job_status", new=AsyncMock()) as upd, \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()), \
             patch("backend.core.ws_manager.ws_manager.send_constraint_warning", new=AsyncMock()):
            await tr.run_extract_clips("job1", "dv1", ExtractOptions())

        success = [c for c in upd.await_args_list if c.args[1:2] == ("success",)][-1]
        out = success.kwargs["output_data"]
        assert out["picker_fallback"] is True
        assert out["fallback_count"] == 2
        # And per row, so a caller with 50 clips knows WHICH to distrust.
        rows = [c.args[0] for c in session.add.call_args_list]
        assert [r.clip_selection for r in rows] == ["duration_fallback"] * 2
