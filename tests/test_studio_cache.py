# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Voice take details (peaks + word timings) and the Studio cache sweep."""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time

import pytest

from backend.services import studio_cache
from backend.services import voice_take_service as vts

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
DAY = 86400


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    from backend.config import Settings
    from backend.services import studio_media
    monkeypatch.setattr(vts, "cache_dir", lambda: tmp_path / "voice")
    monkeypatch.setattr(studio_media, "preview_dir", lambda: tmp_path / "preview")
    monkeypatch.setattr(Settings, "STUDIO_DIR", property(lambda self: tmp_path / "studio"))
    for d in ("voice", "preview", "studio/projects"):
        (tmp_path / d).mkdir(parents=True)
    vts._detail_locks.clear()
    return tmp_path


def write_take(dirs, key, *, created_at, audio=b"x"):
    (dirs / "voice" / f"{key}.mp3").write_bytes(audio)
    (dirs / "voice" / f"{key}.json").write_text(json.dumps({"key": key, "duration": 1.0, "created_at": created_at,
                                                             "text": "Hello there."}))


def key(s):
    return hashlib.sha256(s.encode()).hexdigest()


# ── details ─────────────────────────────────────────────────────────────────

@needs_ffmpeg
def test_peaks_and_words_are_computed_once_and_cached(dirs, monkeypatch):
    k = key("take")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(dirs / "voice" / f"{k}.mp3")],
                   check=True, capture_output=True)
    (dirs / "voice" / f"{k}.json").write_text(json.dumps({"key": k, "duration": 1.0, "text": "Hello there."}))
    calls = []

    class FakeWhisper:
        async def transcribe(self, path, timing_only=False, **_kw):
            calls.append(timing_only)
            return {"segments": [{"start": 0, "end": 1, "text": "hello there",
                                  "words": [{"word": " Hello", "start": 0.1, "end": 0.4},
                                            {"word": " there.", "start": 0.45, "end": 0.9}]}]}

    monkeypatch.setattr("backend.services.whisper_service.whisper_service", FakeWhisper())
    first = asyncio.run(vts.take_details(k))
    assert len(first["peaks"]) == vts.PEAK_COUNT and max(first["peaks"]) > 0.1
    assert [w["word"] for w in first["words"]] == ["Hello", "there."]
    again = asyncio.run(vts.take_details(k))
    assert again["words"] == first["words"] and calls == [True]
    assert "peaks" in json.loads((dirs / "voice" / f"{k}.json").read_text())


def test_words_timed_by_an_older_aligner_are_re_timed_once(dirs, monkeypatch):
    """A sidecar's words are stamped with WORDS_VERSION. Words cached before the
    Latin script alignment ("Did you know" captioned "Did you pay?") are re-timed
    once instead of showing the old mis-hearing on every render of that take."""
    k = key("old-words")
    write_take(dirs, k, created_at=time.time())
    meta_path = dirs / "voice" / f"{k}.json"
    old = {**json.loads(meta_path.read_text()), "text": "Did you know this?", "peaks": [0.5],
           "words": [{"word": "Did", "start": 0.0, "end": 0.2}, {"word": "you", "start": 0.2, "end": 0.4},
                     {"word": "pay?", "start": 0.4, "end": 0.8}]}
    meta_path.write_text(json.dumps(old))
    fresh = [{"word": "Did", "start": 0.0, "end": 0.2}, {"word": "you", "start": 0.2, "end": 0.4},
             {"word": "know", "start": 0.4, "end": 0.6}, {"word": "this?", "start": 0.6, "end": 0.8}]
    calls = []

    async def timings(path, text):
        calls.append(text)
        return fresh

    monkeypatch.setattr(vts, "_word_timings", timings)
    first = asyncio.run(vts.take_details(k))
    assert first["words"] == fresh and calls == ["Did you know this?"]
    assert json.loads(meta_path.read_text())["words_version"] == vts.WORDS_VERSION
    again = asyncio.run(vts.take_details(k))
    assert again["words"] == fresh and len(calls) == 1       # re-timed once, then cached


def test_a_failed_re_time_keeps_the_old_words(dirs, monkeypatch):
    k = key("old-words-2")
    write_take(dirs, k, created_at=time.time())
    meta_path = dirs / "voice" / f"{k}.json"
    stale = [{"word": "Hello", "start": 0.0, "end": 0.5}]
    meta_path.write_text(json.dumps({**json.loads(meta_path.read_text()), "peaks": [0.5], "words": stale}))

    async def broken(path, text):
        return None
    monkeypatch.setattr(vts, "_word_timings", broken)
    assert asyncio.run(vts.take_details(k))["words"] == stale
    assert "words_version" not in json.loads(meta_path.read_text())     # tried again next time


def test_whisper_failure_still_gives_peaks(dirs, monkeypatch):
    k = key("no-whisper")
    write_take(dirs, k, created_at=time.time())
    monkeypatch.setattr(vts, "_decode_peaks", lambda path, count=vts.PEAK_COUNT: [0.5] * 3)

    class Broken:
        async def transcribe(self, *_a, **_k):
            raise RuntimeError("model not downloaded")

    monkeypatch.setattr("backend.services.whisper_service.whisper_service", Broken())
    meta = asyncio.run(vts.take_details(k))
    assert meta["peaks"] == [0.5] * 3 and meta["words"] is None
    # The failure isn't persisted — the next request tries Whisper again.
    assert "words" not in json.loads((dirs / "voice" / f"{k}.json").read_text())


def test_details_for_unknown_keys_are_none(dirs):
    assert asyncio.run(vts.take_details("../../etc/passwd")) is None
    assert asyncio.run(vts.take_details(key("missing"))) is None


# ── sweep ───────────────────────────────────────────────────────────────────

def test_sweep_keeps_what_is_referenced_or_recent(dirs, monkeypatch):
    now = time.time()
    old, recent, referenced = key("old"), key("recent"), key("referenced")
    write_take(dirs, old, created_at=now - 40 * DAY)
    write_take(dirs, recent, created_at=now - 3 * DAY)
    write_take(dirs, referenced, created_at=now - 400 * DAY)

    stale_preview = dirs / "preview" / "pexels_1.mp4"
    fresh_preview = dirs / "preview" / "pexels_2.mp4"
    part = dirs / "voice" / f"{key('half')}.part.mp3"
    for f in (stale_preview, fresh_preview, part):
        f.write_bytes(b"x")
    os.utime(stale_preview, (now - 20 * DAY, now - 20 * DAY))
    os.utime(part, (now - 2 * DAY, now - 2 * DAY))

    live = dirs / "studio" / "projects" / "live-project"
    orphan = dirs / "studio" / "projects" / "deleted-project"
    for d in (live, orphan):
        (d / "assets").mkdir(parents=True)
        os.utime(d, (now - 3 * DAY, now - 3 * DAY))

    async def refs():
        return {referenced}, {"live-project"}

    async def no_live_renders():
        return set()

    monkeypatch.setattr(studio_cache, "referenced_take_keys_and_projects", refs)
    monkeypatch.setattr(studio_cache, "live_render_job_prefixes", no_live_renders)
    counts = asyncio.run(studio_cache.sweep(now=now))
    assert counts == {"voice_takes": 1, "previews": 1, "parts": 1, "orphan_projects": 1, "orphan_take_audio": 0,
                      "render_scratch": 0}
    assert not (dirs / "voice" / f"{old}.mp3").exists()
    assert vts.is_cached(recent) and vts.is_cached(referenced)
    assert fresh_preview.exists() and not stale_preview.exists()
    assert live.exists() and not orphan.exists()


def test_reference_set_reads_every_project_document():
    from backend.database import init_db
    from backend.services import studio_store as store

    asyncio.run(init_db())
    row = asyncio.run(store.create_project(script="A line to voice."))
    uid = json.loads(row.doc_json)["scenes"][0]["uid"]
    k = key("in-a-document")
    asyncio.run(store.write_ops(row.id, [
        {"op": "voice_result", "uid": uid, "voice": {"state": "ready", "take_key": k, "duration": 1.0}},
    ], if_revision=None, internal=True))
    keys, ids = asyncio.run(studio_cache.referenced_take_keys_and_projects())
    assert k in keys and row.id in ids


def test_sweep_removes_dead_render_scratch_and_orphan_take_audio(dirs, monkeypatch):
    """A render killed mid-way never reaches its own cleanup; a take whose
    metadata never landed can never be served. Neither may pile up — but a
    LIVE render's scratch and anything recent stay."""
    from backend.config import Settings
    monkeypatch.setattr(Settings, "TMP_DIR", property(lambda self: dirs / "tmp"))
    now = time.time()
    old = now - 10 * 3600
    (dirs / "tmp").mkdir()
    dead, live, fresh, other = (dirs / "tmp" / n for n in
                                ("studio_render_deadbeef", "studio_render_1ive0000", "studio_render_fresh000", "yt_cookies"))
    for folder in (dead, live, fresh, other):
        folder.mkdir()
        (folder / "x.mp4").write_bytes(b"x")
    for folder in (dead, live, other):
        os.utime(folder, (old, old))
    orphan, recent_orphan = key("orphan"), key("recent")
    (dirs / "voice" / f"{orphan}.mp3").write_bytes(b"x")
    os.utime(dirs / "voice" / f"{orphan}.mp3", (now - 2 * DAY, now - 2 * DAY))
    (dirs / "voice" / f"{recent_orphan}.mp3").write_bytes(b"x")

    counts = studio_cache._sweep_files(set(), set(), now, live_renders={"1ive0000"})
    assert counts["render_scratch"] == 1 and not dead.exists()
    assert live.exists() and fresh.exists() and other.exists()        # never a blanket TMP purge
    assert counts["orphan_take_audio"] == 1 and not (dirs / "voice" / f"{orphan}.mp3").exists()
    assert (dirs / "voice" / f"{recent_orphan}.mp3").exists()
    # Unsure which renders are live → no scratch folder is touched.
    os.utime(live, (old, old))
    assert studio_cache._sweep_files(set(), set(), now, live_renders=None)["render_scratch"] == 0 and live.exists()


def test_take_locks_are_not_kept_forever(dirs, monkeypatch):
    async def synth(text, spec, out):
        out.write_bytes(b"ID3")

    monkeypatch.setattr(vts, "probe_audio_duration", lambda _p: 1.0)
    vts._locks.clear()
    spec = vts.VoiceSpec(provider="edge_tts", voice="en-US-AndrewMultilingualNeural")
    take = asyncio.run(vts.ensure_take(spec, "Hello there.", synth=synth))
    assert vts._locks == {}
    assert take.key == vts.key_for_line(spec, "  Hello   there. ") and vts.is_cached(take.key)
