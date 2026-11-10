# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Render v2 — a real ffmpeg render of a Studio project.

Only the outside edges are faked: TTS synthesis writes a real sine mp3, stock
downloads copy a real test clip, the AI metadata writer and messaging are
recorded. Everything in between — normalise, fit, stitch, narration, mix,
captions, validation, the GeneratedVideo row — is real.
"""
import asyncio
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SCRIPT = ("A jar of coins sits on a wooden table in soft morning light by the window. "
          "A hand reaches in and drops one more shiny coin into the jar.")
FALLBACK_URL = "https://videos.pexels.com/video-files/777/hd.mp4"


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True, capture_output=True)


def sine(out, seconds=1.2):
    ff("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", str(out))



@pytest.fixture(autouse=True)
def _no_saved_preferences(monkeypatch):
    """A new project starts from the user's saved Settings (voice, captions).
    The API test modules share one database, and other tests save a caption
    style there — so these tests pin "nothing saved" instead of depending on
    test order."""
    from backend.api import studio as studio_api

    async def none():
        return None
    monkeypatch.setattr(studio_api, "saved_settings", none)

@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("render-media")
    ff("-f", "lavfi", "-i", "testsrc=size=540x960:rate=30", "-t", "2", "-pix_fmt", "yuv420p", str(d / "clip.mp4"))
    ff("-f", "lavfi", "-i", "sine=frequency=220:duration=6", str(d / "music.mp3"))
    return d


@pytest.fixture(scope="module")
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api import studio
    from backend.database import init_db

    asyncio.run(init_db())
    app = FastAPI()
    app.include_router(studio.router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def env(monkeypatch, tmp_path, media):
    from backend.agents.generator import GeneratorAgent
    from backend.core import task_runner
    from backend.core.ws_manager import ws_manager
    from backend.messaging.manager import messaging
    from backend.services import render_post, studio_media
    from backend.services import voice_take_service as vts

    state = {"runners": [], "sent": [], "warnings": [], "downloads": [], "dead": set(), "search": [],
             "slow": {}, "cancelled": []}
    monkeypatch.setattr(vts, "cache_dir", lambda: tmp_path / "voice")
    # The Smart Video runners live in backend.core.tasks.studio; both dispatchers are captured.
    monkeypatch.setattr(task_runner, "dispatch", state["runners"].append)
    monkeypatch.setattr(task_runner, "dispatch_light", state["runners"].append)

    async def synth(text, spec, out):
        sine(out)

    async def words(path, text):
        return [{"word": w, "start": 0.1 * i, "end": 0.1 * i + 0.09} for i, w in enumerate(text.split()[:8])]

    monkeypatch.setattr(vts, "_synthesise", synth)
    monkeypatch.setattr(vts, "_word_timings", words)

    async def download(url, dest, max_bytes=None):
        state["downloads"].append(url)
        if url in state["slow"]:
            try:
                await asyncio.sleep(state["slow"][url])
            except asyncio.CancelledError:
                state["cancelled"].append(url)
                raise
        if url in state["dead"]:
            raise RuntimeError("410 gone")
        shutil.copyfile(media / "clip.mp4", dest)

    async def search(project_id, query, aspect_ratio="9:16"):
        state["search"].append(query)
        return [studio_media.candidate_from_result({
            "source": "pexels", "id": 777, "download_url": FALLBACK_URL,
            "duration": 5, "width": 1080, "height": 1920}, query)]

    monkeypatch.setattr(studio_media, "download", download)
    monkeypatch.setattr(studio_media, "search_candidates", search)

    async def metadata(self, script, niche, user_settings):
        return {"youtube": {"title": "Coins in a jar", "description": "d", "tags": ["coins"]},
                "tiktok": {"title": "Coins", "description": "t"}}

    monkeypatch.setattr(GeneratorAgent, "_generate_metadata", metadata)

    async def no_user_settings(user_id="local"):
        return SimpleNamespace(sfx_enabled=False, auto_zoom_enabled=False, caption_emoji_style="none")

    monkeypatch.setattr(render_post, "load_user_settings", no_user_settings)

    async def notify(*_a, **_k):
        return None

    monkeypatch.setattr(messaging, "notify", notify)

    async def send(message, user_id="local"):
        state["sent"].append(message)

    async def warning(constraint, message, severity="warning", wizard_id=None, user_id="local"):
        state["warnings"].append(constraint)

    monkeypatch.setattr(ws_manager, "send", send)
    monkeypatch.setattr(ws_manager, "send_constraint_warning", warning)
    return state


def stock_visual(i, alternates=()):
    return {"kind": "stock", "state": "bound", "query": "coins jar", "fit": "loop", "pinned": False,
            "reason": None, "alternates": list(alternates),
            "asset": {"provider": "pexels", "provider_id": str(100 + i),
                      "download_url": f"https://videos.pexels.com/video-files/{100 + i}/hd.mp4",
                      "license": "Pexels License", "creator": "C"}}


def make_project(client, *, music=False, captions=True, script=SCRIPT):
    from backend.services import studio_store as store
    p = client.post("/api/studio/projects", json={"start": {"script": script}}).json()["project"]
    ops = [{"op": "plan_result", "uid": sc["uid"], "expect_line": sc["line"], "visual": stock_visual(i)}
           for i, sc in enumerate(p["doc"]["scenes"])]
    ops.append({"op": "set_setting", "path": "music", "value": {"enabled": music}})
    ops.append({"op": "set_setting", "path": "captions.enabled", "value": captions})
    asyncio.run(store.write_ops(p["id"], ops, if_revision=None, internal=True))
    return client.get(f"/api/studio/projects/{p['id']}").json()["project"]


def start_render(client, project):
    return client.post(f"/api/studio/projects/{project['id']}/render")


def run(env):
    asyncio.run(env["runners"].pop(0))


@pytest.fixture
def loop():
    """A loop a test drives across several steps (a shielded take outlives the
    render that started it). Closed the way asyncio.run closes one: tasks still
    pending are cancelled and awaited, or one left holding a SQLite write leaves
    "database is locked" for every later module."""
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    try:
        pending = [t for t in asyncio.all_tasks(lp) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            lp.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        lp.run_until_complete(lp.shutdown_asyncgens())
    finally:
        asyncio.set_event_loop(None)
        lp.close()


async def drain_takes():
    from backend.services import studio_render
    for _ in range(200):
        if not studio_render._inflight_takes:
            return
        await asyncio.sleep(0.05)


def generated_for(project_id):
    from sqlalchemy import select

    from backend.database import AsyncSessionLocal
    from backend.models.generated_video import GeneratedVideo

    async def q():
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(GeneratedVideo).where(GeneratedVideo.project_id == project_id))).scalars().all()
    return asyncio.run(q())


def test_render_assembles_exactly_the_project(client, env):
    from backend.services.output_validator import validate_output
    from backend.services.studio_render import OUTRO_HOLD_S

    project = make_project(client)
    r = start_render(client, project)
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] and r.json()["project"]["status"] == "rendering"
    run(env)
    rows = generated_for(project["id"])
    assert len(rows) == 1
    video = rows[0]
    check = asyncio.run(validate_output(Path(video.video_path), expect={"audio": True}))
    assert check.ok and check.has_audio
    after = client.get(f"/api/studio/projects/{project['id']}").json()["project"]
    # The render holds the last shot OUTRO_HOLD_S after the last word.
    planned = after["derived"]["total"] + OUTRO_HOLD_S
    assert abs(check.duration - planned) <= 0.75
    assert after["status"] == "rendered" and after["last_video_id"] == video.id
    assert all(s["voice"]["state"] == "ready" for s in after["doc"]["scenes"])
    assert video.title == "Coins in a jar" and video.source_type == "smart_video" and video.thumbnail_path
    assert video.caption_status == "applied" and video.metadata_status == "ai_generated"
    complete = next(m for m in env["sent"] if m.get("type") == "job_complete")
    assert {"generated_video_id", "title", "aspect_ratio", "source_type", "settings", "project_id"} <= set(complete["result"])
    job = asyncio.run(_job_output(r.json()["job_id"]))
    assert job["stock_sources"][0]["source"] == "pexels" and job["generated_video_id"] == video.id


async def _job_output(job_id):
    import json

    from backend.database import AsyncSessionLocal
    from backend.models.job import Job
    async with AsyncSessionLocal() as db:
        job = await db.get(Job, job_id)
    return json.loads(job.output_json or "{}")


def test_render_is_refused_until_it_is_right(client, env):
    from backend.services import studio_store as store

    project = make_project(client)
    rev = project["revision"]
    blocked = client.patch(f"/api/studio/projects/{project['id']}", json={"if_revision": rev, "ops": [
        {"op": "insert_scene", "after_uid": None, "line": ""}]}).json()["project"]
    assert blocked["checks"]["blockers"]
    r = start_render(client, blocked)
    assert r.status_code == 400 and "no line yet" in r.json()["detail"]["message"]
    assert r.json()["detail"]["code"] == "blocked" and r.json()["detail"]["blockers"]
    fresh = make_project(client)
    assert start_render(client, fresh).status_code == 200
    again = start_render(client, fresh)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_rendering"
    env["runners"].pop().close()
    asyncio.run(store.finish_render(fresh["id"]))
    store.release_render(fresh["id"])


def test_a_dead_stock_link_is_replaced_with_a_warning(client, env):
    project = make_project(client, captions=False)
    env["dead"].add(project["doc"]["scenes"][0]["visual"]["asset"]["download_url"])
    start_render(client, project)
    run(env)
    assert "stock_clip_replaced" in env["warnings"]
    assert env["search"] and FALLBACK_URL in env["downloads"] and len(generated_for(project["id"])) == 1


def test_a_failed_output_check_fails_the_job_and_returns_to_draft(client, env, monkeypatch):
    from backend.services import output_validator

    class Bad:
        ok = False
        duration = 0
        has_audio = False

        def message(self):
            return "no video stream"

    async def failing(path, expect=None):
        return Bad()

    monkeypatch.setattr(output_validator, "validate_output", failing)
    project = make_project(client, captions=False)
    start_render(client, project)
    run(env)
    assert generated_for(project["id"]) == []
    after = client.get(f"/api/studio/projects/{project['id']}").json()["project"]
    assert after["status"] == "draft"
    failed = next(m for m in env["sent"] if m.get("type") == "job_failed")
    assert "didn't pass its checks" in failed["error"]


def test_music_bed_from_the_library_is_mixed(client, env, media, monkeypatch):
    from backend.services import music_service
    from backend.services import studio_store as store
    monkeypatch.setattr(music_service, "library_path", lambda name: media / "music.mp3")
    mixed = []
    real_mix = music_service.mix_audio

    async def spy(*a, **k):
        mixed.append(a[1])
        return await real_mix(*a, **k)
    monkeypatch.setattr(music_service, "mix_audio", spy)
    project = make_project(client, music=True, captions=False)
    asyncio.run(store.write_ops(project["id"], [{"op": "set_setting", "path": "music", "value": {
        "enabled": True, "source": "library", "track": "music.mp3", "track_name": "Test", "offset_s": 1.0}}],
        if_revision=None))
    project = client.get(f"/api/studio/projects/{project['id']}").json()["project"]
    start_render(client, project)
    run(env)
    assert "music_missing" not in env["warnings"] and len(generated_for(project["id"])) == 1
    assert len(mixed) == 1                                             # the bed went into the mix


@pytest.mark.parametrize("fit", ["loop", "freeze", "slow"])
def test_every_fit_produces_exactly_the_slot(fit, media, tmp_path):
    from backend.services.studio_render import fit_clip
    from backend.services.video_utils import probe_duration
    out = fit_clip(media / "clip.mp4", 3.5, fit, tmp_path / f"{fit}.mp4")
    assert abs(probe_duration(out, 0) - 3.5) < 0.1


def test_a_ready_take_is_rendered_as_heard(client, env, monkeypatch):
    """A second render re-uses the takes the first one landed — nothing is
    recorded again."""
    from backend.services import voice_take_service as vts

    project = make_project(client)
    assert start_render(client, project).status_code == 200
    run(env)
    recorded = {s["uid"]: s["voice"]["take_key"] for s in
                client.get(f"/api/studio/projects/{project['id']}").json()["project"]["doc"]["scenes"]}
    calls = []
    real_record = vts._record

    async def counting_record(*a, **k):
        calls.append(a[0])
        return await real_record(*a, **k)
    monkeypatch.setattr(vts, "_record", counting_record)

    again = client.get(f"/api/studio/projects/{project['id']}").json()["project"]
    assert start_render(client, again).status_code == 200
    run(env)
    after = client.get(f"/api/studio/projects/{project['id']}").json()["project"]
    assert not calls
    assert {s["uid"]: s["voice"]["take_key"] for s in after["doc"]["scenes"]} == recorded


def test_a_finished_render_reports_the_draft_to_video_funnel(client, env, monkeypatch):
    from backend.core.user_intelligence import UserIntelligence

    events = []

    async def record(self, event_type, data, user_id="local"):
        events.append((event_type, data))
    monkeypatch.setattr(UserIntelligence, "record_event", record)
    project = make_project(client)
    assert start_render(client, project).status_code == 200
    run(env)
    assert any(e == "studio_render_started" and d["project_id"] == project["id"] for e, d in events)
    done = [d for e, d in events if e == "video_generated" and "first_render" in d]
    assert len(done) == 1 and done[0]["project_id"] == project["id"] and done[0]["first_render"] is True
    again = client.get(f"/api/studio/projects/{project['id']}").json()["project"]
    assert start_render(client, again).status_code == 200
    run(env)
    assert [d for e, d in events if e == "video_generated" and "first_render" in d][1]["first_render"] is False


def test_music_on_with_no_track_picks_one_at_render(client, env, media, monkeypatch):
    """Music switched on with no track must not silently render without it."""
    from backend.services import music_service

    async def pick(genre="lofi", duration=60, track_filename=None, **_):
        return media / "music.mp3"
    monkeypatch.setattr(music_service, "select_music", pick)
    monkeypatch.setattr(music_service, "library_path", lambda name: media / "music.mp3")
    project = make_project(client, music=True, captions=False)
    assert project["doc"]["settings"]["music"]["track"] is None
    assert start_render(client, project).status_code == 200
    run(env)
    assert "music_auto_picked" in env["warnings"] and "music_missing" not in env["warnings"]
    after = client.get(f"/api/studio/projects/{project['id']}").json()["project"]
    assert after["doc"]["settings"]["music"]["track"] == "music.mp3"          # kept on the project

    # An empty library still renders — without music, and says why.
    async def nothing(*_a, **_k):
        return None
    monkeypatch.setattr(music_service, "select_music", nothing)
    empty = make_project(client, music=True, captions=False)
    env["warnings"].clear()
    assert start_render(client, empty).status_code == 200
    run(env)
    assert "music_missing" in env["warnings"] and len(generated_for(empty["id"])) == 1


def test_a_missing_clip_fails_the_render_before_any_visual_work(client, env):
    """Cheap blockers first — nothing is downloaded or encoded for a render
    that can't finish."""
    from backend.services import studio_store as store

    project = make_project(client, captions=False)
    uid0 = project["doc"]["scenes"][0]["uid"]
    record = {"asset_id": "as_00000000000000ab", "title": "gone", "media": "video", "source": "upload",
              "native_duration": 3.0, "width": 1080, "height": 1920, "file": "gone.mp4", "poster": None}
    asyncio.run(store.update_assets(project["id"], lambda a: {**a, "imports": {record["asset_id"]: record}}))
    asyncio.run(store.write_ops(project["id"], [{"op": "bind_visual", "uid": uid0, "visual": {
        "kind": "user_clip", "asset_id": record["asset_id"]}}], if_revision=None))
    project = client.get(f"/api/studio/projects/{project['id']}").json()["project"]
    assert project["doc"]["scenes"][0]["visual"]["kind"] == "user_clip"
    assert start_render(client, project).status_code == 200
    run(env)
    failed = next(m for m in env["sent"] if m.get("type") == "job_failed")
    assert "missing" in failed["error"] and failed["job_type"] == "generate"
    assert env["downloads"] == []
    assert client.get(f"/api/studio/projects/{project['id']}").json()["project"]["status"] == "draft"


def test_a_failing_scene_cancels_the_rest(client, env):
    """The first failure cancels the sibling scenes (gather would let them keep
    downloading and encoding after the render had already failed)."""
    project = make_project(client, captions=False)
    env["dead"].update({project["doc"]["scenes"][0]["visual"]["asset"]["download_url"], FALLBACK_URL})
    slow_url = project["doc"]["scenes"][1]["visual"]["asset"]["download_url"]
    env["slow"][slow_url] = 30.0
    assert start_render(client, project).status_code == 200
    started = time.monotonic()
    run(env)
    assert time.monotonic() - started < 20
    failed = next(m for m in env["sent"] if m.get("type") == "job_failed")
    assert "Scene 1 has no usable footage" in failed["error"]
    assert env["cancelled"] == [slow_url]
    assert generated_for(project["id"]) == []


def test_render_completion_carries_its_job_type_and_failures_hide_internals(client, env, monkeypatch):
    """The render ends through the same path as every studio job; a crash's
    message (paths, tool output) never reaches the page."""
    from backend.services import studio_render

    project = make_project(client, captions=False)
    assert start_render(client, project).status_code == 200
    run(env)
    done = next(m for m in env["sent"] if m.get("type") == "job_complete")
    assert done["job_type"] == "generate" and done["result"]["generated_video_id"] and done["result"]["status"] == "success"

    async def crash(*_a, **_k):
        raise OSError("[Errno 28] No space left on device: '/Users/someone/ViralMint/tmp/x.mp4'")
    monkeypatch.setattr(studio_render, "render", crash)
    env["sent"].clear()
    again = client.get(f"/api/studio/projects/{project['id']}").json()["project"]
    assert start_render(client, again).status_code == 200
    run(env)
    failed = next(m for m in env["sent"] if m.get("type") == "job_failed")
    assert failed["job_type"] == "generate" and "/Users" not in failed["error"] and "Errno" not in failed["error"]
    # The claim is released either way: the project can render again.
    assert client.get(f"/api/studio/projects/{project['id']}").json()["project"]["status"] in ("draft", "rendered")


def test_ffmpeg_failures_are_logged_not_shown(tmp_path):
    from backend.core.exceptions import StudioError
    from backend.services.studio_render import _ff
    with pytest.raises(StudioError) as err:
        _ff(["-i", "/definitely/not/here.mp4", str(tmp_path / "out.mp4")])
    assert "/definitely" not in str(err.value)


def test_a_normalised_clip_long_enough_is_kept_not_re_encoded(tmp_path):
    """The normalise pass already made exactly the scene's length."""
    from backend.services import studio_render
    from backend.services.video_utils import probe_duration

    norm = tmp_path / "norm_00.mp4"
    ff("-f", "lavfi", "-i", "testsrc=size=270x480:rate=30", "-t", "2", "-an", "-pix_fmt", "yuv420p", str(norm))
    out = tmp_path / "scene_00.mp4"
    inode = norm.stat().st_ino
    studio_render._fit_or_keep(norm, norm, 1.5, "loop", out)
    assert out.stat().st_ino == inode and not norm.exists()              # renamed, not re-encoded
    short = tmp_path / "norm_01.mp4"
    ff("-f", "lavfi", "-i", "testsrc=size=270x480:rate=30", "-t", "0.8", "-an", "-pix_fmt", "yuv420p", str(short))
    looped = tmp_path / "scene_01.mp4"
    studio_render._fit_or_keep(short, short, 1.6, "loop", looped)
    assert abs(probe_duration(looped, 0) - 1.6) < 0.15 and short.exists()
    # The normaliser fell back to returning its INPUT (a user's clip): never moved.
    source = tmp_path / "users_clip.mp4"
    ff("-f", "lavfi", "-i", "testsrc=size=270x480:rate=30", "-t", "2", "-an", "-pix_fmt", "yuv420p", str(source))
    studio_render._fit_or_keep(source, tmp_path / "norm_02.mp4", 1.0, "loop", tmp_path / "scene_02.mp4")
    assert source.exists()


def test_scene_encodes_share_one_bounded_semaphore_per_loop():
    from backend.services import studio_render

    async def slots():
        return studio_render.encode_slots(), studio_render.encode_slots()
    a, b = asyncio.run(slots())
    c, _ = asyncio.run(slots())
    assert a is b and a is not c and studio_render.ENCODE_SLOTS >= 2


def test_a_take_in_flight_when_a_sibling_fails_is_kept_not_recorded_twice(client, env, monkeypatch, loop):
    """When one take fails, a sibling already recording is not cancelled: it
    finishes and is cached, so the next render re-uses it (an OpenAI take has
    already spent the user's key)."""
    from backend.core.exceptions import VoiceGenerationError
    from backend.services import voice_take_service as vts

    project = make_project(client, captions=False)
    first, second = (s["line"] for s in project["doc"]["scenes"][:2])
    calls = []

    async def flaky(text, spec, out):
        calls.append(text)
        if text == first:
            raise VoiceGenerationError("The voice host returned no audio")
        await asyncio.sleep(0.3)                                # still in flight when the sibling fails
        sine(out)
    monkeypatch.setattr(vts, "_synthesise", flaky)
    assert start_render(client, project).status_code == 200

    loop.run_until_complete(env["runners"].pop(0))            # the render fails on the first take
    assert any(m.get("type") == "job_failed" for m in env["sent"])
    loop.run_until_complete(drain_takes())
    assert second in calls
    spec = vts.VoiceSpec.from_settings(project["doc"]["settings"])
    assert vts.is_cached(vts.key_for_line(spec, second))

    calls.clear()

    async def steady(text, spec, out):
        calls.append(text)
        sine(out)
    monkeypatch.setattr(vts, "_synthesise", steady)
    latest = client.get(f"/api/studio/projects/{project['id']}").json()["project"]
    assert start_render(client, latest).status_code == 200
    loop.run_until_complete(env["runners"].pop(0))
    assert second not in calls, "the shielded take was cached and re-used, not recorded again"
    assert first in calls and len(generated_for(project["id"])) == 1


def test_a_voice_that_cant_be_recorded_names_the_scene_not_try_again(client, env, monkeypatch, loop):
    """The job's message names the scene and the voice service's reason; the
    take that did record is kept for the next render."""
    from backend.core.exceptions import VoiceGenerationError
    from backend.services import voice_take_service as vts

    project = make_project(client, captions=False)
    first, second = (s["line"] for s in project["doc"]["scenes"][:2])

    async def second_fails(text, spec, out):
        if text == second:
            raise VoiceGenerationError("The voice engine couldn't record this line — reword it slightly or pick another voice.")
        sine(out)
    monkeypatch.setattr(vts, "_synthesise", second_fails)
    assert start_render(client, project).status_code == 200
    loop.run_until_complete(env["runners"].pop(0))

    failed = next(m for m in env["sent"] if m.get("type") == "job_failed")
    assert failed["error"].startswith("Scene 2's voice failed: The voice engine couldn't record this line")
    assert "try rendering again" not in failed["error"]
    loop.run_until_complete(drain_takes())
    spec = vts.VoiceSpec.from_settings(project["doc"]["settings"])
    assert vts.is_cached(vts.key_for_line(spec, first)), "the take that recorded is kept for the next render"


def _lufs(path):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    summary = out.rsplit("Summary:", 1)[-1]
    return float(summary.split("I:", 1)[1].split("LUFS", 1)[0])


def test_a_default_project_burns_urban_captions_with_the_hook_title(client, env, monkeypatch):
    from backend.services import caption_service
    from backend.services import studio_doc as sd

    seen = {}
    real = caption_service.generate_captions_ass

    async def spy(segments, **kw):
        path = await real(segments, **kw)
        if kw.get("hook_duration") != 30.0:   # not the frame cover's title-only file
            seen.update(kw, ass=Path(path).read_text(encoding="utf-8"))
        return path

    monkeypatch.setattr(caption_service, "generate_captions_ass", spy)
    project = make_project(client)
    s = project["doc"]["settings"]
    assert s["captions"]["style"] == "urban" and s["captions"]["hook"] is True
    assert s["post"]["sfx"] is False                                    # no synthesised beeps on a new project
    assert start_render(client, project).status_code == 200
    run(env)
    first_line = project["doc"]["scenes"][0]["line"]
    assert seen["style"] == "urban" and seen["hook_text"] == sd.auto_title(first_line) and seen["hook_text"]
    font = caption_service.CAPTION_STYLES["urban"]["font"]
    assert f"Style: Default,{font}," in seen["ass"] and f"Style: Hook,{font}," in seen["ass"]
    assert ",Hook,," in seen["ass"]                                     # the title event is emitted
    video = generated_for(project["id"])[0]
    assert video.caption_status == "applied"
    # The finished mix is lifted to the short-form delivery level.
    assert abs(_lufs(video.video_path) - (-14.0)) <= 1.5


def test_the_hook_title_can_be_retitled_or_turned_off(client, env, monkeypatch):
    from backend.services import caption_service
    from backend.services import studio_store as store

    seen = []
    real = caption_service.generate_captions_ass

    async def spy(segments, **kw):
        if kw.get("hook_duration") != 30.0:   # not the frame cover's title-only file
            seen.append(kw.get("hook_text"))
        return await real(segments, **kw)

    monkeypatch.setattr(caption_service, "generate_captions_ass", spy)
    project = make_project(client)
    asyncio.run(store.write_ops(project["id"], [
        {"op": "set_setting", "path": "captions.hook_text", "value": "  Stop   saving wrong  "}], if_revision=None))
    assert start_render(client, client.get(f"/api/studio/projects/{project['id']}").json()["project"]).status_code == 200
    run(env)
    project2 = make_project(client)
    asyncio.run(store.write_ops(project2["id"], [
        {"op": "set_setting", "path": "captions.hook", "value": False}], if_revision=None))
    assert start_render(client, client.get(f"/api/studio/projects/{project2['id']}").json()["project"]).status_code == 200
    run(env)
    assert seen == ["Stop saving wrong", None]


def test_a_long_stock_scene_cuts_to_its_alternate_at_render(client, env, monkeypatch):
    """A bound stock scene of SECOND_SHOT_MIN_S or more cuts to an unused
    alternate halfway — and the render still lands on the project's length."""
    from backend.services import studio_doc as sd
    from backend.services import studio_store as store
    from backend.services import voice_take_service as vts
    from backend.services.output_validator import validate_output
    from backend.services.studio_render import OUTRO_HOLD_S

    async def long_take(text, spec, out):
        sine(out, sd.SECOND_SHOT_MIN_S + 0.5)
    monkeypatch.setattr(vts, "_synthesise", long_take)
    project = make_project(client, captions=False, script="A jar of coins sits on a wooden table by the window.")
    assert len(project["doc"]["scenes"]) == 1
    uid = project["doc"]["scenes"][0]["uid"]
    alt = {"provider": "pexels", "provider_id": "900", "download_url": "https://videos.pexels.com/video-files/900/hd.mp4",
           "license": "Pexels License", "creator": "D"}
    asyncio.run(store.write_ops(project["id"], [{"op": "plan_result", "uid": uid, "visual": stock_visual(0, [alt])}],
                                if_revision=None, internal=True))
    project = client.get(f"/api/studio/projects/{project['id']}").json()["project"]
    assert project["doc"]["scenes"][0]["visual"]["alternates"][0]["provider_id"] == "900"
    assert start_render(client, project).status_code == 200
    run(env)
    assert alt["download_url"] in env["downloads"]
    video = generated_for(project["id"])[0]
    after = client.get(f"/api/studio/projects/{project['id']}").json()["project"]
    check = asyncio.run(validate_output(Path(video.video_path), expect={"audio": True}))
    assert check.ok and abs(check.duration - (after["derived"]["total"] + OUTRO_HOLD_S)) <= 0.75


def test_the_cover_is_a_clean_frame_with_the_title(client, env, monkeypatch):
    from backend.services import studio_render

    covers = []
    real = studio_render.frame_cover

    async def spy(clean, title, style, aspect, at, out):
        got = await real(clean, title, style, aspect, at, out)
        covers.append((clean.name, title, style, got))
        return got
    monkeypatch.setattr(studio_render, "frame_cover", spy)
    project = make_project(client)
    assert start_render(client, project).status_code == 200
    run(env)
    video = generated_for(project["id"])[0]
    assert len(covers) == 1
    clean, title, style, got = covers[0]
    assert clean == "stitched.mp4" and title == "Coins in a jar" and style == "urban"
    assert got is not None and video.thumbnail_path == str(got) and Path(got).stat().st_size > 1024


@pytest.mark.parametrize("youtube", [{}, {"title": "Untitled Video", "description": "d"}])
def test_a_missing_ai_title_falls_back_to_the_project_title(client, env, monkeypatch, youtube):
    """No AI title (no key, a failed call, or the writer's "Untitled Video"
    placeholder): the video and its cover carry the project's own title."""
    from backend.agents.generator import GeneratorAgent
    from backend.services import studio_render

    async def metadata(self, script, niche, user_settings):
        return {"youtube": dict(youtube), "tiktok": {}}
    monkeypatch.setattr(GeneratorAgent, "_generate_metadata", metadata)
    titles = []
    real = studio_render.frame_cover

    async def spy(clean, title, *a):
        titles.append(title)
        return await real(clean, title, *a)
    monkeypatch.setattr(studio_render, "frame_cover", spy)
    project = make_project(client, captions=False)
    assert start_render(client, project).status_code == 200
    run(env)
    video = generated_for(project["id"])[0]
    assert video.title == project["title"] != "Untitled Video"
    assert video.youtube_title == project["title"] and video.metadata_status == "fallback"
    assert titles == [project["title"]]


@pytest.mark.parametrize("captions", [False, True])
def test_fast_intermediates_get_exactly_one_real_final_encode(client, env, monkeypatch, captions):
    """VIDEO_INTERMEDIATE_PRESET=ultrafast: scene clips are near-lossless
    intermediates. With captions on the burn is the deliverable's encode; with
    captions off the render adds one, so the file a user downloads is never an
    ultrafast intermediate."""
    from backend.config import settings
    from backend.services import studio_render as sr
    from backend.services.output_validator import validate_output
    monkeypatch.setattr(settings, "VIDEO_INTERMEDIATE_PRESET", "ultrafast")
    calls = []
    real = sr._final_encode
    monkeypatch.setattr(sr, "_final_encode", lambda src, out: calls.append(src) or real(src, out))
    project = make_project(client, captions=captions)
    assert start_render(client, project).status_code == 200
    run(env)
    (video,) = generated_for(project["id"])
    assert calls == ([] if captions else [calls[0]])
    check = asyncio.run(validate_output(Path(video.video_path), expect={"audio": True}))
    assert check.ok and check.has_audio


def test_the_default_render_adds_no_extra_encode(client, env, monkeypatch):
    from backend.services import studio_render as sr
    calls = []
    monkeypatch.setattr(sr, "_final_encode", lambda src, out: calls.append(src) or src)
    project = make_project(client, captions=False)
    assert start_render(client, project).status_code == 200
    run(env)
    assert calls == [] and len(generated_for(project["id"])) == 1


def test_the_render_logs_where_its_time_went(client, env, caplog):
    import logging
    project = make_project(client)
    assert start_render(client, project).status_code == 200
    with caplog.at_level(logging.INFO, logger="backend.services.studio_render"):
        run(env)
    line = next(r.getMessage() for r in caplog.records if "studio render timing" in r.getMessage())
    for step in ("setup=", "voices=", "visuals=", "stitch=", "captions=", "persist=", "encode=", "fetch="):
        assert step in line
