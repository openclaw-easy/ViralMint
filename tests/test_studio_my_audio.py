# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Video Studio "My audio": a recording becomes an editable project that speaks in it.

The door (POST /api/studio/projects/from-audio), the listening job (run_studio_audio), the editing
rules, the checks and the render. Whisper is stubbed (a transcript with word timings); ffmpeg, the
take cache, the project store and the render are real."""
import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.core import task_runner
from backend.core.tasks import studio as tasks
from backend.services import own_audio as oa
from backend.services import studio_audio
from backend.services import studio_doc as sd
from backend.services import studio_store as store
from backend.services import voice_take_service as vts
from backend.services.video_utils import probe_duration
from tests.test_studio_render import client, env, ff, generated_for, media, run, start_render  # noqa: F401 — fixtures

S1, S2 = "Cats sleep for most of the whole long day.", "Then they wake up and eat their dinner."


def _w(text, t0):
    return [{"word": f" {w}", "start": round(t0 + 0.1 * i, 2), "end": round(t0 + 0.1 * i + 0.08, 2)}
            for i, w in enumerate(text.split())]


HEARD = {"language": "en", "segments": [{"words": _w(S1, 0.2) + _w(S2, 3.0)}]}


def _recording(tmp_path, seconds=6, name="morning walk.wav", rate=None) -> Path:
    p = tmp_path / name
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
                    *(["-ar", str(rate)] if rate else []), str(p)], check=True, timeout=120)
    return p


def _post(client, path: Path, **form):
    with path.open("rb") as f:
        return client.post("/api/studio/projects/from-audio", files={"file": (path.name, f, "audio/wav")}, data=form)


def _get(client, pid):
    return client.get(f"/api/studio/projects/{pid}").json()["project"]


def _job(job_id):
    from backend.database import AsyncSessionLocal
    from backend.models.job import Job

    async def go():
        async with AsyncSessionLocal() as db:
            return await db.get(Job, job_id)
    return asyncio.run(go())


def _count() -> int:
    return len(asyncio.run(store.list_projects(limit=500)))


async def _fake_plan(project_id, user_id="local", **_k):
    """The planner's result without the network: real stock bound to every scene."""
    doc = json.loads((await store.get_project(project_id, user_id)).doc_json)
    ops = [{"op": "plan_result", "uid": sc["uid"], "expect_line": sc["line"], "visual": {
        "kind": "stock", "state": "bound", "query": "cat nap", "fit": "loop", "pinned": False,
        "reason": None, "alternates": [],
        "asset": {"provider": "pexels", "provider_id": str(300 + i),
                  "download_url": f"https://videos.pexels.com/video-files/{300 + i}/hd.mp4",
                  "license": "Pexels License", "creator": "C"}}} for i, sc in enumerate(doc["scenes"])]
    await store.write_ops(project_id, ops, if_revision=None, internal=True, user_id=user_id)
    return {"planned": len(ops), "warnings": [], "voice_job_id": None}


def _listen(env, transcript=HEARD, *, whisper=None, plan=_fake_plan):
    """Run the dispatched listening job with Whisper stubbed; returns the transcribe mock."""
    from backend.services.whisper_service import whisper_service
    mock = whisper or AsyncMock(return_value=transcript)
    with patch.object(whisper_service, "transcribe", mock), \
            patch.object(whisper_service, "ensure_model", AsyncMock(return_value=None)), \
            patch.object(whisper_service, "is_model_cached", lambda *_a, **_k: True), \
            patch.object(tasks, "plan_into_project", plan):
        run(env)
    return mock


def _started(client, env, tmp_path, **kw):
    r = _post(client, _recording(tmp_path, **kw))
    assert r.status_code == 200, r.text
    return r.json()


# ── the door ────────────────────────────────────────────────────────────────

def test_the_refusals_name_the_shared_limits():
    assert str(int(sd.AUDIO_MIN_S)) in studio_audio.TOO_SHORT and str(int(sd.AUDIO_MAX_S // 60)) in studio_audio.TOO_LONG
    assert str(studio_audio.MAX_BYTES // (1024 * 1024)) in studio_audio.TOO_LARGE
    assert vts.OWN_AUDIO_PROVIDER == sd.OWN_AUDIO


@pytest.mark.parametrize("seconds,rate,code", [(2, None, "too_short"), (481, 8000, "too_long")])
def test_a_length_outside_the_limits_is_refused_before_anything_is_made(client, env, tmp_path, seconds, rate, code):
    before = _count()
    r = _post(client, _recording(tmp_path, seconds=seconds, rate=rate))
    assert r.status_code == 400 and r.json()["detail"]["code"] == code and r.json()["detail"]["message"]
    assert _count() == before and not env["runners"]


def test_a_file_over_the_size_limit_is_refused_while_it_uploads(client, env, tmp_path, monkeypatch):
    monkeypatch.setattr(studio_audio, "MAX_BYTES", 20_000)
    before = _count()
    r = _post(client, _recording(tmp_path))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "too_large"
    assert _count() == before and not env["runners"]


def test_a_file_that_is_not_audio_is_refused(client, env, tmp_path):
    text = tmp_path / "notes.txt"
    text.write_text("hello")
    assert _post(client, text).json()["detail"]["code"] == "media"
    noise = tmp_path / "broken.mp3"
    noise.write_bytes(b"not an mp3 at all" * 50)
    r = _post(client, noise)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "no_audio"
    silent = tmp_path / "silent.mp4"
    ff("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "4", "-pix_fmt", "yuv420p", str(silent))
    assert _post(client, silent).json()["detail"]["code"] == "no_audio"
    assert not env["runners"]


def test_the_door_makes_an_empty_audio_project_and_starts_listening(client, env, tmp_path):
    body = _started(client, env, tmp_path)
    p = body["project"]
    assert p["doc"]["input"]["mode"] == "audio" and p["doc"]["scenes"] == [] and p["title"] == "morning walk"
    audio = p["doc"]["input"]["audio"]
    assert sd.is_audio_id(audio["audio_id"]) and 5.9 < audio["duration"] < 6.1
    assert p["derived"]["audio_listening"] is True and p["audio_job_id"] == body["job_id"] and p["audio_failed"] is None
    assert p["checks"]["blockers"]                                              # Render is closed until scenes land
    assert len(env["runners"]) == 1 and _job(body["job_id"]).job_type == "studio_audio"
    kept = studio_audio.source_file(p["id"])
    assert kept is not None and kept.name == "audio.wav" and kept.stat().st_size > 1000
    poll = client.get(f"/api/studio/projects/{p['id']}", params={"known_revision": p["revision"]}).json()
    assert poll["unchanged"] is True and poll["audio_alive"] is True
    env["runners"].pop().close()


def test_the_users_file_is_copied_never_moved_or_changed(client, tmp_path):
    src = _recording(tmp_path)
    before = src.read_bytes()
    row = asyncio.run(studio_audio.create(src, "My take.wav"))
    assert src.read_bytes() == before
    kept = studio_audio.source_file(row.id)
    assert kept.read_bytes() == before and kept.stat().st_ino != src.stat().st_ino      # a copy, not a link to theirs
    asyncio.run(store.delete_project(row.id))
    assert studio_audio.source_file(row.id) is None and src.read_bytes() == before


# ── the listening job ───────────────────────────────────────────────────────

def test_listening_lands_one_scene_per_sentence_with_its_take_in_one_write(client, env, tmp_path):
    body = _started(client, env, tmp_path)
    pid = body["project"]["id"]
    whisper = _listen(env, plan=AsyncMock(return_value={"planned": 0, "warnings": [], "voice_job_id": None}))
    kw = whisper.call_args.kwargs
    assert kw["quality"] == "balanced" and kw["skip_if_no_speech"] is True
    p = _get(client, pid)
    assert p["revision"] == 2                                                   # ONE write landed both scenes
    assert [s["line"] for s in p["doc"]["scenes"]] == [S1, S2]
    for scene in p["doc"]["scenes"]:
        v = scene["voice"]
        assert v["provider"] == "own_audio" and v["state"] == "ready" and v["words"]
        take = vts.recorded_take(v["take_key"])
        assert take is not None and abs(probe_duration(take.path, 0.0) - v["duration"]) < 0.05
        meta = vts.read_meta(v["take_key"])
        assert meta["provider"] == "own_audio" and meta["words_version"] == vts.WORDS_VERSION and meta["words"]
    assert p["derived"]["audio_listening"] is False and p["audio_failed"] is None
    assert p["doc"]["input"]["audio"]["language"] == "en"
    job = _job(body["job_id"])
    assert job.status == "success" and json.loads(job.output_json)["scenes"] == 2
    assert {"type": "studio_project_updated", "project_id": pid, "revision": 2} in env["sent"]
    assert not list(Path(studio_audio.settings.TMP_DIR).glob("studio_audio_*"))  # scratch gone


def test_the_take_keys_are_the_own_audio_keys_and_the_scenes_are_planned(client, env, tmp_path):
    pid = _started(client, env, tmp_path)["project"]["id"]
    _listen(env)
    doc = _get(client, pid)["doc"]
    lines = oa.cut_lines(HEARD, doc["input"]["audio"]["duration"], doc["input"]["audio"]["audio_id"])
    assert [s["voice"]["take_key"] for s in doc["scenes"]] == [ln["key"] for ln in lines]
    assert doc["scenes"][0]["visual"]["state"] == "bound"                        # then planned like any new project


def test_a_failed_listening_leaves_no_stuck_project_and_says_why(client, env, tmp_path):
    body = _started(client, env, tmp_path)
    pid = body["project"]["id"]
    _listen(env, whisper=AsyncMock(side_effect=RuntimeError("/private/path ctranslate2 exploded")))
    p = _get(client, pid)
    assert p["doc"]["scenes"] == [] and p["derived"]["audio_listening"] is False
    assert p["audio_failed"] == {"code": "failed", "message": studio_audio.LISTEN_FAILED, "retry": True}
    job = _job(body["job_id"])
    assert job.status == "failed" and "private" not in (job.error_message or "")
    poll = client.get(f"/api/studio/projects/{pid}", params={"known_revision": p["revision"]}).json()
    assert poll["unchanged"] is True and "audio_alive" not in poll               # → the editor stops saying "Listening…"
    assert {"type": "studio_project_updated", "project_id": pid} in env["sent"]   # the editor is told to re-read
    # Try again listens to the project's own copy: no second upload.
    again = client.post(f"/api/studio/projects/{pid}/listen")
    assert again.status_code == 200 and again.json()["project"]["derived"]["audio_listening"] is True
    assert client.post(f"/api/studio/projects/{pid}/listen").json()["detail"]["code"] == "busy"
    _listen(env)
    assert len(_get(client, pid)["doc"]["scenes"]) == 2
    assert client.post(f"/api/studio/projects/{pid}/listen").json()["detail"]["code"] == "invalid"


def test_no_speech_is_a_refusal_that_a_retry_cannot_fix(client, env, tmp_path):
    body = _started(client, env, tmp_path)
    _listen(env, {"language": "en", "segments": []})
    p = _get(client, body["project"]["id"])
    assert p["audio_failed"]["code"] == "no_speech" and p["audio_failed"]["retry"] is False and p["doc"]["scenes"] == []
    assert json.loads(_job(body["job_id"]).output_json)["refused"] == "no_speech"


def test_a_recording_with_no_speech_is_refused_without_a_decode(client, env, tmp_path):
    """The real transcribe + the real speech detector on 5 s of silence: refused, and the model is never touched."""
    from backend.services.whisper_service import whisper_service
    p = tmp_path / "quiet room.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=5", str(p)],
                   check=True, timeout=60)
    body = _post(client, p).json()

    # The job fetches the model once before listening; the real transcribe must not load it again.
    ensure = AsyncMock(return_value=None)
    with patch.object(whisper_service, "ensure_model", ensure), \
            patch.object(whisper_service, "is_model_cached", lambda *_a, **_k: True), \
            patch.object(tasks, "plan_into_project", _fake_plan):
        run(env)
    got = _get(client, body["project"]["id"])
    assert got["audio_failed"]["code"] == "no_speech" and got["audio_failed"]["retry"] is False
    assert got["doc"]["scenes"] == [] and ensure.await_count == 1


def test_a_decode_past_its_limit_fails_in_plain_words(client, env, tmp_path, monkeypatch):
    body = _started(client, env, tmp_path)
    monkeypatch.setattr(studio_audio, "WHISPER_LIMIT_S", 0.05)

    async def slow(*_a, **_k):
        await asyncio.sleep(5)
        return HEARD
    _listen(env, whisper=slow)
    assert _get(client, body["project"]["id"])["audio_failed"]["message"] == studio_audio.TOOK_TOO_LONG


def test_a_cancel_while_listening_stops_the_job_and_lands_nothing(client, env, tmp_path, monkeypatch):
    from backend.agents.job_helper import cancel_if_live
    body = _started(client, env, tmp_path)
    monkeypatch.setattr(tasks, "AUDIO_WATCH_S", 0.05)

    async def slow(*_a, **_k):
        await cancel_if_live(body["job_id"])
        await asyncio.sleep(30)
        return HEARD
    _listen(env, whisper=slow)
    p = _get(client, body["project"]["id"])
    job = _job(body["job_id"])
    assert job.status == "cancelled" and job.completed_at is not None
    assert p["doc"]["scenes"] == [] and p["derived"]["audio_listening"] is False
    assert p["audio_failed"]["code"] == "cancelled" and p["audio_failed"]["retry"] is True


def test_a_job_dropped_while_it_queued_leaves_nothing_listening(client, env, tmp_path):
    from backend.agents.job_helper import cancel_if_live
    body = _started(client, env, tmp_path)
    assert asyncio.run(cancel_if_live(body["job_id"]))
    asyncio.run(task_runner._run_with_limit(env["runners"].pop(0), asyncio.Semaphore(1)))
    p = _get(client, body["project"]["id"])
    assert p["derived"]["audio_listening"] is False and p["audio_failed"]["code"] == "cancelled"
    assert _job(body["job_id"]).completed_at is not None


def test_a_dead_process_leaves_a_project_that_offers_try_again(client, env, tmp_path):
    """The boot sweep fails the job row; nothing else holds the project."""
    from backend.agents.job_helper import update_job_status
    body = _started(client, env, tmp_path)
    env["runners"].pop().close()
    asyncio.run(update_job_status(body["job_id"], "failed", error_message="Server restarted — job did not complete"))
    p = _get(client, body["project"]["id"])
    assert p["derived"]["audio_listening"] is False
    assert p["audio_failed"] == {"code": "failed", "message": studio_audio.LISTEN_FAILED, "retry": True}


def test_deleting_the_project_mid_listening_ends_the_job_quietly(client, env, tmp_path):
    body = _started(client, env, tmp_path)
    assert client.delete(f"/api/studio/projects/{body['project']['id']}").status_code == 200
    _listen(env)
    assert _job(body["job_id"]).status == "cancelled"


def test_a_plan_that_fails_after_the_landing_keeps_the_scenes(client, env, tmp_path):
    body = _started(client, env, tmp_path)
    _listen(env, plan=AsyncMock(side_effect=RuntimeError("pexels down")))
    p = _get(client, body["project"]["id"])
    job = _job(body["job_id"])
    assert len(p["doc"]["scenes"]) == 2 and job.status == "success" and "Re-plan" in json.loads(job.output_json)["warning"]
    assert "studio_plan" in env["warnings"]


def test_a_second_landing_changes_nothing(client, env, tmp_path):
    pid = _started(client, env, tmp_path)["project"]["id"]
    _listen(env)
    doc = _get(client, pid)["doc"]
    lines = oa.cut_lines(HEARD, doc["input"]["audio"]["duration"], doc["input"]["audio"]["audio_id"])
    assert asyncio.run(studio_audio.land(pid, lines, "en")) is None
    assert _get(client, pid)["doc"] == doc


# ── editing, the checks, the render ─────────────────────────────────────────

def _landed(client, env, tmp_path):
    pid = _started(client, env, tmp_path)["project"]["id"]
    _listen(env)
    return _get(client, pid)


def _patch(client, p, *ops):
    return client.patch(f"/api/studio/projects/{p['id']}", json={"if_revision": p["revision"], "ops": list(ops)})


def test_scenes_of_your_audio_cannot_be_split_merged_added_or_revoiced(client, env, tmp_path):
    p = _landed(client, env, tmp_path)
    a, b = (s["uid"] for s in p["doc"]["scenes"])
    for op, sentence in (({"op": "split_scene", "uid": a}, sd.AUDIO_NO_SPLIT),
                         ({"op": "merge_scenes", "uid": a, "next_uid": b}, sd.AUDIO_NO_SPLIT),
                         ({"op": "insert_scene", "after_uid": a, "line": "A new line."}, sd.AUDIO_NO_INSERT),
                         ({"op": "set_setting", "path": "voice", "value": {"provider": "edge_tts", "voice": "x"}},
                          sd.AUDIO_NO_VOICE)):
        r = _patch(client, p, op)
        assert r.status_code == 400 and r.json()["detail"]["message"] == sentence, op
    r = client.post(f"/api/studio/projects/{p['id']}/voice", json={"scene_uids": "all"})
    assert not env["runners"], r.text                                          # nothing to voice
    assert _get(client, p["id"])["doc"] == p["doc"]


def test_a_line_edit_changes_the_captions_and_keeps_the_take(client, env, tmp_path):
    p = _landed(client, env, tmp_path)
    first = p["doc"]["scenes"][0]
    r = _patch(client, p, {"op": "set_line", "uid": first["uid"], "line": "Cats nap all day long."})
    assert r.status_code == 200
    after = r.json()["project"]["doc"]["scenes"][0]
    assert after["line"] == "Cats nap all day long." and after["voice"]["state"] == "ready"
    assert after["voice"]["take_key"] == first["voice"]["take_key"] and after["voice"]["duration"] == first["voice"]["duration"]
    assert [w[0] for w in after["voice"]["words"]] == "Cats nap all day long.".split()
    b = p["doc"]["scenes"][1]["uid"]                                              # reorder and delete are allowed
    assert client.patch(f"/api/studio/projects/{p['id']}",
                        json={"ops": [{"op": "reorder_scenes", "order": [b, first["uid"]]}]}).status_code == 200


def test_no_scene_of_an_audio_project_can_lose_its_own_audio():
    doc = {"input": {"mode": "audio", "audio": {"audio_id": "au_0123456789abcdef"}},
           "settings": sd.default_settings(), "scenes": [sd.make_scene("A typed line.")]}
    with pytest.raises(sd.StudioDocError):
        sd.validate_doc(doc)
    assert sd.validate_doc({**doc, "scenes": []})["scenes"] == []               # empty only while it listens
    with pytest.raises(sd.StudioDocError):
        sd.validate_doc({"input": {"mode": "script"}, "settings": sd.default_settings(), "scenes": []})


def test_the_render_speaks_in_the_users_audio_and_never_synthesises(client, env, tmp_path, monkeypatch):
    async def never(*_a, **_k):
        raise AssertionError("own audio must never be synthesised")
    monkeypatch.setattr(vts, "_synthesise", never)
    p = _landed(client, env, tmp_path)
    client.patch(f"/api/studio/projects/{p['id']}", json={"ops": [
        {"op": "set_setting", "path": "music", "value": {"enabled": False}}]})
    p = _get(client, p["id"])
    assert not p["checks"]["blockers"]
    assert start_render(client, p).status_code == 200
    run(env)
    rows = generated_for(p["id"])
    assert len(rows) == 1 and Path(rows[0].video_path).is_file()
    spoken = sum(s["voice"]["duration"] for s in p["doc"]["scenes"])
    assert probe_duration(rows[0].video_path, 0.0) >= spoken - 0.2


def test_a_lost_take_blocks_the_render_and_is_never_voiced(client, env, tmp_path):
    p = _landed(client, env, tmp_path)
    vts.take_path(p["doc"]["scenes"][1]["voice"]["take_key"]).unlink()
    again = _get(client, p["id"])
    assert any("no longer" in b for b in again["checks"]["blockers"])
    assert start_render(client, again).status_code == 400
