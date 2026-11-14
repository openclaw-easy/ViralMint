# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A cancel stops the ladder, and nothing after it starts.

The ladder's own steps (attempts, waits, the cookie refresh) are pinned in
tests/test_download_engine.py. Here: the thread-side cancel read, the AI link
fix (a whole second download) never starting after a cancel, and the scout
batch passing its job id down.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.exceptions import DownloadError
from backend.download import fetch, progress


class _Cancel:
    """job_cancelled: False for the first `after` reads, then True (None = never)."""

    def __init__(self, after=0):
        self.after, self.reads = after, 0

    async def __call__(self, job_id):
        self.reads += 1
        return self.after is not None and self.reads > self.after


class TestTheCheckFromAThread:
    def test_a_dead_loop_reads_as_not_cancelled_and_never_hangs(self):
        loop = asyncio.new_event_loop()
        loop.close()
        assert progress.job_cancelled_from_thread("j", loop) is False

    def test_a_loop_that_never_answers_times_out(self, monkeypatch):
        monkeypatch.setattr(progress, "_CANCEL_CHECK_TIMEOUT_S", 0.05)
        loop = asyncio.new_event_loop()     # never running: the read can't complete
        try:
            assert progress.job_cancelled_from_thread("j", loop) is False
        finally:
            loop.close()

    def test_no_job_is_never_cancelled(self):
        assert progress.job_cancelled_from_thread(None, None) is False


class _NoSettingsDb:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def execute(self, *a):
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=None)
        return r


class TestTheAiUrlFixIsNotStarted:
    def _failing_once(self, monkeypatch, err):
        async def once(req):
            raise err
        monkeypatch.setattr(fetch, "_once", once)

    def test_after_a_cancel(self, monkeypatch):
        monkeypatch.setattr("backend.agents.job_helper.job_cancelled", AsyncMock(return_value=True))
        fix = AsyncMock(return_value="https://example.com/fixed")
        monkeypatch.setattr("backend.core.ai_retry.ai_fix_url", fix)
        err = DownloadError("Download failed: odd")
        err.error_class = fetch.ErrorClass.UNKNOWN
        self._failing_once(monkeypatch, err)
        with pytest.raises(DownloadError) as e:
            asyncio.run(fetch.fetch_video("https://odd.example/v", job_id="j1"))
        assert e.value is err
        fix.assert_not_called()

    def test_a_cancel_while_the_fix_thinks_keeps_the_error(self, monkeypatch):
        c = _Cancel(after=None)

        async def fix(*a, **kw):
            c.after = 0
            return "https://example.com/fixed"
        monkeypatch.setattr("backend.agents.job_helper.job_cancelled", c)
        monkeypatch.setattr("backend.core.ai_retry.ai_fix_url", fix)
        monkeypatch.setattr("backend.database.AsyncSessionLocal", _NoSettingsDb)
        calls = []
        err = DownloadError("Download failed: odd")
        err.error_class = fetch.ErrorClass.UNKNOWN

        async def once(req):
            calls.append(req.url)
            raise err
        monkeypatch.setattr(fetch, "_once", once)
        with pytest.raises(DownloadError) as e:
            asyncio.run(fetch.fetch_video("https://odd.example/v", job_id="j1"))
        assert e.value is err
        assert calls == ["https://odd.example/v"]

    def test_scout_path_after_a_cancel(self):
        import tests.test_downloader as t
        from backend.agents.downloader import DownloadAgent
        sr = t._make_sr("a")
        with patch("backend.agents.downloader.shutil.disk_usage") as du, \
             patch("backend.agents.downloader.ws_manager.send_progress", new=AsyncMock()), \
             patch("backend.agents.downloader.ws_manager.send_constraint_warning", new=AsyncMock()), \
             patch("backend.agents.downloader.ws_manager.send", new=AsyncMock()), \
             patch("backend.agents.downloader.update_job_status", new=AsyncMock()) as ujs, \
             patch("backend.agents.downloader.AsyncSessionLocal",
                   side_effect=lambda: t._fake_session(sr)), \
             patch("backend.agents.downloader.fetch_video",
                   new=AsyncMock(side_effect=DownloadError("Download failed: odd"))) as dv, \
             patch("backend.agents.downloader.job_cancelled",
                   new=AsyncMock(side_effect=[False, True, True])), \
             patch("backend.agents.job_helper.job_cancelled", new=AsyncMock(return_value=True)), \
             patch("backend.core.ai_retry.ai_fix_url", new=AsyncMock()) as fix:
            du.return_value = MagicMock(free=10_000_000_000)
            asyncio.run(DownloadAgent().run("j1", ["a"], user_id="local"))
        fix.assert_not_called()
        assert dv.await_args.kwargs["job_id"] == "j1"
        last = ujs.await_args_list[-1]
        assert last.args[1] == "cancelled" and "Download failed: odd" in last.kwargs["error_message"]
