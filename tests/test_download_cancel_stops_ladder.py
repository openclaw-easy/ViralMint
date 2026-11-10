# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A cancel stops the ladder.

The transfer in flight can't be interrupted, but everything after it used to
run anyway: the backoff, the remaining attempts and format rungs, the cookie
retry, the audio re-download and the AI URL fix (a whole second download).
The ladder now stops with the error it already has, so the cancelled job
keeps its real reason.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.exceptions import DownloadError, JobCancelledError
from backend.services import ytdlp_service as YT
from tests.test_ytdlp_download import _DownloadError, _install_yt_dlp

NET = "ERROR: Connection reset by peer"
ODD = "ERROR: something odd"
FMT = "ERROR: Requested format is not available"
BOT = "ERROR: Sign in to confirm you're not a bot. Use --cookies-from-browser"


class _Cancel:
    """job_cancelled: False for the first `after` reads, then True (None = never)."""

    def __init__(self, after=0):
        self.after, self.reads = after, 0

    async def __call__(self, job_id):
        self.reads += 1
        return self.after is not None and self.reads > self.after


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(YT, "_check_cooldown", lambda: 0)
    monkeypatch.setattr(YT, "_get_cookie_file", lambda: None)
    monkeypatch.setattr(YT, "_detect_cookie_browser", lambda: None)
    sleeps = []
    monkeypatch.setattr(YT.time, "sleep", lambda s: sleeps.append(s))
    env = {"dir": tmp_path, "sleeps": sleeps}

    def cancel(after=0):
        c = _Cancel(after)
        monkeypatch.setattr("backend.agents.job_helper.job_cancelled", c)
        return c
    env["cancel"] = cancel
    return env


def _video(env, job_id="j1"):
    return asyncio.run(YT.download_video(
        "https://youtu.be/testid", output_dir=env["dir"], filename="testid",
        extract_audio=False, job_id=job_id))


class TestVideoLadder:
    @pytest.mark.parametrize("msg", [NET, ODD, FMT])
    def test_a_cancel_during_the_first_transfer_starts_nothing_more(self, env, monkeypatch, msg):
        env["cancel"](after=0)
        calls = _install_yt_dlp(monkeypatch, [_DownloadError(msg)] * 40, env["dir"])
        with pytest.raises(DownloadError) as e:
            _video(env)
        assert not isinstance(e.value, JobCancelledError)
        assert "cancelled" in str(e.value) and msg.split("ERROR: ")[1] in str(e.value)
        assert len(calls) == 1
        assert sum(env["sleeps"]) == 0

    def test_a_cancel_during_backoff_wakes_it_within_a_slice(self, env, monkeypatch):
        env["cancel"](after=2)
        calls = _install_yt_dlp(monkeypatch, [_DownloadError(NET)] * 40, env["dir"])
        with pytest.raises(DownloadError, match="cancelled"):
            _video(env)
        assert len(calls) == 1
        assert 0 < sum(env["sleeps"]) < 5          # the backoff is 5 s
        assert max(env["sleeps"]) <= YT._CANCEL_POLL_S

    def test_a_cancelled_job_never_reaches_the_cookie_retry(self, env, monkeypatch):
        env["cancel"](after=0)
        monkeypatch.setattr(YT, "_detect_cookie_browser", lambda: "chrome")
        applied = []
        monkeypatch.setattr(YT, "_apply_cookies", lambda o: applied.append(o))
        calls = _install_yt_dlp(monkeypatch, [_DownloadError(BOT)] * 40, env["dir"])
        with pytest.raises(DownloadError, match="cancelled"):
            _video(env)
        assert len(applied) == 1, "only the base opts' cookies, no refresh"
        assert len(calls) == 1

    def test_a_cancel_is_not_a_yt_dlp_failure(self, env, monkeypatch):
        env["cancel"](after=0)
        monkeypatch.setattr(YT, "_consecutive_download_failures", 0)
        _install_yt_dlp(monkeypatch, [_DownloadError(ODD)] * 40, env["dir"])
        with pytest.raises(DownloadError):
            _video(env)
        assert YT._consecutive_download_failures == 0

    def test_no_cancel_keeps_the_whole_ladder(self, env, monkeypatch):
        env["cancel"](after=None)
        calls = _install_yt_dlp(monkeypatch, [_DownloadError(NET)] * 40, env["dir"])
        with pytest.raises(DownloadError) as e:
            _video(env)
        assert "cancelled" not in str(e.value)
        assert len(calls) >= 2
        assert sum(env["sleeps"]) >= 5

    def test_a_download_with_no_job_reads_no_job_and_sleeps_whole(self, env, monkeypatch):
        c = env["cancel"](after=0)
        _install_yt_dlp(monkeypatch, [_DownloadError(NET)] * 40, env["dir"])
        with pytest.raises(DownloadError):
            _video(env, job_id=None)
        assert c.reads == 0
        assert 5 in env["sleeps"]


class TestTheCheckFromAThread:
    def test_a_dead_loop_reads_as_not_cancelled_and_never_hangs(self):
        loop = asyncio.new_event_loop()
        loop.close()
        assert YT._job_cancelled_from_thread("j", loop) is False

    def test_a_loop_that_never_answers_times_out(self, monkeypatch):
        monkeypatch.setattr(YT, "_CANCEL_CHECK_TIMEOUT_S", 0.05)
        loop = asyncio.new_event_loop()     # never running: the read can't complete
        try:
            assert YT._job_cancelled_from_thread("j", loop) is False
        finally:
            loop.close()

    def test_no_job_is_never_cancelled(self):
        assert YT._job_cancelled_from_thread(None, None) is False


class _NoSettingsDb:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def execute(self, *a):
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=None)
        return r


class TestTheAiUrlFixIsNotStarted:
    def test_url_path_after_a_cancel(self, monkeypatch):
        from backend.core import task_runner
        monkeypatch.setattr("backend.agents.job_helper.job_cancelled", AsyncMock(return_value=True))
        fix = AsyncMock(return_value="https://example.com/fixed")
        monkeypatch.setattr("backend.core.ai_retry.ai_fix_url", fix)
        err = DownloadError("Download failed: odd")
        dv = AsyncMock(side_effect=err)
        monkeypatch.setattr(YT, "download_video", dv)
        with pytest.raises(DownloadError) as e:
            asyncio.run(task_runner._download_single_video_to_db("j1", "https://odd.example/v", "", "local"))
        assert e.value is err
        fix.assert_not_called()
        assert dv.await_args.kwargs["job_id"] == "j1"

    def test_url_path_cancel_while_the_fix_thinks_keeps_the_error(self, monkeypatch):
        from backend.core import task_runner
        c = _Cancel(after=None)

        async def fix(*a, **kw):
            c.after = 0
            return "https://example.com/fixed"
        monkeypatch.setattr("backend.agents.job_helper.job_cancelled", c)
        monkeypatch.setattr("backend.core.ai_retry.ai_fix_url", fix)
        monkeypatch.setattr("backend.database.AsyncSessionLocal", _NoSettingsDb)
        err = DownloadError("Download failed: odd")
        dv = AsyncMock(side_effect=err)
        monkeypatch.setattr(YT, "download_video", dv)
        with pytest.raises(DownloadError) as e:
            asyncio.run(task_runner._download_single_video_to_db("j1", "https://odd.example/v", "", "local"))
        assert e.value is err
        assert dv.await_count == 1

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
             patch("backend.agents.downloader.download_video",
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
