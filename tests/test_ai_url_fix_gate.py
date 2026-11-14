# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The AI URL-fix retry is gated and its output validated.

It used to start a whole second download after ANY error on the URL path —
rate limits, bot checks, private videos — and handed whatever URL the model
wrote straight to yt-dlp, loopback and LAN addresses included. The gate is
now the error's class (`fetch._LINK_MAY_BE_WRONG`), read once by `classify`.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ai_retry import _is_public_web_url, ai_fix_url
from backend.core.exceptions import DownloadError, DownloadStalledError, DownloadTimeoutError
from backend.download import engine, fetch
from backend.download.classify import ErrorClass as E


def _req():
    return engine.Request(url="https://youtu.be/x", output_dir=None, stem="s", job_id=None)


def _err(cls, text="Download failed: x", exc=DownloadError):
    e = exc(text)
    if cls is not None:
        e.error_class = cls
    return e


@pytest.fixture
def fixer(monkeypatch):
    fix = AsyncMock(return_value="https://www.youtube.com/watch?v=x")
    monkeypatch.setattr("backend.core.ai_retry.ai_fix_url", fix)
    monkeypatch.setattr("backend.database.AsyncSessionLocal", _NoSettingsDb)
    return fix


@pytest.mark.parametrize("err", [
    _err(E.RATE_LIMITED), _err(E.UNAVAILABLE), _err(E.SIGN_IN_REQUIRED), _err(E.AGE_GATE),
    _err(E.TRANSIENT_NETWORK), _err(E.COOKIE_SOURCE), _err(E.HTTP_403), _err(E.ANTI_BOT_CHALLENGE),
    _err(E.DISK_FULL),
    _err(None, "stalled", DownloadStalledError), _err(None, "timed out", DownloadTimeoutError),
    _err(None, "download pool is wedged: all 8 workers are held"),
])
def test_never_worth_a_url_fix(err, fixer):
    assert asyncio.run(fetch._corrected_link(_req(), err)) is None
    fixer.assert_not_called()


@pytest.mark.parametrize("cls", [E.UNSUPPORTED_URL, E.HTTP_404, E.NO_FORMATS, E.UNKNOWN])
def test_url_shaped_errors_still_qualify(cls, fixer):
    assert asyncio.run(fetch._corrected_link(_req(), _err(cls))) == "https://www.youtube.com/watch?v=x"
    assert fixer.await_count == 1


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


class TestUrlPath:
    def test_the_url_path_passes_its_options_and_records_the_link_fetched(self, tmp_path):
        from backend.core import task_runner
        fetched = {"video_path": None, "url": "https://www.youtube.com/watch?v=fixed"}
        fv = AsyncMock(return_value=fetched)
        with patch("backend.download.fetch.fetch_video", new=fv), \
             patch("backend.core.task_runner._detect_platform", side_effect=RuntimeError("stop here")) as plat:
            with pytest.raises(RuntimeError, match="stop here"):
                asyncio.run(task_runner._download_single_video_to_db(
                    "job", "https://youtu.be/x", "", "local", options={"quality": "720p"}))
        assert fv.await_args.kwargs["options"] == {"quality": "720p"}
        assert fv.await_args.kwargs["job_id"] == "job"
        assert plat.call_args.args[0] == "https://www.youtube.com/watch?v=fixed"
