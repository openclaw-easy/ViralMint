# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Propose-don't-cut: /suggest-clips, and the two extractor bugs under it.

The endpoint runs the same AI window selection as mode="ai" extraction and
stops before a frame is encoded. What matters here is that the proposals come
back in a shape the bench can draw, that a bad window cannot fail the whole
search, and that two jobs on one source don't each pay for Whisper.
"""
import asyncio
import json

import pytest

from backend.api import downloaded as dl
from backend.services import clip_extractor as ce

# Imported here on purpose. `job_helper` binds `AsyncSessionLocal` at MODULE
# level, so if its first import happened inside a test that had patched
# `backend.database.AsyncSessionLocal`, it would hold the fake for the rest of
# the session — and the next test to create a real job would fail somewhere
# unrelated. Bind it against the real thing first.
from backend.agents import job_helper  # noqa: F401


# ── _remove_overlapping_clips ─────────────────────────────────────────────

def test_overlap_filter_drops_an_unusable_window_instead_of_raising():
    """Every window here came from an LLM. A null bound used to be a
    TypeError that failed the ENTIRE search rather than dropping one pick."""
    windows = [
        {"start": 0.0, "end": 10.0, "title": "good"},
        {"start": None, "end": 30.0, "title": "no start"},
        {"end": 50.0, "title": "no start key"},
        {"start": "not a number", "end": 60.0},
        {"start": 40.0, "end": None},
        {"start": 100.0, "end": 110.0, "title": "also good"},
    ]
    kept = ce._remove_overlapping_clips(windows)
    assert [w.get("title") for w in kept] == ["good", "also good"]


def test_overlap_filter_still_removes_overlaps():
    """The guard must not switch the filter off — it only skips what it
    cannot compare."""
    windows = [
        {"start": 0.0, "end": 30.0, "title": "first"},
        {"start": 10.0, "end": 40.0, "title": "overlaps"},
        {"start": 60.0, "end": 90.0, "title": "clear"},
    ]
    kept = ce._remove_overlapping_clips(windows)
    assert [w["title"] for w in kept] == ["first", "clear"]


def test_overlap_filter_accepts_numeric_strings():
    """Coercion, not rejection: "12" is a usable bound and dropping it would
    silently lose a clip the model meant."""
    kept = ce._remove_overlapping_clips([{"start": "0", "end": "12"}])
    assert len(kept) == 1


def test_overlap_filter_survives_a_non_dict():
    assert ce._remove_overlapping_clips(["nope", {"start": 0, "end": 5}]) == [
        {"start": 0, "end": 5}]


# ── auto_clip_count ───────────────────────────────────────────────────────

@pytest.mark.parametrize("duration,expected", [
    (None, 5),      # unknown length → the flat fallback, not "0 clips"
    (0, 5),
    (-1, 5),
    (30, 3),        # floor: fewer than 3 is not worth a job
    (60, 3),
    (120, 4),
    (1291, 43),     # the 21-minute source that started this
    (99999, 99),    # ceiling
])
def test_auto_clip_count(duration, expected):
    assert dl.auto_clip_count(duration) == expected


def test_auto_clip_count_always_returns_an_int():
    """A float count reaches the UI as "28.0 clips"."""
    got = dl.auto_clip_count(840.0)
    assert isinstance(got, int) and got == 28


# ── The transcription lock ────────────────────────────────────────────────

def test_two_jobs_on_one_source_never_transcribe_at_the_same_time():
    """The lock half. Without it, two clip jobs started seconds apart on one
    source both saw a NULL snapshot and both queued for Whisper."""
    overlap = {"max": 0, "now": 0}

    class _Row:
        id = "src-lock"
        transcript_segments_json = None

    async def fake_transcribe(*a, **k):
        overlap["now"] += 1
        overlap["max"] = max(overlap["max"], overlap["now"])
        await asyncio.sleep(0.01)
        overlap["now"] -= 1
        return [{"start": 0, "end": 1, "text": "hi"}]

    async def run():
        orig = ce._transcribe_locked
        ce._transcribe_locked = fake_transcribe
        try:
            await asyncio.gather(
                ce._load_or_transcribe_segments(_Row(), None),
                ce._load_or_transcribe_segments(_Row(), None),
            )
        finally:
            ce._transcribe_locked = orig

    ce._TRANSCRIBE_LOCKS.clear()
    asyncio.run(run())
    assert overlap["max"] == 1


def test_the_second_job_reads_the_transcript_the_first_one_wrote():
    """The re-check half, and the actual saving. `video` is a row snapshot
    taken when the job started, so after waiting out the first job's Whisper
    run the second one's copy STILL says "no transcript" — and it would
    transcribe the same audio again, starting a second after the first
    finished writing exactly what it needed."""
    class _Row:
        id = "src-recheck"
        transcript_segments_json = None
        audio_path = None
        video_path = "/tmp/nope.mp4"
        duration_seconds = 600

    async def fake_recheck(video_id):
        return [{"start": 0, "end": 1, "text": "written by the other job"}]

    async def run():
        orig = ce._cached_segments_from_db
        ce._cached_segments_from_db = fake_recheck
        try:
            # No whisper stub: reaching Whisper at all is the failure.
            return await ce._transcribe_locked(
                _Row(), None, "balanced", False, None, "local")
        finally:
            ce._cached_segments_from_db = orig

    got = asyncio.run(run())
    assert got == [{"start": 0, "end": 1, "text": "written by the other job"}]


def test_a_cached_snapshot_never_takes_the_lock():
    """The common path must not pay for serialization."""
    class _Row:
        id = "src-cached"
        transcript_segments_json = json.dumps([{"start": 0, "end": 1, "text": "x"}])

    ce._TRANSCRIBE_LOCKS.clear()
    got = asyncio.run(ce._load_or_transcribe_segments(_Row(), None))
    assert got == [{"start": 0, "end": 1, "text": "x"}]
    assert ce._TRANSCRIBE_LOCKS == {}


def test_force_retranscribe_still_bypasses_the_cache():
    class _Row:
        id = "src-force"
        transcript_segments_json = json.dumps([{"start": 0, "end": 1, "text": "old"}])

    async def fake_transcribe(*a, **k):
        return [{"start": 0, "end": 2, "text": "new"}]

    async def run():
        orig = ce._transcribe_locked
        ce._transcribe_locked = fake_transcribe
        try:
            return await ce._load_or_transcribe_segments(_Row(), None, force_retranscribe=True)
        finally:
            ce._transcribe_locked = orig

    assert asyncio.run(run()) == [{"start": 0, "end": 2, "text": "new"}]


def test_a_corrupt_snapshot_is_treated_as_missing_not_fatal():
    class _Row:
        id = "src-corrupt"
        transcript_segments_json = "{not json"

    async def fake_transcribe(*a, **k):
        return [{"start": 0, "end": 1, "text": "fresh"}]

    async def run():
        orig = ce._transcribe_locked
        ce._transcribe_locked = fake_transcribe
        try:
            return await ce._load_or_transcribe_segments(_Row(), None)
        finally:
            ce._transcribe_locked = orig

    assert asyncio.run(run())[0]["text"] == "fresh"


def test_the_lock_table_does_not_grow_per_source():
    """One lock per source ever transcribed is a leak in a long session."""
    class _Row:
        transcript_segments_json = None
        def __init__(self, i):
            self.id = f"src-{i}"

    async def fake_transcribe(*a, **k):
        return [{"start": 0, "end": 1, "text": "x"}]

    async def run():
        orig = ce._transcribe_locked
        ce._transcribe_locked = fake_transcribe
        try:
            for i in range(5):
                await ce._load_or_transcribe_segments(_Row(i), None)
        finally:
            ce._transcribe_locked = orig

    ce._TRANSCRIBE_LOCKS.clear()
    asyncio.run(run())
    assert ce._TRANSCRIBE_LOCKS == {}


# ── The runner ────────────────────────────────────────────────────────────

def _run_suggest(monkeypatch, windows, max_clips=5):
    """Drive run_suggest_clips with the AI selection stubbed, capturing the
    terminal job update."""
    from backend.core import task_runner as tr
    from backend.services.clip_options import ExtractOptions

    captured = {}

    class _Row:
        id = "vid-1"
        title = "A podcast"
        duration_seconds = 900

    class _Result:
        def __init__(self, v): self._v = v
        def scalar_one_or_none(self): return self._v

    class _DB:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *a, **k): return _Result(_Row())

    async def fake_update(job_id, status, **kw):
        if status in ("success", "failed"):
            captured["status"] = status
            captured.update(kw)

    async def fake_segments(*a, **k):
        return [{"start": 0, "end": 5, "text": "words"}]

    async def fake_select(*a, **k):
        return windows

    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr("backend.agents.job_helper.update_job_status", fake_update)
    monkeypatch.setattr(ce, "_load_or_transcribe_segments", fake_segments)
    monkeypatch.setattr(ce, "_select_clip_windows_with_retries", fake_select)

    asyncio.run(tr.run_suggest_clips(
        job_id="job-1234-abcd", downloaded_video_id="vid-1",
        opts=ExtractOptions(mode="ai", max_clips=max_clips)))
    return captured


def test_suggestions_come_back_in_the_shape_the_bench_draws(monkeypatch):
    out = _run_suggest(monkeypatch, [
        {"start": 120.0, "end": 150.0, "title": "The bit about pricing",
         "virality_score": 8.5, "hook_score": 9.0, "hook_type": "contrarian",
         "reason": "a clean standalone claim"},
        {"start": 10.0, "end": 40.0, "title": "Opening", "virality_score": 7.0},
    ])
    assert out["status"] == "success"
    sugg = out["output_data"]["suggestions"]
    # Chronological, not score order: the numbers on the timeline are the
    # numbers on the output clips.
    assert [s["start"] for s in sugg] == [10.0, 120.0]
    assert sugg[1] == {
        "start": 120.0, "end": 150.0, "title": "The bit about pricing",
        "score": 8.5, "hook_score": 9.0, "hook_type": "contrarian",
        "reason": "a clean standalone claim",
    }
    assert out["output_data"]["type"] == "clip_suggestions"


def test_the_runner_sanitizes_llm_bounds_before_anything_compares_them(monkeypatch):
    """A NaN bound would reach the bench as a block of undefined width."""
    out = _run_suggest(monkeypatch, [
        {"start": None, "end": 30.0},
        {"start": 10.0, "end": 10.0},        # zero length
        {"start": -5.0, "end": 20.0},        # negative start
        {"start": 60.0, "end": 90.0, "title": "the only real one"},
    ])
    sugg = out["output_data"]["suggestions"]
    assert len(sugg) == 1 and sugg[0]["title"] == "the only real one"


def test_the_runner_honours_max_clips(monkeypatch):
    out = _run_suggest(monkeypatch, [
        {"start": i * 100.0, "end": i * 100.0 + 30.0} for i in range(8)
    ], max_clips=3)
    assert len(out["output_data"]["suggestions"]) == 3


def test_a_search_that_finds_nothing_fails_with_a_next_step(monkeypatch):
    out = _run_suggest(monkeypatch, [])
    assert out["status"] == "failed"
    assert "drag your own" in out["error_message"]


def test_a_silent_source_says_so_rather_than_proposing_noise(monkeypatch):
    from backend.core import task_runner as tr
    from backend.services.clip_options import ExtractOptions

    captured = {}

    class _Result:
        def scalar_one_or_none(self):
            class _Row:
                id = "vid-1"
                title = "silence"
                duration_seconds = 60
            return _Row()

    class _DB:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *a, **k): return _Result()

    async def fake_update(job_id, status, **kw):
        if status in ("success", "failed"):
            captured["status"] = status
            captured.update(kw)

    async def no_segments(*a, **k):
        return []

    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr("backend.agents.job_helper.update_job_status", fake_update)
    monkeypatch.setattr(ce, "_load_or_transcribe_segments", no_segments)

    asyncio.run(tr.run_suggest_clips(
        job_id="job-1", downloaded_video_id="vid-1",
        opts=ExtractOptions(mode="ai", max_clips=5)))
    assert captured["status"] == "failed"
    assert "no speech" in captured["error_message"]


# ── The endpoint ──────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from starlette.testclient import TestClient

    from backend.main import app
    with TestClient(app) as c:
        yield c


async def _seed(video_path, duration):
    from backend.database import AsyncSessionLocal
    from backend.models.downloaded_video import DownloadedVideo
    async with AsyncSessionLocal() as db:
        v = DownloadedVideo(user_id="local", title="src", platform="youtube",
                            video_path=str(video_path), duration_seconds=duration)
        db.add(v)
        await db.commit()
        return v.id


def test_suggest_caps_proposals_at_the_manual_range_limit(client, tmp_path, monkeypatch):
    """Every proposal is cut back through manual mode, so a proposal the bench
    cannot submit is worse than one it never made. A 90-minute source's
    auto count is 99; the endpoint must ask for 10."""
    src = tmp_path / "long.mp4"
    src.write_bytes(b"x")
    vid = asyncio.run(_seed(src, 5400))

    asked = {}
    from backend.core import task_runner as tr
    monkeypatch.setattr(tr, "dispatch", lambda coro: None)
    monkeypatch.setattr(tr, "run_suggest_clips",
                        lambda **kw: asked.setdefault("max_clips", kw["opts"].max_clips))

    r = client.post(f"/api/downloaded/{vid}/suggest-clips", json={})
    assert r.status_code == 200, r.text
    assert asked["max_clips"] == dl._MANUAL_MAX_RANGES


def test_suggest_clamps_an_over_large_request(client, tmp_path, monkeypatch):
    src = tmp_path / "long.mp4"
    src.write_bytes(b"x")
    vid = asyncio.run(_seed(src, 600))

    asked = {}
    from backend.core import task_runner as tr
    monkeypatch.setattr(tr, "dispatch", lambda coro: None)
    monkeypatch.setattr(tr, "run_suggest_clips",
                        lambda **kw: asked.setdefault("max_clips", kw["opts"].max_clips))

    client.post(f"/api/downloaded/{vid}/suggest-clips", json={"max_clips": 500})
    assert asked["max_clips"] == dl._MANUAL_MAX_RANGES


def test_suggest_404s_for_an_unknown_source(client):
    assert client.post("/api/downloaded/nope/suggest-clips", json={}).status_code == 404


def test_suggest_400s_when_the_file_is_gone(client, tmp_path):
    vid = asyncio.run(_seed(tmp_path / "missing.mp4", 600))
    r = client.post(f"/api/downloaded/{vid}/suggest-clips", json={})
    assert r.status_code == 400


def test_suggest_rejects_an_inverted_duration_range(client, tmp_path):
    src = tmp_path / "x.mp4"
    src.write_bytes(b"x")
    vid = asyncio.run(_seed(src, 600))
    r = client.post(f"/api/downloaded/{vid}/suggest-clips",
                    json={"min_duration": 90, "max_duration": 30})
    assert r.status_code == 400


def test_suggest_normalises_through_the_shared_builder(client, tmp_path, monkeypatch):
    """suggest-clips reads the AI-targeting knobs exactly like extract-clips:
    same clamps, same vocabulary, through one builder (test_clip_options_builder)."""
    src = tmp_path / "long.mp4"
    src.write_bytes(b"x")
    vid = asyncio.run(_seed(src, 600))

    asked = {}
    from backend.core import task_runner as tr
    monkeypatch.setattr(tr, "dispatch", lambda coro: None)
    monkeypatch.setattr(tr, "run_suggest_clips", lambda **kw: asked.setdefault("opts", kw["opts"]))

    r = client.post(f"/api/downloaded/{vid}/suggest-clips", json={
        "min_duration": 5, "max_duration": 8, "target_platform": " TikTok ",
        "genre": " Podcast ", "user_query": "  pricing  ", "whisper_quality": "ULTRA",
    })
    assert r.status_code == 200, r.text
    o = asked["opts"]
    assert (o.mode, o.min_duration, o.max_duration) == ("ai", 10, 15)
    assert (o.target_platform, o.genre, o.user_query) == ("tiktok", "podcast", "pricing")
    assert o.whisper_quality == "balanced"        # unknown quality -> balanced
    assert o.max_clips >= 1 and o.time_ranges is None


def test_suggest_400s_on_inverted_bounds(client, tmp_path):
    src = tmp_path / "long.mp4"
    src.write_bytes(b"x")
    vid = asyncio.run(_seed(src, 600))
    r = client.post(f"/api/downloaded/{vid}/suggest-clips", json={"min_duration": 40, "max_duration": 20})
    assert r.status_code == 400
