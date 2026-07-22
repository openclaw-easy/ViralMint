# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Behavioral tests for backend/core/task_runner.py.

Covers the job-dispatch heart of the app: the concurrency-limited
`dispatch` / `_run_with_limit` primitives, the thin agent-wrapping runners'
failure routing (an agent raising must land the job in `failed`, never crash
the loop), the channel-analysis parse + AI-summary helpers, and the
orchestration runners (batch download / batch generate / news save /
extract clips) exercised through mocked collaborators.

Everything is mocked — no DB, no network, no ffmpeg, no whisper — so the
suite is fast and hermetic. Lazy `from backend...` imports inside each runner
mean we patch the SOURCE module (e.g. backend.agents.job_helper.update_job_status)
rather than a task_runner-local rebinding.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core import task_runner as tr


# ── Shared fakes ────────────────────────────────────────────────────────────

def _fake_session_cm(scalar_return=None):
    """A fake `async with AsyncSessionLocal() as db:` context manager whose
    `execute(...).scalar_one_or_none()` returns `scalar_return`."""
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_return)
    result.fetchall = MagicMock(return_value=[])
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    class CM:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    return CM, session


@pytest.fixture
def job_status_spy():
    with patch("backend.agents.job_helper.update_job_status", new=AsyncMock()) as m:
        yield m


@pytest.fixture
def ws_spy():
    mock = MagicMock()
    mock.send = AsyncMock()
    mock.send_progress = AsyncMock()
    mock.send_constraint_warning = AsyncMock()
    with patch("backend.core.ws_manager.ws_manager", new=mock):
        yield mock


def _statuses(spy):
    """All `status` positional args passed to update_job_status."""
    return [c.args[1] for c in spy.call_args_list if len(c.args) >= 2]


# ── dispatch / _run_with_limit ──────────────────────────────────────────────

class TestDispatchAndRunWithLimit:
    async def test_run_with_limit_runs_the_coro(self):
        ran = asyncio.Event()

        async def work():
            ran.set()

        await tr._run_with_limit(work())
        assert ran.is_set()

    async def test_run_with_limit_swallows_exceptions(self):
        async def work():
            raise RuntimeError("boom")

        # Last-resort catch — must NOT propagate.
        await tr._run_with_limit(work())

    async def test_dispatch_schedules_and_runs_to_completion(self):
        ran = asyncio.Event()

        async def work():
            ran.set()

        tr.dispatch(work())
        await asyncio.wait_for(ran.wait(), timeout=2)


# ── Thin agent-wrapping runners: failure routing ────────────────────────────

class TestSimpleRunnerFailureRouting:
    async def test_run_scout_success_does_not_mark_failed(self, job_status_spy):
        agent = MagicMock()
        agent.run = AsyncMock()
        with patch("backend.agents.scout.ScoutAgent", return_value=agent):
            await tr.run_scout("job1", "ai tools", ["youtube"])
        agent.run.assert_awaited_once()
        assert "failed" not in _statuses(job_status_spy)

    async def test_run_scout_failure_routes_to_failed(self, job_status_spy):
        agent = MagicMock()
        agent.run = AsyncMock(side_effect=RuntimeError("scout boom"))
        with patch("backend.agents.scout.ScoutAgent", return_value=agent):
            await tr.run_scout("job1", "ai tools", ["youtube"])
        job_status_spy.assert_awaited_once()
        assert job_status_spy.call_args.args[1] == "failed"
        assert job_status_spy.call_args.kwargs["error_message"] == "scout boom"

    async def test_run_download_failure_routes_to_failed(self, job_status_spy):
        dl = MagicMock()
        dl.run = AsyncMock(side_effect=RuntimeError("dl boom"))
        with patch("backend.agents.downloader.DownloadAgent", return_value=dl), \
             patch("backend.agents.analyzer.AnalyzerAgent", return_value=MagicMock()):
            await tr.run_download("job1", ["sr1"])
        assert _statuses(job_status_spy) == ["failed"]

    async def test_run_download_success_chains_analyzer(self, job_status_spy):
        dl = MagicMock(); dl.run = AsyncMock()
        an = MagicMock(); an.run = AsyncMock()
        with patch("backend.agents.downloader.DownloadAgent", return_value=dl), \
             patch("backend.agents.analyzer.AnalyzerAgent", return_value=an):
            await tr.run_download("job1", ["sr1"])
        dl.run.assert_awaited_once()
        an.run.assert_awaited_once()
        assert "failed" not in _statuses(job_status_spy)

    async def test_run_generate_failure_routes_to_failed(self, job_status_spy):
        gen = MagicMock()
        gen.run = AsyncMock(side_effect=RuntimeError("gen boom"))
        with patch("backend.agents.generator.GeneratorAgent", return_value=gen):
            await tr.run_generate("job1", "dv1")
        assert _statuses(job_status_spy) == ["failed"]

    async def test_run_reanalyze_failure_routes_to_failed(self, job_status_spy):
        an = MagicMock()
        an.reanalyze_single = AsyncMock(side_effect=RuntimeError("re boom"))
        with patch("backend.agents.analyzer.AnalyzerAgent", return_value=an):
            await tr.run_reanalyze("job1", "vid1")
        assert _statuses(job_status_spy) == ["failed"]

    async def test_run_upload_failure_routes_to_failed(self, job_status_spy):
        up = MagicMock()
        up.run = AsyncMock(side_effect=RuntimeError("up boom"))
        with patch("backend.agents.uploader.UploadAgent", return_value=up):
            await tr.run_upload("job1", "gv1", ["youtube"])
        assert _statuses(job_status_spy) == ["failed"]

    async def test_run_news_scout_failure_routes_to_failed(self, job_status_spy):
        agent = MagicMock()
        agent.run = AsyncMock(side_effect=RuntimeError("news boom"))
        with patch("backend.agents.news_scout.NewsScoutAgent", return_value=agent):
            await tr.run_news_scout("job1", "ai")
        assert _statuses(job_status_spy) == ["failed"]


# ── _analyze_youtube_channel ────────────────────────────────────────────────

class TestAnalyzeYoutubeChannel:
    async def test_parses_channel_and_prefixes_bare_video_ids(self):
        info = {
            "channel": "Cool Channel",
            "description": "d",
            "channel_follower_count": 1000,
            "channel_url": "https://youtube.com/c",
            "thumbnails": [{"url": "thumb"}],
            "entries": [
                {"id": "v1", "title": "V1", "url": "v1", "view_count": 10},
                {"id": "v2", "title": "V2", "url": "https://youtube.com/watch?v=v2"},
            ],
        }
        with patch("backend.services.ytdlp_service.get_video_info",
                   new=AsyncMock(return_value=info)):
            out = await tr._analyze_youtube_channel("https://youtube.com/c")
        assert out["platform"] == "youtube"
        assert out["channel_title"] == "Cool Channel"
        assert out["subscriber_count"] == 1000
        assert out["thumbnail"] == "thumb"
        assert len(out["videos"]) == 2
        # bare id gets the watch?v= prefix; already-full url is left alone
        assert out["videos"][0]["url"] == "https://www.youtube.com/watch?v=v1"
        assert out["videos"][1]["url"] == "https://youtube.com/watch?v=v2"

    async def test_returns_none_when_no_channel_info(self):
        with patch("backend.services.ytdlp_service.get_video_info",
                   new=AsyncMock(return_value=None)):
            out = await tr._analyze_youtube_channel("https://youtube.com/c")
        assert out is None


# ── _analyze_tiktok_channel ─────────────────────────────────────────────────

class TestAnalyzeTiktokChannel:
    async def test_parses_and_renames_hashtag_titles(self):
        result = {
            "user": {"display_name": "TT User", "follower_count": 500, "video_count": 3},
            "videos": [
                {"url": "u1", "video_id": "1", "title": "#dance #fun",
                 "view_count": 100, "created_at": 1_600_000_000},
                {"url": "u2", "video_id": "2", "title": "Real Title", "view_count": 5},
            ],
        }
        with patch("backend.services.channel_reader.get_tiktok_channel",
                   new=AsyncMock(return_value=result)):
            out = await tr._analyze_tiktok_channel("https://tiktok.com/@u")
        assert out["platform"] == "tiktok"
        assert out["channel_title"] == "TT User"
        assert out["subscriber_count"] == 500
        # hashtag-only title becomes "Video #N — ..."
        assert out["videos"][0]["title"].startswith("Video #1")
        # a Unix ts is formatted to YYYY-MM-DD
        assert out["videos"][0]["upload_date"] and "-" in out["videos"][0]["upload_date"]
        # a real title is preserved
        assert out["videos"][1]["title"] == "Real Title"


# ── _generate_channel_ai_analysis ───────────────────────────────────────────

class TestGenerateChannelAiAnalysis:
    async def test_returns_stripped_ai_text(self):
        cm, _ = _fake_session_cm(scalar_return=None)
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="  ## Analysis  ")
        with patch("backend.database.AsyncSessionLocal", return_value=cm()), \
             patch("backend.core.ai_provider.get_ai_client", return_value=ai):
            out = await tr._generate_channel_ai_analysis(
                {"channel_title": "C", "videos": []}, "local")
        assert out == "## Analysis"
        ai.chat.assert_awaited_once()

    async def test_returns_empty_string_on_error(self):
        # AI/db failures are non-fatal: swallow and return "".
        with patch("backend.database.AsyncSessionLocal", side_effect=RuntimeError("db down")):
            out = await tr._generate_channel_ai_analysis({"videos": []}, "local")
        assert out == ""


# ── run_analyze_channel (orchestration) ─────────────────────────────────────

class TestRunAnalyzeChannel:
    async def test_youtube_success_sends_channel_analysis(self, job_status_spy, ws_spy):
        summary = {"channel_title": "C", "videos": [], "platform": "youtube"}
        with patch.object(tr, "_analyze_youtube_channel", new=AsyncMock(return_value=summary)), \
             patch.object(tr, "_generate_channel_ai_analysis", new=AsyncMock(return_value="AI")):
            await tr.run_analyze_channel("job1", "https://youtube.com/c")
        assert "success" in _statuses(job_status_spy)
        sent_types = [c.args[0].get("type") for c in ws_spy.send.call_args_list]
        assert "channel_analysis" in sent_types
        assert "job_complete" in sent_types

    async def test_tiktok_routes_to_tiktok_helper(self, job_status_spy, ws_spy):
        summary = {"channel_title": "T", "videos": [], "platform": "tiktok"}
        tt = AsyncMock(return_value=summary)
        yt = AsyncMock(return_value=None)
        with patch.object(tr, "_analyze_tiktok_channel", new=tt), \
             patch.object(tr, "_analyze_youtube_channel", new=yt), \
             patch.object(tr, "_generate_channel_ai_analysis", new=AsyncMock(return_value="")):
            await tr.run_analyze_channel("job1", "https://www.tiktok.com/@u")
        tt.assert_awaited_once()
        yt.assert_not_awaited()

    async def test_none_summary_routes_to_failed(self, job_status_spy, ws_spy):
        with patch.object(tr, "_analyze_youtube_channel", new=AsyncMock(return_value=None)):
            await tr.run_analyze_channel("job1", "https://youtube.com/c")
        assert "failed" in _statuses(job_status_spy)
        assert "job_failed" in [c.args[0].get("type") for c in ws_spy.send.call_args_list]


# ── run_analyze_imported ────────────────────────────────────────────────────

class TestRunAnalyzeImported:
    async def test_missing_video_routes_to_failed(self, job_status_spy, ws_spy):
        cm, _ = _fake_session_cm(scalar_return=None)  # DownloadedVideo not found
        with patch("backend.database.AsyncSessionLocal", return_value=cm()):
            await tr.run_analyze_imported("job1", "dv-missing")
        assert "failed" in _statuses(job_status_spy)
        assert "job_failed" in [c.args[0].get("type") for c in ws_spy.send.call_args_list]


# ── run_batch_download_urls ─────────────────────────────────────────────────

class TestRunBatchDownloadUrls:
    async def test_all_downloads_fail_routes_to_failed(self, job_status_spy, ws_spy):
        with patch.object(tr, "_download_single_video_to_db",
                          new=AsyncMock(side_effect=RuntimeError("dl fail"))):
            await tr.run_batch_download_urls(
                "job1", [{"url": "u1", "title": "t1"}])
        assert _statuses(job_status_spy)[-1] == "failed"
        assert "job_failed" in [c.args[0].get("type") for c in ws_spy.send.call_args_list]

    async def test_success_analyzes_and_completes(self, job_status_spy, ws_spy):
        an = MagicMock(); an.run = AsyncMock()
        with patch.object(tr, "_download_single_video_to_db",
                          new=AsyncMock(return_value="dv1")), \
             patch("backend.agents.analyzer.AnalyzerAgent", return_value=an):
            await tr.run_batch_download_urls(
                "job1", [{"url": "u1", "title": "t1"}])
        an.run.assert_awaited_once()
        assert _statuses(job_status_spy)[-1] == "success"
        assert "job_complete" in [c.args[0].get("type") for c in ws_spy.send.call_args_list]

    async def test_skips_items_with_no_url(self, job_status_spy, ws_spy):
        dl = AsyncMock(return_value="dv1")
        an = MagicMock(); an.run = AsyncMock()
        with patch.object(tr, "_download_single_video_to_db", new=dl), \
             patch("backend.agents.analyzer.AnalyzerAgent", return_value=an), \
             patch("backend.core.http_utils.jittered_delay", return_value=0):
            await tr.run_batch_download_urls(
                "job1", [{"url": "", "title": "skip"}, {"url": "u2", "title": "t2"}])
        # only the one with a URL was downloaded
        assert dl.await_count == 1


# ── run_batch_generate ──────────────────────────────────────────────────────

class TestRunBatchGenerate:
    async def test_all_succeed_marks_parent_success(self, job_status_spy, ws_spy):
        child = MagicMock(); child.id = "child1"
        gen = MagicMock(); gen.run = AsyncMock()
        with patch("backend.agents.job_helper.create_job", new=AsyncMock(return_value=child)), \
             patch("backend.agents.generator.GeneratorAgent", return_value=gen):
            await tr.run_batch_generate(
                "parent", [{"downloaded_video_id": "v1"}], {"aspect_ratio": "9:16"})
        gen.run.assert_awaited_once()
        assert _statuses(job_status_spy)[-1] == "success"
        assert "job_complete" in [c.args[0].get("type") for c in ws_spy.send.call_args_list]

    async def test_all_fail_marks_parent_failed(self, job_status_spy, ws_spy):
        child = MagicMock(); child.id = "child1"
        gen = MagicMock(); gen.run = AsyncMock(side_effect=RuntimeError("gen boom"))
        with patch("backend.agents.job_helper.create_job", new=AsyncMock(return_value=child)), \
             patch("backend.agents.generator.GeneratorAgent", return_value=gen):
            await tr.run_batch_generate("parent", [{"downloaded_video_id": "v1"}], {})
        # both the child fail-mark AND the parent final status are "failed"
        assert _statuses(job_status_spy)[-1] == "failed"
        assert "job_failed" in [c.args[0].get("type") for c in ws_spy.send.call_args_list]

    async def test_partial_failure_still_succeeds(self, job_status_spy, ws_spy):
        children = [MagicMock(id="c1"), MagicMock(id="c2")]
        gen = MagicMock()
        gen.run = AsyncMock(side_effect=[None, RuntimeError("second fails")])
        with patch("backend.agents.job_helper.create_job",
                   new=AsyncMock(side_effect=children)), \
             patch("backend.agents.generator.GeneratorAgent", return_value=gen):
            await tr.run_batch_generate(
                "parent", [{"downloaded_video_id": "v1"}, {"downloaded_video_id": "v2"}], {})
        # 1 ok + 1 failed → overall success (not all failed)
        assert _statuses(job_status_spy)[-1] == "success"


# ── run_news_save ───────────────────────────────────────────────────────────

class TestRunNewsSave:
    async def test_empty_ids_completes_success(self, job_status_spy, ws_spy):
        cm, session = _fake_session_cm()
        with patch("backend.database.AsyncSessionLocal", return_value=cm()):
            await tr.run_news_save("job1", [])
        assert _statuses(job_status_spy)[-1] == "success"
        session.commit.assert_awaited()
        assert "news_saved" in [c.args[0].get("type") for c in ws_spy.send.call_args_list]

    async def test_db_error_routes_to_failed(self, job_status_spy, ws_spy):
        with patch("backend.database.AsyncSessionLocal", side_effect=RuntimeError("db x")):
            await tr.run_news_save("job1", ["a1"])
        assert "failed" in _statuses(job_status_spy)


# ── run_download_url routing ────────────────────────────────────────────────

class TestRunDownloadUrl:
    async def test_routes_channel_url_to_channel_downloader(self, job_status_spy, ws_spy):
        ch = AsyncMock()
        with patch("backend.services.ytdlp_service.is_channel_or_playlist_url",
                   return_value=True), \
             patch.object(tr, "_download_channel", new=ch), \
             patch.object(tr, "_download_single_url", new=AsyncMock()) as single:
            await tr.run_download_url("job1", "https://youtube.com/@chan")
        ch.assert_awaited_once()
        single.assert_not_awaited()

    async def test_routes_single_url_to_single_downloader(self, job_status_spy, ws_spy):
        single = AsyncMock()
        with patch("backend.services.ytdlp_service.is_channel_or_playlist_url",
                   return_value=False), \
             patch.object(tr, "_download_channel", new=AsyncMock()) as ch, \
             patch.object(tr, "_download_single_url", new=single):
            await tr.run_download_url("job1", "https://youtube.com/watch?v=x")
        single.assert_awaited_once()
        ch.assert_not_awaited()

    async def test_error_routes_to_failed(self, job_status_spy, ws_spy):
        with patch("backend.services.ytdlp_service.is_channel_or_playlist_url",
                   side_effect=RuntimeError("bad url")):
            await tr.run_download_url("job1", "https://youtube.com/x")
        assert "failed" in _statuses(job_status_spy)
        assert "job_failed" in [c.args[0].get("type") for c in ws_spy.send.call_args_list]


# ── run_extract_clips ───────────────────────────────────────────────────────

class TestRunExtractClips:
    async def test_video_not_found_routes_to_failed(self, job_status_spy, ws_spy):
        from backend.services.clip_options import ExtractOptions
        cm, _ = _fake_session_cm(scalar_return=None)  # DownloadedVideo missing
        with patch("backend.database.AsyncSessionLocal", return_value=cm()):
            await tr.run_extract_clips("job1", "missing", ExtractOptions())
        assert "failed" in _statuses(job_status_spy)
        assert "job_failed" in [c.args[0].get("type") for c in ws_spy.send.call_args_list]

    async def test_no_clips_extracted_routes_to_failed(self, job_status_spy, ws_spy):
        from backend.services.clip_options import ExtractOptions
        video = MagicMock(); video.title = "Src"
        cm, _ = _fake_session_cm(scalar_return=video)
        with patch("backend.database.AsyncSessionLocal", return_value=cm()), \
             patch("backend.services.clip_extractor.extract_viral_clips",
                   new=AsyncMock(return_value=[])):
            await tr.run_extract_clips("job1", "dv1", ExtractOptions())
        assert "failed" in _statuses(job_status_spy)
