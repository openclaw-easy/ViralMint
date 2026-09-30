# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Follow-ups to the stuck-download fix: the same classes of bug on the
neighbouring paths. Each test below pins one of them:

  * the stall watchdog only saw progress hooks, so yt-dlp's FFmpegFD (which
    reports nothing until it finishes) and the hook-less audio extract looked
    "stalled" while healthy — and a download still QUEUED for a worker had
    its stall clock running;
  * "cancelled" + a late failure must keep both: the cancelled status AND the
    reason, on every download runner — and with no red job_failed toast;
  * a job cancelled while QUEUED still ran once it got a slot, and a
    cancelled job gave clients no way to know when its in-flight work ended;
  * a cancel could overwrite a job that had just finished.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.core.exceptions import DownloadStalledError
from backend.database import AsyncSessionLocal
from backend.models.job import Job


@pytest.fixture(scope="module", autouse=True)
def _tables():
    from backend.database import init_db
    asyncio.run(init_db())


async def _make_job(status: str) -> str:
    job_id = f"t-{uuid.uuid4().hex[:10]}"
    async with AsyncSessionLocal() as db:
        db.add(Job(id=job_id, job_type="download", status=status, user_id="local"))
        await db.commit()
    return job_id


async def _row(job_id: str) -> Job:
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()


class _NullWs:
    def __init__(self):
        self.sent = []

    async def send(self, msg, *a, **kw):
        self.sent.append(msg)

    async def send_progress(self, *a, **kw): pass
    async def send_constraint_warning(self, *a, **kw): pass


# ── 2. The watchdog: queued ≠ stalled, and disk growth is progress ───────────

def test_a_queued_attempt_has_no_stall_clock_until_admitted():
    from backend.services.ytdlp_service import _DownloadProgress
    p = _DownloadProgress()
    p.reset(queued=True)
    assert p.idle_seconds() is None, "time spent queueing for a worker was charged as a stall"
    p.mark_started()
    assert p.idle_seconds() is not None


def test_disk_footprint_changes_count_as_activity_but_the_baseline_does_not():
    from backend.services.ytdlp_service import _DownloadProgress
    p = _DownloadProgress()
    p._last_activity -= 100
    p.note_disk(1000)                      # first sample: baseline only
    assert p.idle_seconds() >= 99
    p.note_disk(1000)                      # unchanged: still idle
    assert p.idle_seconds() >= 99
    p.note_disk(5000)                      # grew: that is progress
    assert p.idle_seconds() < 5


def test_stem_footprint_covers_part_files_fragments_and_audio(tmp_path):
    from backend.services.ytdlp_service import _stem_footprint
    (tmp_path / "abc.mp4.part").write_bytes(b"x" * 10)
    (tmp_path / "abc.mp4.part-Frag3").write_bytes(b"x" * 20)
    (tmp_path / "abc_audio.mp3").write_bytes(b"x" * 5)
    (tmp_path / "other.mp4").write_bytes(b"x" * 999)
    assert _stem_footprint((tmp_path, "abc")) == 35
    # A template stem cannot be globbed and must not match everything.
    assert _stem_footprint((tmp_path, "%(id)s"), (tmp_path, None)) == 0


@pytest.mark.asyncio
async def test_hook_less_disk_growth_keeps_a_download_alive(tmp_path, monkeypatch):
    """FFmpegFD: no `downloading` hooks at all, but the .part file grows."""
    import backend.services.ytdlp_service as ys
    from backend.services.ytdlp_service import (
        _DownloadProgress, _await_with_stall_guard, _stem_footprint,
    )
    monkeypatch.setattr(ys, "_STALL_POLL_S", 0.05)
    monkeypatch.setattr(ys, "_DISK_POLL_S", 0.1)
    part = tmp_path / "vid.mp4.part"

    async def ffmpeg_like():
        for i in range(12):                 # ~1.2s of steady, hook-less growth
            part.write_bytes(b"x" * (i + 1) * 100)
            await asyncio.sleep(0.1)
        return "done"

    tracker = _DownloadProgress()
    got = await _await_with_stall_guard(
        ffmpeg_like(), tracker, hard_timeout=30, stall_timeout=0.5, url="u",
        footprint=lambda: _stem_footprint((tmp_path, "vid")),
    )
    assert got == "done"


@pytest.mark.asyncio
async def test_a_file_that_stops_growing_still_stalls(tmp_path, monkeypatch):
    import backend.services.ytdlp_service as ys
    from backend.services.ytdlp_service import (
        _DownloadProgress, _await_with_stall_guard, _stem_footprint,
    )
    monkeypatch.setattr(ys, "_STALL_POLL_S", 0.05)
    monkeypatch.setattr(ys, "_DISK_POLL_S", 0.1)
    (tmp_path / "vid.mp4.part").write_bytes(b"x" * 100)

    async def wedged():
        await asyncio.sleep(30)

    with pytest.raises(DownloadStalledError) as exc:
        await _await_with_stall_guard(
            wedged(), _DownloadProgress(), hard_timeout=30, stall_timeout=0.5,
            url="u", footprint=lambda: _stem_footprint((tmp_path, "vid")),
        )
    assert "0 minutes" not in str(exc.value), "a sub-minute stall rendered as '0 minutes'"


@pytest.mark.asyncio
async def test_pool_calls_on_admit_once_a_slot_is_reserved():
    from backend.core.executors import download_pool
    seen = []
    result = await download_pool.run(lambda: seen.append("work") or 7,
                                     on_admit=lambda: seen.append("admit"))
    assert result == 7
    assert seen == ["admit", "work"]


# ── 3. Cancelled + failed keeps both, on every download runner ──────────────

@pytest.mark.asyncio
async def test_update_job_status_keeps_cancelled_and_records_a_late_failure():
    from backend.agents.job_helper import update_job_status
    job_id = await _make_job("running")
    await update_job_status(job_id, "cancelled")
    await update_job_status(job_id, "failed", error_message="HTTP 403")
    row = await _row(job_id)
    assert row.status == "cancelled"
    assert row.error_message == "HTTP 403"


@pytest.mark.asyncio
async def test_single_url_download_failing_after_a_cancel_is_not_a_red_failure(monkeypatch):
    from backend.core import task_runner as dl
    import backend.core.ws_manager as wsm
    import backend.services.ytdlp_service as ys

    ws = _NullWs()
    monkeypatch.setattr(wsm, "ws_manager", ws)
    monkeypatch.setattr(ys, "is_channel_or_playlist_url", lambda url: False)

    job_id = await _make_job("pending")

    async def cancel_then_fail(jid, url, title, user_id):
        from backend.agents.job_helper import cancel_if_live
        await cancel_if_live(jid)                        # the user cancels…
        raise RuntimeError("HTTP Error 403: Forbidden")  # …then the transfer dies
    monkeypatch.setattr(dl, "_download_single_url", cancel_then_fail)

    await dl.run_download_url(job_id, "https://youtube.com/watch?v=x")
    row = await _row(job_id)
    assert row.status == "cancelled"
    assert "403" in (row.error_message or "")
    assert not [m for m in ws.sent if m.get("type") == "job_failed"], \
        "a red job_failed toast for a job the user cancelled"


def _stub_analyzer(monkeypatch):
    import backend.agents.analyzer as an

    async def _run(self, *a, **kw):
        return None
    monkeypatch.setattr(an.AnalyzerAgent, "run", _run)


@pytest.mark.asyncio
async def test_channel_download_reports_partial_failures(monkeypatch):
    """Per-video failures used to be logged and dropped."""
    from backend.core import task_runner as dl
    import backend.core.ws_manager as wsm
    import backend.services.ytdlp_service as ys
    import backend.core.http_utils as hu

    monkeypatch.setattr(wsm, "ws_manager", _NullWs())
    monkeypatch.setattr(hu, "jittered_delay", lambda: 0)
    _stub_analyzer(monkeypatch)

    async def listing(url, max_videos=5):
        return [{"url": f"https://y/{i}", "title": f"v{i}"} for i in range(3)]
    monkeypatch.setattr(ys, "list_channel_videos", listing)

    async def one_fails(jid, url, title, user_id, options=None):
        if url.endswith("/1"):
            raise RuntimeError("HTTP Error 403: Forbidden")
        return {"id": f"dv-{url[-1]}"}
    monkeypatch.setattr(dl, "_download_single_video_to_db", one_fails)

    job_id = await _make_job("running")
    await dl._download_channel(job_id, "https://y/channel", "local")
    row = await _row(job_id)
    assert row.status == "success"
    assert "Video 2/3" in (row.error_message or ""), row.error_message
    assert "403" in row.error_message


@pytest.mark.asyncio
async def test_channel_download_where_everything_failed_says_why(monkeypatch):
    from backend.core import task_runner as dl
    import backend.core.ws_manager as wsm
    import backend.services.ytdlp_service as ys
    import backend.core.http_utils as hu

    monkeypatch.setattr(wsm, "ws_manager", _NullWs())
    monkeypatch.setattr(hu, "jittered_delay", lambda: 0)

    async def listing(url, max_videos=5):
        return [{"url": "https://y/0", "title": "v0"}]
    monkeypatch.setattr(ys, "list_channel_videos", listing)

    async def fails(jid, url, title, user_id, options=None):
        raise RuntimeError("HTTP Error 403: Forbidden")
    monkeypatch.setattr(dl, "_download_single_video_to_db", fails)

    job_id = await _make_job("running")
    with pytest.raises(Exception) as exc:
        await dl._download_channel(job_id, "https://y/channel", "local")
    assert "403" in str(exc.value), "the reason was dropped: " + str(exc.value)


@pytest.mark.asyncio
async def test_channel_download_cancelled_and_failed_keeps_the_reason(monkeypatch):
    from backend.core import task_runner as dl
    import backend.core.ws_manager as wsm
    import backend.services.ytdlp_service as ys

    monkeypatch.setattr(wsm, "ws_manager", _NullWs())

    async def listing(url, max_videos=5):
        return [{"url": "https://y/0", "title": "v0"}]
    monkeypatch.setattr(ys, "list_channel_videos", listing)

    async def cancel_then_fail(jid, url, title, user_id, options=None):
        from backend.agents.job_helper import cancel_if_live
        await cancel_if_live(jid)
        raise RuntimeError("HTTP Error 403: Forbidden")
    monkeypatch.setattr(dl, "_download_single_video_to_db", cancel_then_fail)

    job_id = await _make_job("running")
    await dl._download_channel(job_id, "https://y/channel", "local")
    row = await _row(job_id)
    assert row.status == "cancelled"
    assert "403" in (row.error_message or "")


@pytest.mark.asyncio
async def test_scout_download_cancelled_mid_batch_stops_and_says_so(monkeypatch):
    """DownloadAgent never polled for a cancel: it pulled every video, sent
    job_complete, and run_download then started the analysis pass."""
    import backend.agents.downloader as downloader
    import backend.core.http_utils as hu
    from backend.agents.job_helper import cancel_if_live
    from backend.models.scout_result import ScoutResult

    ws = _NullWs()
    monkeypatch.setattr(downloader, "ws_manager", ws)
    monkeypatch.setattr(hu, "jittered_delay", lambda: 0)
    monkeypatch.setattr(downloader, "jittered_delay", lambda: 0, raising=False)

    job_id = await _make_job("running")
    sr_ids = []
    async with AsyncSessionLocal() as db:
        for i in range(3):
            sr = ScoutResult(user_id="local", platform="youtube", video_id=f"v{i}-{job_id}",
                             video_url=f"https://youtube.com/watch?v={i}", title=f"t{i}")
            db.add(sr)
            await db.flush()
            sr_ids.append(sr.id)
        await db.commit()

    pulled = []

    async def fake_download(url, output_dir, filename, extract_audio):
        pulled.append(url)
        await cancel_if_live(job_id)        # the user cancels during video 1
        raise RuntimeError("HTTP Error 403: Forbidden")
    monkeypatch.setattr(downloader, "download_video", fake_download)

    async def no_fix(*a, **kw):
        return None
    import backend.core.ai_retry as ai_retry
    monkeypatch.setattr(ai_retry, "ai_fix_url", no_fix)

    await downloader.DownloadAgent().run(job_id=job_id, scout_result_ids=sr_ids)

    assert len(pulled) == 1, f"kept downloading after the cancel: {pulled}"
    row = await _row(job_id)
    assert row.status == "cancelled"
    assert "403" in (row.error_message or "")
    types = [m.get("type") for m in ws.sent]
    assert "job_complete" not in types and "job_failed" not in types, types


# ── 4. Queued cancels never run; draining runners are visible ───────────────

@pytest.mark.asyncio
async def test_a_job_cancelled_while_queued_never_runs():
    from backend.core.task_runner import _run_with_limit
    from backend.agents.job_helper import cancel_if_live

    ran = []

    async def runner(job_id: str):
        ran.append(job_id)

    job_id = await _make_job("pending")
    assert await cancel_if_live(job_id)
    await _run_with_limit(runner(job_id))
    assert ran == [], "the cancel API said 'cancelled before it started' — it started anyway"


@pytest.mark.asyncio
async def test_runner_active_tracks_the_runner_not_the_status():
    from backend.core.task_runner import _run_with_limit, runner_active
    from backend.agents.job_helper import cancel_if_live

    gate = asyncio.Event()
    seen = {}

    async def runner(job_id: str):
        seen["during"] = runner_active(job_id)
        await gate.wait()

    job_id = await _make_job("running")
    task = asyncio.create_task(_run_with_limit(runner(job_id)))
    await asyncio.sleep(0.05)
    assert seen["during"] is True
    await cancel_if_live(job_id)
    assert runner_active(job_id), "a cancel does not stop in-flight work"
    gate.set()
    await task
    assert not runner_active(job_id)


@pytest_asyncio.fixture
async def api():
    from backend.main import create_app
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:16888",
        headers={"Origin": "http://127.0.0.1:16888"},
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_a_draining_cancelled_job_cannot_be_deleted_yet(api, monkeypatch):
    import backend.api.jobs as jobs_api
    job_id = await _make_job("cancelled")
    monkeypatch.setattr(jobs_api, "runner_active", lambda jid: jid == job_id)

    r = await api.delete(f"/api/jobs/{job_id}")
    assert r.status_code == 409
    assert (await _row(job_id)).status == "cancelled"

    r = await api.get(f"/api/jobs/{job_id}")
    assert r.json()["runner_active"] is True

    r = await api.post("/api/jobs/bulk-delete", json={"job_ids": [job_id]})
    assert r.json()["kept_draining"] == 1
    assert (await _row(job_id)).status == "cancelled"


# ── 5. A cancel never overwrites a job that already finished ─────────────────

@pytest.mark.asyncio
async def test_cancel_if_live_leaves_a_finished_job_alone():
    from backend.agents.job_helper import cancel_if_live
    job_id = await _make_job("success")
    assert await cancel_if_live(job_id) is False
    assert (await _row(job_id)).status == "success"


@pytest.mark.asyncio
async def test_update_job_status_loses_a_race_gracefully(monkeypatch):
    """The status write is a compare-and-swap: when another writer changed
    the row between our read and our write, the guards re-run on the NEW
    status instead of blindly overwriting it."""
    import backend.agents.job_helper as jh
    from sqlalchemy import update

    job_id = await _make_job("running")
    real_session = jh.AsyncSessionLocal
    raced = {"done": False}

    class _RacingSession:
        """First session: after our SELECT, a cancel commits elsewhere."""
        def __init__(self):
            self._s = real_session()

        async def __aenter__(self):
            s = await self._s.__aenter__()
            orig_execute = s.execute

            async def execute(stmt, *a, **kw):
                res = await orig_execute(stmt, *a, **kw)
                if not raced["done"] and stmt.is_select:
                    raced["done"] = True
                    async with real_session() as other:
                        await other.execute(update(Job).where(Job.id == job_id)
                                            .values(status="cancelled"))
                        await other.commit()
                return res
            s.execute = execute
            return s

        async def __aexit__(self, *exc):
            return await self._s.__aexit__(*exc)

    monkeypatch.setattr(jh, "AsyncSessionLocal", _RacingSession)
    await jh.update_job_status(job_id, "success", current_step="done")
    monkeypatch.setattr(jh, "AsyncSessionLocal", real_session)

    assert raced["done"]
    assert (await _row(job_id)).status == "cancelled", \
        "a runner's success overwrote a cancel that committed mid-write"
