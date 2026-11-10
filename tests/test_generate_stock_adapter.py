# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""`POST /api/generate/stock` is an adapter onto the Smart Video Studio.

A script body answers `{job_id, project_id}` with job type `generate`, and
underneath a real project is created, configured from the request, claimed,
then planned (inside the job) and handed to render v2 — so every such render
is an editable project afterwards. A single `start_image` with no
`user_images` keeps the one-shot pipeline and answers `{job_id}`.

The caller's `user_images` are imported as stills (a normalised copy — the
caller's file is never consumed) and bound, pinned, to the opening scenes in
order; extras and unreadable images are skipped with a `user_images_skipped`
warning, never fatal.

No network: the planner, the music pick and the AI client are faked, the
dispatched job is run explicitly and the render it hands over to is captured.
Real ffmpeg makes the stills so the import probes and normalises a genuine file.
"""
import asyncio
import json
import shutil
import subprocess

import pytest

SCRIPT = ("A jar of coins sits on a wooden table in soft morning light by the window. "
          "A hand reaches in and drops one more shiny coin into the jar. "
          "Small habits, repeated, are what fill it.")

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def tiny_png(path, color="red"):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:s=64x96",
                    "-frames:v", "1", str(path)], check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api import generate, studio
    from backend.database import init_db

    asyncio.run(init_db())
    app = FastAPI()
    app.include_router(generate.router, prefix="/api")
    app.include_router(studio.router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def env(monkeypatch):
    from backend.core import task_runner
    from backend.core.tasks import studio as studio_tasks
    from backend.core.ws_manager import ws_manager
    from backend.services import music_service, studio_media, studio_planner

    state = {"runners": [], "legacy": [], "planned": 0, "renders": [], "warnings": [], "sent": []}
    monkeypatch.setattr(task_runner, "dispatch", state["runners"].append)
    monkeypatch.setattr(task_runner, "dispatch_light", state["runners"].append)
    monkeypatch.setattr(studio_media, "stock_keys", lambda: ("k", ""))
    monkeypatch.setattr("backend.core.ai_provider.get_ai_client", lambda *a, **k: None)

    async def plan(doc, *, project_id, ai_client, pexels_key, pixabay_key="", only_uids=None):
        state["planned"] += 1
        ops = []
        for i, sc in enumerate(doc["scenes"]):
            cand = studio_media.candidate_from_result({
                "source": "pexels", "id": 500 + i,
                "download_url": f"https://videos.pexels.com/video-files/{500 + i}/hd.mp4",
                "duration": 6, "width": 1080, "height": 1920}, "coins jar")
            studio_media.remember(project_id, [cand])
            ops.append({"op": "plan_result", "uid": sc["uid"], "expect_line": sc["line"], "mood": "calm",
                        "visual": {"kind": "stock", "state": "bound", "asset": cand, "query": "coins jar",
                                   "alternates": [cand], "fit": "loop", "pinned": False, "reason": None,
                                   "planned_for": sc["line"]}})
        return ops, []
    monkeypatch.setattr(studio_planner, "plan_project", plan)

    async def pick(genre="lofi", duration=60, track_filename=None, **_):
        from pathlib import Path
        return Path(f"/music/{genre}_calm_01.mp3")
    monkeypatch.setattr(music_service, "select_music", pick)

    # The job plans for real; the render it hands over to is captured, never run.
    async def fake_render(job_id, project_id, user_id="local"):
        state["renders"].append((job_id, project_id))
    monkeypatch.setattr(studio_tasks, "run_studio_render", fake_render)

    # The one-shot pipeline (a lone start_image) is captured, never run.
    async def legacy(**kwargs):
        state["legacy"].append(kwargs)
        return {"job_id": "legacy-job"}
    from backend.api import generate
    monkeypatch.setattr(generate, "_dispatch_generate", legacy)

    async def send(message, user_id="local"):
        state["sent"].append(message)

    async def warning(constraint, message, severity="warning", wizard_id=None, user_id="local"):
        state["warnings"].append((constraint, message))
    monkeypatch.setattr(ws_manager, "send", send)
    monkeypatch.setattr(ws_manager, "send_constraint_warning", warning)
    return state


def run_jobs(env):
    """Run the dispatched /generate/stock jobs (planning happens in the job)."""
    while env["runners"]:
        asyncio.run(env["runners"].pop(0))


def project_of(client, job_response):
    return client.get(f"/api/studio/projects/{job_response['project_id']}").json()["project"]


def job_row(job_id):
    from sqlalchemy import select

    from backend.database import AsyncSessionLocal
    from backend.models.job import Job

    async def q():
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
    return asyncio.run(q())


def release(project_id):
    """A captured render never releases its claim — do it, so later tests and
    files see a settled project."""
    from backend.services import studio_store as store
    asyncio.run(store.finish_render(project_id))
    store.release_render(project_id)


def skipped(env):
    return [m for c, m in env["warnings"] if c == "user_images_skipped"]


# ── the adapter ────────────────────────────────────────────────────────────

def test_a_script_body_becomes_a_planned_project_and_a_generate_job(client, env):
    r = client.post("/api/generate/stock", json={
        "script": SCRIPT, "aspect_ratio": "9:16", "tts_provider": "edge_tts", "tts_voice": "en-US-AvaMultilingualNeural",
        "caption_style": "bold", "caption_enabled": True, "music_genre": "cinematic", "music_enabled": True,
        "visual_style": "neon", "transition_style": "smooth",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"job_id", "project_id"}
    job = job_row(body["job_id"])
    assert job.job_type == "generate" and json.loads(job.input_json)["project_id"] == body["project_id"]
    # The request answers before any planning — it runs in the job.
    assert len(env["runners"]) == 1 and env["planned"] == 0
    p = project_of(client, body)
    assert p["status"] == "rendering"                                  # claimed for render v2 at once
    run_jobs(env)
    assert env["planned"] == 1
    assert env["renders"] == [(body["job_id"], body["project_id"])]
    p = project_of(client, body)
    s = p["doc"]["settings"]
    assert s["voice"]["provider"] == "edge_tts" and s["voice"]["voice"] == "en-US-AvaMultilingualNeural"
    assert s["voice"]["label"] == "Ava"                                # not the default's "Andrew"
    assert s["captions"] == {"enabled": True, "style": "bold", "hook": True, "hook_text": None}
    assert s["music"]["enabled"] and s["music"]["genre"] == "cinematic" and s["music"]["track"] == "cinematic_calm_01.mp3"
    assert s["visual_style"] == "neon" and s["transition"] == "smooth"
    assert all(sc["visual"]["kind"] == "stock" and sc["visual"]["state"] == "bound" for sc in p["doc"]["scenes"])
    assert not env["legacy"]
    release(body["project_id"])


def test_mcp_and_onboarding_bodies(client, env):
    mcp = client.post("/api/generate/stock", json={
        "aspect_ratio": "16:9", "tts_provider": "openai_tts", "tts_voice": "nova", "caption_enabled": False,
        "caption_style": "viral", "music_enabled": False, "music_genre": "lofi", "script": SCRIPT, "source_id": None,
    })
    assert mcp.status_code == 200, mcp.text
    run_jobs(env)
    p = project_of(client, mcp.json())
    s = p["doc"]["settings"]
    assert p["doc"]["aspect_ratio"] == "16:9"
    assert s["voice"]["provider"] == "openai_tts" and s["voice"]["voice"] == "nova"
    assert s["captions"]["enabled"] is False and s["music"]["enabled"] is False and s["music"]["track"] is None
    release(mcp.json()["project_id"])

    onboarding = client.post("/api/generate/stock", json={
        "script": SCRIPT, "aspect_ratio": "9:16", "caption_enabled": True, "caption_style": "viral",
        "music_enabled": True, "music_genre": "lofi",
    })
    assert onboarding.status_code == 200 and onboarding.json()["job_id"]
    run_jobs(env)
    p2 = project_of(client, onboarding.json())
    s2 = p2["doc"]["settings"]
    assert s2["voice"]["provider"] == "edge_tts" and s2["voice"]["voice"]          # the free voice's default
    # The untouched "lofi" default follows the scene moods ("calm" → ambient).
    assert s2["music"]["track"] == "ambient_calm_01.mp3"
    release(onboarding.json()["project_id"])


def test_a_lone_start_image_stays_on_the_one_shot_pipeline(client, env, tmp_path):
    still = str(tmp_path / "start.png")
    r = client.post("/api/generate/stock", json={"script": SCRIPT, "start_image": still})
    assert r.status_code == 200 and r.json() == {"job_id": "legacy-job"}
    assert env["legacy"] and env["legacy"][-1]["start_image"] == still
    assert not env["runners"]


def test_a_start_image_with_user_images_goes_to_the_studio(client, env, tmp_path):
    r = client.post("/api/generate/stock", json={"script": SCRIPT, "start_image": str(tmp_path / "s.png"),
                                                 "user_images": [str(tmp_path / "gone.png")]})
    assert r.status_code == 200 and set(r.json()) == {"job_id", "project_id"}
    assert not env["legacy"]
    run_jobs(env)
    release(r.json()["project_id"])


def test_a_planning_failure_fails_the_job_and_releases_the_claim(client, env, monkeypatch):
    from backend.services import studio_planner

    async def boom(*_a, **_k):
        raise RuntimeError("stock search exploded with /private/path details")
    monkeypatch.setattr(studio_planner, "plan_project", boom)
    r = client.post("/api/generate/stock", json={"script": SCRIPT})
    assert r.status_code == 200, r.text
    run_jobs(env)
    job = job_row(r.json()["job_id"])
    assert job.status == "failed" and "/private" not in (job.error_message or "")
    assert not env["renders"]
    assert project_of(client, r.json())["status"] != "rendering"          # the claim is released
    failed = [m for m in env["sent"] if m.get("type") == "job_failed"]
    assert failed and failed[-1]["job_type"] == "generate"


def test_a_script_that_does_not_fit_is_refused_before_any_job(client, env):
    long_script = " ".join(("word " * 79).strip() + "." for _ in range(42))   # > studio_doc.SCENE_MAX (40) full scenes
    before = len(client.get("/api/studio/projects", params={"limit": 200}).json()["items"])
    r = client.post("/api/generate/stock", json={"script": long_script})
    assert r.status_code == 400
    assert not env["runners"]
    assert len(client.get("/api/studio/projects", params={"limit": 200}).json()["items"]) == before


def test_none_turns_captions_off_and_an_unknown_style_is_never_stored(client, env):
    off = client.post("/api/generate/stock", json={"script": SCRIPT, "caption_style": "none"})
    assert off.status_code == 200, off.text
    s = project_of(client, off.json())["doc"]["settings"]
    assert s["captions"]["enabled"] is False and s["captions"]["style"] != "none"
    env["runners"].pop().close()
    release(off.json()["project_id"])

    # Captions on: an unknown id is refused before any job.
    r = client.post("/api/generate/stock", json={"script": SCRIPT, "caption_style": "sparkles-9000"})
    assert r.status_code == 400 and "sparkles-9000" in r.json()["detail"]
    assert not env["runners"]

    # Captions off: the style is never read, so the unknown id is not stored —
    # the project keeps a real style that renders as itself if turned back on.
    quiet = client.post("/api/generate/stock", json={"script": SCRIPT, "caption_enabled": False,
                                                     "caption_style": "sparkles-9000"})
    assert quiet.status_code == 200, quiet.text
    s = project_of(client, quiet.json())["doc"]["settings"]
    assert s["captions"]["enabled"] is False and s["captions"]["style"] == "viral"
    env["runners"].pop().close()
    release(quiet.json()["project_id"])


def test_a_second_request_cannot_render_a_project_twice(client, env):
    """The adapter claims the project it just made, so the render it dispatched
    owns it; a Studio render of the same project meanwhile is refused."""
    r = client.post("/api/generate/stock", json={"script": SCRIPT})
    pid = r.json()["project_id"]
    again = client.post(f"/api/studio/projects/{pid}/render")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_rendering"
    env["runners"].pop().close()
    release(pid)


def test_the_source_is_recorded_on_the_project_and_the_job(client, env):
    from backend.database import AsyncSessionLocal
    from backend.models.downloaded_video import DownloadedVideo

    async def add_source():
        async with AsyncSessionLocal() as db:
            src = DownloadedVideo(title="Source clip", video_path="/nowhere/source.mp4")
            db.add(src)
            await db.commit()
            await db.refresh(src)
            return src.id
    source_id = asyncio.run(add_source())
    r = client.post("/api/generate/stock", json={"script": SCRIPT, "source_id": source_id})
    assert r.status_code == 200, r.text
    assert json.loads(job_row(r.json()["job_id"]).input_json)["source_id"] == source_id
    assert project_of(client, r.json())["doc"]["input"]["source_id"] == source_id
    env["runners"].pop().close()
    release(r.json()["project_id"])

    missing = client.post("/api/generate/stock", json={"script": SCRIPT, "source_id": "no-such-source"})
    assert missing.status_code == 404 and not env["runners"]


# ── the caller's own stills ────────────────────────────────────────────────

def _user_clip_scenes(project):
    return [sc for sc in project["doc"]["scenes"] if sc["visual"]["kind"] == "user_clip"]


@needs_ffmpeg
def test_user_images_are_imported_and_bound_pinned_to_the_opening_scenes_in_order(client, env, tmp_path):
    a, b = tiny_png(tmp_path / "first.png", "red"), tiny_png(tmp_path / "second.png", "blue")
    r = client.post("/api/generate/stock", json={"script": SCRIPT, "user_images": [str(a), str(b)]})
    assert r.status_code == 200, r.text
    run_jobs(env)
    p = project_of(client, r.json())
    scenes = p["doc"]["scenes"]
    assert len(scenes) >= 3
    first, second = scenes[0]["visual"], scenes[1]["visual"]
    assert first["kind"] == second["kind"] == "user_clip"
    assert first["pinned"] is True and second["pinned"] is True        # nothing re-plans them
    titles = {m["asset_id"]: m["title"] for m in p["media"]}
    assert titles[first["asset"]["asset_id"]] == "first.png" and titles[second["asset"]["asset_id"]] == "second.png"
    assert all(m["media"] == "image" for m in p["media"]) and len(p["media"]) == 2
    assert scenes[2]["visual"]["kind"] == "stock"                       # the rest stay on stock
    assert a.is_file() and b.is_file()                                  # the caller's files are never consumed
    assert not skipped(env)
    assert env["renders"] and env["renders"][-1][1] == r.json()["project_id"]
    release(r.json()["project_id"])


@needs_ffmpeg
def test_an_unreadable_or_missing_image_is_skipped_with_a_warning_and_the_others_kept(client, env, tmp_path):
    good = tiny_png(tmp_path / "good.png")
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image at all")
    r = client.post("/api/generate/stock", json={
        "script": SCRIPT, "user_images": [str(broken), str(tmp_path / "missing.png"), str(good)]})
    assert r.status_code == 200, r.text
    run_jobs(env)
    p = project_of(client, r.json())
    bound = _user_clip_scenes(p)
    assert len(bound) == 1 and bound[0]["uid"] == p["doc"]["scenes"][0]["uid"]   # the first open scene
    assert [m["title"] for m in p["media"]] == ["good.png"]
    warns = skipped(env)
    assert len(warns) == 1 and warns[0].startswith("2 of your images")
    assert env["renders"]                                                       # still rendered — never fatal
    release(r.json()["project_id"])


@needs_ffmpeg
def test_images_beyond_the_scene_count_are_kept_in_media_and_say_so(client, env, tmp_path):
    images = [str(tiny_png(tmp_path / f"img{i}.png")) for i in range(5)]
    r = client.post("/api/generate/stock", json={"script": SCRIPT, "user_images": images})
    assert r.status_code == 200, r.text
    run_jobs(env)
    p = project_of(client, r.json())
    n = len([sc for sc in p["doc"]["scenes"] if sc["line"]])
    assert n < 5
    assert len(_user_clip_scenes(p)) == n                                      # no scene takes two
    assert len(p["media"]) == 5                                                # extras stay in the project
    warns = skipped(env)
    assert len(warns) == 1 and f"{5 - n} of your images weren't used" in warns[0]
    release(r.json()["project_id"])


@needs_ffmpeg
def test_an_uploaded_media_reference_resolves_under_the_upload_dir_only(client, env, tmp_path):
    from backend.config import settings
    settings.TMP_DIR.mkdir(parents=True, exist_ok=True)
    name = "adapter_upload_test.png"
    tiny_png(settings.TMP_DIR / name)
    try:
        r = client.post("/api/generate/stock", json={
            "script": SCRIPT, "user_images": [f"/api/media/{name}", f"/api/media/../../{name}x"]})
        assert r.status_code == 200, r.text
        run_jobs(env)
        p = project_of(client, r.json())
        assert len(_user_clip_scenes(p)) == 1 and [m["title"] for m in p["media"]] == [name]
        assert skipped(env) and skipped(env)[0].startswith("1 of your image ")
    finally:
        (settings.TMP_DIR / name).unlink(missing_ok=True)
    release(r.json()["project_id"])


def test_without_user_images_nothing_is_imported_or_warned(client, env):
    r = client.post("/api/generate/stock", json={"script": SCRIPT})
    run_jobs(env)
    p = project_of(client, r.json())
    assert not _user_clip_scenes(p) and p["media"] == [] and not skipped(env)
    release(r.json()["project_id"])
