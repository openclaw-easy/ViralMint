# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Coverage suite for `backend.agents.downloader.DownloadAgent` (OSS/BYOK).

The OSS download agent orchestrates one batch of scout-result downloads:
  - pre-flight disk-space warning (non-blocking)
  - per-video try/except: RateLimitError aborts the rest of the batch,
    VideoUnavailableError is per-video, any other Exception triggers an
    AI-assisted URL-repair retry
  - writes a DownloadedVideo row + marks the ScoutResult downloaded on success
  - 0/N success ⇒ job marked "failed" + a `job_failed` WS event
  - >=1 success ⇒ job "success" + a `job_complete` WS event

The OSS agent has NO billing (no require_balance / bill) and NO Playwright
Tier-5 fallback / DRM / bot-detection branches that the SaaS variant carries —
those SaaS tests are dropped here (see the final report).

All heavy I/O (yt-dlp, DB, WS, AI URL-fix) is mocked, so no real download
happens and every branch is hermetic.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


def _run(coro):
    return asyncio.run(coro)


def _make_sr(sr_id="sr_a", title="A nice video", platform="youtube",
             url="https://www.youtube.com/watch?v=abc"):
    sr = MagicMock()
    sr.id = sr_id
    sr.video_id = "vid_" + sr_id
    sr.video_url = url
    sr.title = title
    sr.platform = platform
    sr.is_downloaded = False
    return sr


@asynccontextmanager
async def _fake_session(sr_value):
    """Yield a fake async DB session.

    `sr_value` controls what `execute().scalar_one_or_none()` returns:
      - a list  → pop from the front each call (None when exhausted)
      - anything else (MagicMock / None) → returned on every call
    """
    session = MagicMock()

    async def execute(stmt):
        result = MagicMock()
        if isinstance(sr_value, list):
            val = sr_value.pop(0) if sr_value else None
        else:
            val = sr_value
        result.scalar_one_or_none = MagicMock(return_value=val)
        return result

    session.execute = execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    try:
        yield session
    finally:
        pass


@pytest.fixture
def infra():
    """Patch the cross-cutting infrastructure for a test. Yields handles
    to the mocks the tests assert against."""
    with patch("backend.agents.downloader.shutil.disk_usage") as du, \
         patch("backend.agents.downloader.ws_manager.send_progress",
               new=AsyncMock()), \
         patch("backend.agents.downloader.ws_manager.send_constraint_warning",
               new=AsyncMock()) as warn, \
         patch("backend.agents.downloader.ws_manager.send",
               new=AsyncMock()) as send_ws, \
         patch("backend.agents.downloader.update_job_status",
               new=AsyncMock()) as ujs, \
         patch("backend.agents.downloader.jittered_delay", return_value=0), \
         patch("backend.agents.downloader.DownloadedVideo",
               return_value=MagicMock(id="dv_saved", video_path="/tmp/v.mp4")):
        du.return_value = MagicMock(free=10_000_000_000)  # 10 GB free
        yield {"warn": warn, "send": send_ws, "ujs": ujs, "disk": du}


def _final_status_calls(ujs):
    """Return the (status) positional value from every update_job_status call
    that carried a status argument."""
    out = []
    for c in ujs.await_args_list:
        if len(c.args) >= 2:
            out.append(c.args[1])
        elif "status" in c.kwargs:
            out.append(c.kwargs["status"])
    return out


class TestSuccessPath:
    def test_single_success_writes_downloaded_video_and_marks_success(self, infra):
        from backend.agents.downloader import DownloadAgent
        sr = _make_sr("a")
        with patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: _fake_session([sr, sr])), \
             patch("backend.agents.downloader.download_video",
                   new=AsyncMock(return_value={
                       "video_path": "/tmp/v.mp4",
                       "audio_path": "/tmp/v.mp3",
                       "duration": 60,
                       "file_size_mb": 5.0,
                   })):
            downloaded = _run(DownloadAgent().run("job_1", ["a"], user_id="local"))

        assert downloaded == ["dv_saved"]
        assert "success" in _final_status_calls(infra["ujs"])
        # job_complete WS event emitted.
        assert any(
            c.args and isinstance(c.args[0], dict)
            and c.args[0].get("type") == "job_complete"
            for c in infra["send"].await_args_list
        )

    def test_success_marks_scout_result_downloaded(self, infra):
        from backend.agents.downloader import DownloadAgent
        sr = _make_sr("a")
        # Same sr returned for both the load and the save-time re-lookup.
        with patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: _fake_session(sr)), \
             patch("backend.agents.downloader.download_video",
                   new=AsyncMock(return_value={"video_path": "/tmp/v.mp4", "duration": 30})):
            _run(DownloadAgent().run("job_1", ["a"], user_id="local"))
        assert sr.is_downloaded is True

    def test_subtitles_and_metadata_persisted(self, infra):
        """The subtitles/chapters/tags/category branch of the save path runs
        without error and still reaches the success state."""
        from backend.agents.downloader import DownloadAgent
        sr = _make_sr("a")
        dl_result = {
            "video_path": "/tmp/v.mp4",
            "duration": 60,
            "subtitles": {
                "text": "Hello world",
                "language": "en",
                "source": "creator_subtitles",
                "segments": [{"start": 0, "end": 5, "text": "Hello world"}],
            },
            "chapters": [{"start": 0, "end": 60, "title": "Intro"}],
            "tags": ["tag1", "tag2"],
            "category": "Education",
        }
        with patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: _fake_session(sr)), \
             patch("backend.agents.downloader.download_video",
                   new=AsyncMock(return_value=dl_result)):
            downloaded = _run(DownloadAgent().run("job_1", ["a"], user_id="local"))
        assert downloaded == ["dv_saved"]
        assert "success" in _final_status_calls(infra["ujs"])


class TestDiskSpaceWarning:
    def test_low_disk_warns_but_continues(self, infra):
        from backend.agents.downloader import DownloadAgent
        infra["disk"].return_value = MagicMock(free=10 * 1024 * 1024)  # 10 MB
        sr = _make_sr("a")
        with patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: _fake_session(sr)), \
             patch("backend.agents.downloader.download_video",
                   new=AsyncMock(return_value={"video_path": "/tmp/v.mp4", "duration": 30})):
            downloaded = _run(DownloadAgent().run("job_1", ["a"], user_id="local"))
        assert any(
            c.kwargs.get("constraint") == "disk_space"
            for c in infra["warn"].await_args_list
        )
        # Still completed the download despite the warning.
        assert downloaded == ["dv_saved"]

    def test_disk_usage_error_does_not_block(self, infra):
        from backend.agents.downloader import DownloadAgent
        infra["disk"].side_effect = OSError("disk_usage failed")
        # No SR found for the id → skipped, but must not raise.
        with patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: _fake_session(None)):
            _run(DownloadAgent().run("job_1", ["missing"], user_id="local"))
        # Reached a terminal status without crashing.
        assert _final_status_calls(infra["ujs"])


class TestPerVideoBranches:
    def test_scout_result_not_found_is_skipped(self, infra):
        from backend.agents.downloader import DownloadAgent
        with patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: _fake_session(None)), \
             patch("backend.agents.downloader.download_video",
                   new=AsyncMock()) as dl:
            downloaded = _run(DownloadAgent().run("job_1", ["missing"], user_id="local"))
        # Never attempted a download for a missing scout row.
        dl.assert_not_awaited()
        assert downloaded == []
        # 0/N ⇒ job failed.
        assert "failed" in _final_status_calls(infra["ujs"])

    def test_rate_limit_aborts_remaining_videos(self, infra):
        from backend.agents.downloader import DownloadAgent
        from backend.core.exceptions import RateLimitError
        sr = _make_sr("a")
        dl = AsyncMock(side_effect=RateLimitError("HTTP 429"))
        with patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: _fake_session(sr)), \
             patch("backend.agents.downloader.download_video", new=dl):
            downloaded = _run(DownloadAgent().run("job_1", ["a", "b", "c"], user_id="local"))
        # download_video only tried on the FIRST video; the other two are
        # short-circuited by the rate_limited flag.
        assert dl.await_count == 1
        assert downloaded == []
        assert any(
            c.kwargs.get("constraint") == "rate_limit"
            for c in infra["warn"].await_args_list
        )
        assert "failed" in _final_status_calls(infra["ujs"])

    def test_video_unavailable_is_per_video_not_batch(self, infra):
        from backend.agents.downloader import DownloadAgent
        from backend.core.exceptions import VideoUnavailableError
        sr = _make_sr("a")
        with patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: _fake_session(sr)), \
             patch("backend.agents.downloader.download_video",
                   new=AsyncMock(side_effect=[
                       VideoUnavailableError("private"),
                       {"video_path": "/tmp/v.mp4", "duration": 30},
                   ])):
            downloaded = _run(DownloadAgent().run("job_1", ["a", "b"], user_id="local"))
        # First failed (per-video), second succeeded ⇒ batch is a partial success.
        assert downloaded == ["dv_saved"]
        assert "success" in _final_status_calls(infra["ujs"])


class TestAllFailedSummary:
    def test_all_failed_marks_job_failed_and_emits_event(self, infra):
        from backend.agents.downloader import DownloadAgent
        from backend.core.exceptions import DownloadError
        sr = _make_sr("a")
        with patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: _fake_session(sr)), \
             patch("backend.agents.downloader.download_video",
                   new=AsyncMock(side_effect=DownloadError("permanent fail"))), \
             patch("backend.core.ai_retry.ai_fix_url",
                   new=AsyncMock(return_value=None)):
            downloaded = _run(DownloadAgent().run("job_1", ["a", "b"], user_id="local"))
        assert downloaded == []
        assert "failed" in _final_status_calls(infra["ujs"])
        failed = [
            c for c in infra["send"].await_args_list
            if c.args and isinstance(c.args[0], dict)
            and c.args[0].get("type") == "job_failed"
        ]
        assert failed, "no job_failed WS event emitted"

    def test_rate_limited_all_fail_uses_rate_limit_message(self, infra):
        from backend.agents.downloader import DownloadAgent
        from backend.core.exceptions import RateLimitError
        sr = _make_sr("a")
        with patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: _fake_session(sr)), \
             patch("backend.agents.downloader.download_video",
                   new=AsyncMock(side_effect=RateLimitError("HTTP 429"))):
            _run(DownloadAgent().run("job_1", ["a"], user_id="local"))
        failed = [
            c.args[0] for c in infra["send"].await_args_list
            if c.args and isinstance(c.args[0], dict)
            and c.args[0].get("type") == "job_failed"
        ]
        assert failed
        assert "rate-limit" in failed[0].get("error", "").lower()


class TestAIRetryPath:
    """Any non-RateLimit / non-VideoUnavailable failure triggers an
    AI-assisted URL-repair retry before the video is recorded as failed."""

    def test_ai_retry_succeeds_and_records_download(self, infra):
        from backend.agents.downloader import DownloadAgent
        from backend.core.exceptions import DownloadError
        sr = _make_sr("a", url="https://YOUtube.com/watch?v=abc")

        calls = {"n": 0}

        async def fake_download(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise DownloadError("Unsupported URL: typo'd hostname")
            return {"video_path": "/tmp/v.mp4", "duration": 60}

        with patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: _fake_session(sr)), \
             patch("backend.agents.downloader.download_video",
                   new=AsyncMock(side_effect=fake_download)), \
             patch("backend.core.ai_retry.ai_fix_url",
                   new=AsyncMock(return_value="https://www.youtube.com/watch?v=abc")):
            downloaded = _run(DownloadAgent().run("job_1", ["a"], user_id="local"))

        # download_video called twice — original + AI-corrected retry.
        assert calls["n"] == 2
        assert downloaded == ["dv_saved"]
        assert "success" in _final_status_calls(infra["ujs"])

    def test_ai_retry_returns_none_records_failure(self, infra):
        from backend.agents.downloader import DownloadAgent
        from backend.core.exceptions import DownloadError
        sr = _make_sr("a")
        ai_fix = AsyncMock(return_value=None)
        with patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: _fake_session(sr)), \
             patch("backend.agents.downloader.download_video",
                   new=AsyncMock(side_effect=DownloadError("boom"))), \
             patch("backend.core.ai_retry.ai_fix_url", new=ai_fix):
            downloaded = _run(DownloadAgent().run("job_1", ["a"], user_id="local"))
        ai_fix.assert_awaited()  # the repair was attempted
        assert downloaded == []
        assert "failed" in _final_status_calls(infra["ujs"])
