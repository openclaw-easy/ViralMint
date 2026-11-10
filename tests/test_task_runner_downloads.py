# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Download and analyze runners — the job layer above the yt-dlp ladder.

`ytdlp_service` handles "can this URL be fetched at all". These runners handle
what happens around that: persisting the result, reporting progress, and —
mostly — deciding what a partial failure means.

The rule that matters is the one an earlier audit had to fix: a batch where
0 of N succeed must mark the job FAILED. Reporting "success" over an empty
batch is the worst outcome available, because the user goes looking for videos
that were never downloaded and has no error to act on. A batch where SOME
succeed is a success with the failures named.

yt-dlp, Whisper, the analyzer and ffprobe are stubbed.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from backend.core import task_runner as TR
from backend.database import AsyncSessionLocal, init_db


@pytest.fixture(scope="module", autouse=True)
def _schema():
    asyncio.run(init_db())


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """The batch runner sleeps a jittered delay between downloads (polite
    pacing against the platform). Without this the suite waits ~2 real
    minutes for nothing."""
    async def instant(_s):
        return None
    monkeypatch.setattr(TR.asyncio, "sleep", instant)


@pytest.fixture(autouse=True)
def quiet_ws(monkeypatch):
    async def noop(*a, **k):
        return None
    from backend.core.ws_manager import ws_manager
    monkeypatch.setattr(ws_manager, "send", noop)
    monkeypatch.setattr(ws_manager, "send_progress", noop)
    monkeypatch.setattr(ws_manager, "send_constraint_warning", noop)


@pytest.fixture()
def downloader(monkeypatch, tmp_path):
    """Stub the yt-dlp layer; each test scripts per-URL outcomes."""
    outcomes: dict = {}
    calls: list[str] = []
    seen_options: list = []

    async def fake_download(url, output_dir=None, filename=None,
                            extract_audio=True, options=None, job_id=None):
        calls.append(url)
        seen_options.append(options)
        out = outcomes.get(url, "ok")
        if isinstance(out, Exception):
            raise out
        vid = tmp_path / f"{abs(hash(url)) % 10**8}.mp4"
        vid.write_bytes(b"\x00" * 4096)
        # The real service returns STRINGS here — its docstring says Path, but
        # `download_video` does `str(video_path)`, and the DB layer can't bind
        # a PosixPath. Returning a Path from this stub hides that mismatch.
        return {"video_path": str(vid), "audio_path": None, "duration": 120,
                "file_size_mb": 1.0, "subtitles": None, "chapters": None,
                "tags": None, "category": None, "title": "A video",
                "height": 1080, "requested_quality": (options or {}).get("quality"),
                "kept_subtitle_paths": [], "option_postprocessors_dropped": False}

    monkeypatch.setattr("backend.services.ytdlp_service.download_video",
                        fake_download)
    return {"outcomes": outcomes, "calls": calls, "options": seen_options}


@pytest.fixture()
def no_analyzer(monkeypatch):
    """The downloader chains into analysis; that has its own suite."""
    async def noop(*a, **k):
        return None
    monkeypatch.setattr("backend.agents.analyzer.AnalyzerAgent.run", noop)
    return noop


async def _job(job_type="download") -> str:
    from backend.agents.job_helper import create_job
    return (await create_job(job_type, "local", {})).id


async def _job_row(job_id):
    from backend.models.job import Job
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()


# ── platform detection ──────────────────────────────────────────────────────

class TestDetectPlatform:
    @pytest.mark.parametrize("url,expected", [
        ("https://www.youtube.com/watch?v=abc", "youtube"),
        ("https://youtu.be/abc", "youtube"),
        ("https://m.youtube.com/watch?v=abc", "youtube"),
        ("https://www.tiktok.com/@x/video/1", "tiktok"),
        ("https://www.bilibili.com/video/BV1", "bilibili"),
        ("https://vimeo.com/12345", "vimeo"),
    ])
    def test_it_reads_the_platform_off_the_domain(self, url, expected):
        """Generic by design — a hardcoded list would reject every host we
        haven't met, and yt-dlp supports hundreds."""
        assert TR._detect_platform(url) == expected

    @pytest.mark.parametrize("bad", ["", "not a url", "://nope", None])
    def test_junk_degrades_to_unknown_rather_than_raising(self, bad):
        assert TR._detect_platform(bad or "") in ("unknown", "")


# ── the batch contract ──────────────────────────────────────────────────────

class TestBatchDownload:
    def _run(self, urls):
        async def go():
            jid = await _job()
            await TR.run_batch_download_urls(jid, urls, user_id="local")
            return await _job_row(jid)
        return asyncio.run(go())

    def test_a_fully_successful_batch_succeeds(self, downloader, no_analyzer):
        row = self._run([{"url": "https://youtu.be/a", "title": "A"},
                         {"url": "https://youtu.be/b", "title": "B"}])
        assert row.status == "success"
        assert len(downloader["calls"]) == 2

    def test_a_batch_where_NOTHING_succeeds_is_marked_failed(
            self, downloader, no_analyzer):
        """The worst available outcome is reporting success over an empty
        batch: the user goes looking for videos that were never downloaded
        and has no error to act on."""
        downloader["outcomes"]["https://youtu.be/a"] = RuntimeError("gone")
        downloader["outcomes"]["https://youtu.be/b"] = RuntimeError("gone")
        row = self._run([{"url": "https://youtu.be/a"},
                         {"url": "https://youtu.be/b"}])
        assert row.status == "failed"

    def test_a_partial_batch_still_succeeds(self, downloader, no_analyzer):
        """One dead link must not throw away the video that did download."""
        downloader["outcomes"]["https://youtu.be/a"] = RuntimeError("gone")
        row = self._run([{"url": "https://youtu.be/a"},
                         {"url": "https://youtu.be/b"}])
        assert row.status == "success"

    def test_an_empty_batch_does_not_crash(self, downloader, no_analyzer):
        row = self._run([])
        assert row.status in ("success", "failed")

    def test_each_url_is_attempted_once(self, downloader, no_analyzer):
        self._run([{"url": "https://youtu.be/a"}, {"url": "https://youtu.be/b"},
                   {"url": "https://youtu.be/c"}])
        assert len(downloader["calls"]) == 3

    def test_no_options_reaches_the_downloader_as_none(self, downloader,
                                                       no_analyzer):
        """The invariant that keeps every pre-existing caller byte-identical:
        a batch that asked for nothing must not invent an options dict."""
        self._run([{"url": "https://youtu.be/a"}])
        assert downloader["options"] == [None]

    def test_options_apply_to_every_url_in_the_batch(self, downloader,
                                                     no_analyzer):
        async def go():
            jid = await _job()
            await TR.run_batch_download_urls(
                jid, [{"url": "https://youtu.be/a"}, {"url": "https://youtu.be/b"}],
                user_id="local", options={"quality": "720p"})
            return await _job_row(jid)
        asyncio.run(go())
        assert downloader["options"] == [{"quality": "720p"}] * 2

    def test_the_job_persists_a_delivery_receipt_per_video(self, downloader,
                                                           no_analyzer):
        """What the user GOT vs what they asked for. Without this on the job
        row there is nowhere honest for a UI to say "you asked for 4K, this
        source only had 1080p" — the WS completion event carries no output."""
        import json as _json

        async def go():
            jid = await _job()
            await TR.run_batch_download_urls(
                jid, [{"url": "https://youtu.be/a", "title": "A"}],
                user_id="local", options={"quality": "2160p"})
            return await _job_row(jid)
        row = asyncio.run(go())
        out = _json.loads(row.output_json)
        assert len(out["videos"]) == 1
        got = out["videos"][0]
        assert got["requested_quality"] == "2160p" and got["height"] == 1080
        assert got["ext"] == "mp4" and got["id"] in out["downloaded_ids"]

    def test_analyze_false_skips_the_analyzer(self, downloader, monkeypatch):
        """The tool page asked for files, not transcripts — Whisper over every
        download would be minutes nobody requested."""
        ran = []

        async def spy(self, *a, **k):
            ran.append(True)
        monkeypatch.setattr("backend.agents.analyzer.AnalyzerAgent.run", spy)

        async def go():
            jid = await _job()
            await TR.run_batch_download_urls(
                jid, [{"url": "https://youtu.be/a"}], user_id="local",
                analyze=False)
            return await _job_row(jid)
        row = asyncio.run(go())
        assert row.status == "success"
        assert ran == [], "analysis must not run when it wasn't asked for"

    def test_analyze_defaults_to_on_for_existing_callers(self, downloader,
                                                         monkeypatch):
        ran = []

        async def spy(self, *a, **k):
            ran.append(True)
        monkeypatch.setattr("backend.agents.analyzer.AnalyzerAgent.run", spy)
        self._run([{"url": "https://youtu.be/a"}])
        assert ran == [True]


class TestSingleDownloadToDb:
    def test_it_persists_a_downloaded_row(self, downloader, no_analyzer):
        async def go():
            jid = await _job()
            vid = await TR._download_single_video_to_db(
                jid, "https://youtu.be/single", "A title", "local")
            from backend.models.downloaded_video import DownloadedVideo
            async with AsyncSessionLocal() as db:
                return (await db.execute(select(DownloadedVideo).where(
                    DownloadedVideo.id == vid["id"]))).scalar_one()
        row = asyncio.run(go())
        assert row.video_path and row.duration_seconds == 120

    def test_the_platform_is_recorded_from_the_url(self, downloader, no_analyzer):
        async def go():
            jid = await _job()
            vid = await TR._download_single_video_to_db(
                jid, "https://www.tiktok.com/@x/video/9", "T", "local")
            from backend.models.downloaded_video import DownloadedVideo
            async with AsyncSessionLocal() as db:
                return (await db.execute(select(DownloadedVideo).where(
                    DownloadedVideo.id == vid["id"]))).scalar_one()
        assert asyncio.run(go()).platform == "tiktok"

    def test_a_download_failure_propagates_to_the_caller(
            self, downloader, no_analyzer):
        """The batch runner decides what a failure means; this helper must not
        silently swallow it."""
        downloader["outcomes"]["https://youtu.be/bad"] = RuntimeError("gone")

        async def go():
            jid = await _job()
            await TR._download_single_video_to_db(
                jid, "https://youtu.be/bad", "T", "local")
        with pytest.raises(Exception):
            asyncio.run(go())


class TestSingleUrlRunner:
    def test_a_good_url_succeeds(self, downloader, no_analyzer):
        async def go():
            jid = await _job("download_url")
            await TR.run_download_url(jid, "https://youtu.be/x", user_id="local")
            return await _job_row(jid)
        assert asyncio.run(go()).status == "success"

    def test_a_dead_url_fails_the_job_with_a_reason(self, downloader,
                                                    no_analyzer):
        downloader["outcomes"]["https://youtu.be/dead"] = RuntimeError(
            "Video unavailable")

        async def go():
            jid = await _job("download_url")
            await TR.run_download_url(jid, "https://youtu.be/dead",
                                      user_id="local")
            return await _job_row(jid)
        row = asyncio.run(go())
        assert row.status == "failed" and row.error_message


# ── analyzing an imported file ──────────────────────────────────────────────

class TestAnalyzeImported:
    @pytest.fixture()
    def imported(self, tmp_path):
        async def make():
            from backend.models.downloaded_video import DownloadedVideo
            f = tmp_path / "mine.mp4"
            f.write_bytes(b"\x00" * 4096)
            async with AsyncSessionLocal() as db:
                v = DownloadedVideo(user_id="local", title="Imported",
                                    platform="local", video_path=str(f),
                                    duration_seconds=60)
                db.add(v)
                await db.commit()
                return v.id
        return asyncio.run(make())

    def test_it_runs_the_analyzer_and_succeeds(self, imported, monkeypatch):
        ran = {}

        async def fake_run(self, *a, **k):
            ran["yes"] = True
        monkeypatch.setattr("backend.agents.analyzer.AnalyzerAgent.run", fake_run)

        async def go():
            jid = await _job("analyze")
            await TR.run_analyze_imported(jid, imported, user_id="local")
            return await _job_row(jid)
        row = asyncio.run(go())
        assert ran.get("yes") and row.status == "success"

    def test_an_unknown_video_fails_the_job(self, monkeypatch):
        async def go():
            jid = await _job("analyze")
            await TR.run_analyze_imported(jid, "no-such-id", user_id="local")
            return await _job_row(jid)
        assert asyncio.run(go()).status == "failed"

    def test_an_analyzer_crash_fails_the_job_rather_than_hanging_it(
            self, imported, monkeypatch):
        """A runner that returns without a terminal write leaves the job
        'running' forever and the UI spins."""
        async def boom(self, *a, **k):
            raise RuntimeError("whisper exploded")
        monkeypatch.setattr("backend.agents.analyzer.AnalyzerAgent.run", boom)

        async def go():
            jid = await _job("analyze")
            await TR.run_analyze_imported(jid, imported, user_id="local")
            return await _job_row(jid)
        row = asyncio.run(go())
        assert row.status == "failed" and "whisper exploded" in (row.error_message or "")
