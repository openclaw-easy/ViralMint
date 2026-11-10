# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The AI URL-fix retry is gated and its output validated.

It used to start a whole second download after ANY error on the URL path —
rate limits, bot checks, private videos — and handed whatever URL the model
wrote straight to yt-dlp, loopback and LAN addresses included.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents.downloader import url_fix_worth_trying
from backend.core.ai_retry import _is_public_web_url, ai_fix_url
from backend.core.exceptions import (
    DownloadError, DownloadStalledError, DownloadTimeoutError, JobCancelledError,
    RateLimitError, VideoUnavailableError,
)
from backend.services.ytdlp_service import _sign_in_message


@pytest.mark.parametrize("err", [
    RateLimitError("429"), VideoUnavailableError("private"), JobCancelledError("x"),
    DownloadStalledError("stalled"), DownloadTimeoutError("timed out"),
    DownloadError(_sign_in_message("https://youtu.be/x")),
    DownloadError(_sign_in_message("https://www.instagram.com/reel/x")),
    DownloadError("Download failed: HTTP Error 429: Too Many Requests"),
    DownloadError("Download failed: <urlopen error timed out>"),
    DownloadError("Download failed: Connection reset by peer"),
    DownloadError("download pool is wedged: all 8 workers are held"),
])
def test_never_worth_a_url_fix(err):
    assert url_fix_worth_trying(err) is False


@pytest.mark.parametrize("err", [
    DownloadError("Download failed: ERROR: [generic] Unable to extract video id"),
    DownloadError("Download failed: ERROR: Unsupported URL: https://youtub.com/x"),
    RuntimeError("Incomplete YouTube ID abc"),
])
def test_url_shaped_errors_still_qualify(err):
    assert url_fix_worth_trying(err) is True


@pytest.mark.parametrize("url,ok", [
    ("https://www.youtube.com/watch?v=x", True),
    ("https://youtu.be/x", True),
    ("http://127.0.0.1:16888/api/settings", False),
    ("http://localhost/x", False),
    ("http://169.254.169.254/latest/meta-data", False),
    ("http://192.168.1.10/v.mp4", False),
    ("http://10.0.0.5/v.mp4", False),
    ("http://[::1]/", False),
    ("http://nas.local/v", False),
    ("http://intranet/v", False),
    ("file:///etc/passwd", False),
    ("ftp://example.com/a", False),
    ("http://[bad", False),
])
def test_public_url_check(url, ok):
    assert _is_public_web_url(url) is ok


def _ai_replying(text):
    ai = MagicMock()
    ai.chat = AsyncMock(return_value=text)
    return patch("backend.core.ai_retry.get_ai_client", return_value=ai)


def test_ai_fix_url_rejects_a_loopback_suggestion():
    with _ai_replying("http://127.0.0.1:16888/api/settings"):
        assert asyncio.run(ai_fix_url("https://youtu.be/x", "bad id")) is None


def test_ai_fix_url_accepts_a_public_rewrite():
    with _ai_replying("https://www.youtube.com/watch?v=x"):
        assert asyncio.run(ai_fix_url("https://youtu.be/x", "bad id")) == "https://www.youtube.com/watch?v=x"


class _NoSettingsDb:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def execute(self, *a):
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=None)
        return r


class TestUrlPathUsesTheGate:
    def _run(self, first_error):
        from backend.core import task_runner
        dl = AsyncMock(side_effect=[first_error, {"video_path": None}])
        fix = AsyncMock(return_value="https://www.youtube.com/watch?v=x")
        with patch("backend.services.ytdlp_service.download_video", new=dl), \
             patch("backend.core.ai_retry.ai_fix_url", new=fix), \
             patch("backend.database.AsyncSessionLocal", _NoSettingsDb):
            try:
                asyncio.run(task_runner._download_single_video_to_db(
                    "job", "https://youtu.be/x", "", "local", options={"quality": "720p"}))
            except Exception as e:
                return dl, fix, e
        return dl, fix, None

    @pytest.mark.parametrize("err", [RateLimitError("429"), VideoUnavailableError("private"),
                                     DownloadError(_sign_in_message("https://youtu.be/x"))])
    def test_a_verdict_is_not_retried_with_ai(self, err):
        dl, fix, raised = self._run(err)
        assert raised is err
        assert fix.await_count == 0 and dl.await_count == 1

    def test_url_error_retry_keeps_the_options(self):
        dl, fix, _ = self._run(DownloadError("Download failed: Incomplete YouTube ID"))
        assert fix.await_count == 1 and dl.await_count == 2
        assert dl.await_args_list[1].kwargs["options"] == {"quality": "720p"}
