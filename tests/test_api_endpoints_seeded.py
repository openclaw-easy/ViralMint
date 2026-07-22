# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Seeded API suite — reaches the router handler bodies the parameter-free
smoke suite (test_api_smoke.py) can't touch.

We boot the real FastAPI app against a throwaway SQLite DB, SEED one row per
model behind a path-param route, then exercise:
  - path-param GET routes (jobs/{id}, videos/{id}, downloaded/{id},
    chat/sessions/{id}/messages, scout/results/{id}, channels/videos/{id})
  - cheap, side-effect-safe POST/DELETE/PATCH/PUT routes (settings save,
    template delete, caption-style CRUD, chat-session CRUD, row deletes).

As with the smoke suite this asserts only that each handler runs WITHOUT a 5xx
(a handler crash). No network- or AI-bound route is exercised, so the suite
stays hermetic and fast. A route that 5xxes on a genuine OSS bug is dropped
(and noted in the module) rather than patched — this file never touches source.

Seeding runs via asyncio.run() + engine.dispose() BEFORE the TestClient is
entered so the module-level async engine pool never shares a connection across
event loops (the TestClient's portal loop reopens fresh connections).
"""
import asyncio
import os
import tempfile
from pathlib import Path

# Throwaway data dir BEFORE backend.config is imported.
_TMP = Path(tempfile.mkdtemp(prefix="vm-seed-"))
os.environ["VIRALMINT_DATA_DIR"] = str(_TMP)
os.environ.setdefault("DEBUG", "false")

import pytest
from starlette.testclient import TestClient

from backend.main import create_app
from backend.messaging import manager as messaging_manager


async def _seed() -> dict:
    """Create tables + insert one row per path-param model. Returns captured ids.

    Disposes the engine at the end so no pooled connection outlives this loop.
    """
    from backend.database import engine, AsyncSessionLocal, Base
    from backend.models.job import Job
    from backend.models.generated_video import GeneratedVideo
    from backend.models.downloaded_video import DownloadedVideo
    from backend.models.scout_result import ScoutResult
    from backend.models.chat_session import ChatSession, ChatMessage
    from backend.models.dynamic_template import DynamicTemplate
    from backend.models.connected_channel import ConnectedChannel

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    ids: dict = {}
    async with AsyncSessionLocal() as db:
        # Jobs — one to GET, one (terminal) to DELETE.
        job_get = Job(job_type="generate", status="success", title="seed job")
        job_del = Job(job_type="generate", status="success", title="seed job del")
        db.add_all([job_get, job_del])

        # Generated videos — GET / PATCH target + DELETE target.
        gv_get = GeneratedVideo(title="seed video", status="ready", niche="testniche")
        gv_del = GeneratedVideo(title="seed video del", status="draft")
        db.add_all([gv_get, gv_del])

        # Downloaded videos — GET target + DELETE target.
        dv_get = DownloadedVideo(title="seed dl", platform="import")
        dv_del = DownloadedVideo(title="seed dl del", platform="import")
        db.add_all([dv_get, dv_del])

        # Scout results — GET target + DELETE target (platform/video_id/url NN).
        sr_get = ScoutResult(platform="youtube", video_id="vid_get",
                             video_url="https://example.com/get", title="seed scout")
        sr_del = ScoutResult(platform="youtube", video_id="vid_del",
                             video_url="https://example.com/del", title="seed scout del")
        db.add_all([sr_get, sr_del])

        # Chat session (+ one message) for the messages route.
        session_get = ChatSession(title="seed session")
        db.add(session_get)
        await db.flush()
        db.add(ChatMessage(session_id=session_get.id, role="user", content="hello"))

        # Dynamic template — DELETE target; also visible in GET /templates filter.
        from datetime import datetime, timedelta
        tmpl_del = DynamicTemplate(
            mode="stock", niche="testniche", name="Seed Template",
            description="a seeded template", icon="🔥",
            tags_json='["trending"]', defaults_json='{"aspectRatio": "9:16"}',
            trend_source="search_demand", trend_score=50.0,
            expires_at=datetime.utcnow() + timedelta(days=7),
        )
        db.add(tmpl_del)

        # Connected channel (youtube, no API key configured → handler 400s, <500).
        ch_get = ConnectedChannel(platform="youtube", channel_id="UCseed",
                                 channel_url="https://youtube.com/@seed",
                                 channel_name="Seed Channel")
        db.add(ch_get)

        await db.flush()
        ids.update(
            job_get=job_get.id, job_del=job_del.id,
            gv_get=gv_get.id, gv_del=gv_del.id,
            dv_get=dv_get.id, dv_del=dv_del.id,
            sr_get=sr_get.id, sr_del=sr_del.id,
            session_get=session_get.id,
            tmpl_del=tmpl_del.id,
            channel_get=ch_get.id,
        )
        await db.commit()

    await engine.dispose()
    return ids


@pytest.fixture(scope="module")
def client_and_ids():
    async def _noop(*a, **k):
        return None

    messaging_manager.messaging.start_all = _noop  # type: ignore[assignment]
    messaging_manager.messaging.stop_all = _noop  # type: ignore[assignment]

    ids = asyncio.run(_seed())

    app = create_app()
    with TestClient(app) as c:
        yield c, ids


@pytest.fixture(scope="module")
def client(client_and_ids):
    return client_and_ids[0]


@pytest.fixture(scope="module")
def ids(client_and_ids):
    return client_and_ids[1]


def _ok(resp):
    return resp.status_code < 500


# ── jobs.py ──────────────────────────────────────────────────────────────────

def test_get_job(client, ids):
    r = client.get(f"/api/jobs/{ids['job_get']}")
    assert r.status_code == 200
    assert r.json()["id"] == ids["job_get"]


def test_get_job_missing_404(client):
    r = client.get("/api/jobs/does-not-exist")
    assert r.status_code == 404


def test_delete_job(client, ids):
    r = client.delete(f"/api/jobs/{ids['job_del']}")
    assert _ok(r)


# ── videos.py ────────────────────────────────────────────────────────────────

def test_get_video(client, ids):
    r = client.get(f"/api/videos/{ids['gv_get']}")
    assert r.status_code == 200
    assert r.json()["id"] == ids["gv_get"]


def test_patch_video_metadata(client, ids):
    r = client.patch(f"/api/videos/{ids['gv_get']}", json={"title": "renamed"})
    assert _ok(r)


def test_get_video_thumbnail_missing(client, ids):
    # No thumbnail on disk → 404, but the handler must run cleanly.
    r = client.get(f"/api/videos/{ids['gv_get']}/thumbnail")
    assert _ok(r)


def test_delete_video(client, ids):
    r = client.delete(f"/api/videos/{ids['gv_del']}")
    assert _ok(r)


# ── downloaded.py ────────────────────────────────────────────────────────────

def test_get_downloaded(client, ids):
    r = client.get(f"/api/downloaded/{ids['dv_get']}")
    assert r.status_code == 200
    assert r.json()["id"] == ids["dv_get"]


def test_stream_downloaded_no_file(client, ids):
    # video_path is None → 404; handler runs.
    r = client.get(f"/api/downloaded/{ids['dv_get']}/stream")
    assert _ok(r)


def test_delete_downloaded(client, ids):
    r = client.delete(f"/api/downloaded/{ids['dv_del']}")
    assert _ok(r)


# ── scout.py (results) ───────────────────────────────────────────────────────

def test_get_scout_result(client, ids):
    r = client.get(f"/api/scout/results/{ids['sr_get']}")
    assert r.status_code == 200
    assert r.json()["id"] == ids["sr_get"]


def test_delete_scout_result(client, ids):
    r = client.delete(f"/api/scout/results/{ids['sr_del']}")
    assert _ok(r)


# ── chat_sessions.py ─────────────────────────────────────────────────────────

def test_list_session_messages(client, ids):
    r = client.get(f"/api/chat/sessions/{ids['session_get']}/messages")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) >= 1


def test_chat_session_crud(client):
    # create → add message → list → rename → delete, all side-effect-safe.
    created = client.post("/api/chat/sessions", json={"title": "crud session"})
    assert created.status_code == 201
    sid = created.json()["id"]

    msg = client.post(f"/api/chat/sessions/{sid}/messages",
                      json={"role": "user", "content": "hi there"})
    assert _ok(msg)

    listed = client.get(f"/api/chat/sessions/{sid}/messages")
    assert listed.status_code == 200

    renamed = client.put(f"/api/chat/sessions/{sid}", json={"title": "renamed"})
    assert _ok(renamed)

    deleted = client.delete(f"/api/chat/sessions/{sid}")
    assert deleted.status_code == 204


# ── templates.py ─────────────────────────────────────────────────────────────

def test_list_templates_filtered(client):
    # Exercises the tags/defaults json.loads serialization branch.
    r = client.get("/api/templates?mode=stock&niche=testniche")
    assert r.status_code == 200
    assert "templates" in r.json()


def test_delete_template(client, ids):
    r = client.delete(f"/api/templates/{ids['tmpl_del']}")
    assert _ok(r)


# ── captions.py ──────────────────────────────────────────────────────────────

def test_caption_style_crud(client):
    created = client.post("/api/captions/styles", json={"name": "Seed Style"})
    assert created.status_code == 201
    cid = created.json()["id"]

    updated = client.put(f"/api/captions/styles/{cid}", json={"name": "Renamed Style"})
    assert _ok(updated)

    deleted = client.delete(f"/api/captions/styles/{cid}")
    assert _ok(deleted)


def test_create_caption_style_empty_name_422(client):
    r = client.post("/api/captions/styles", json={"name": "   "})
    assert r.status_code == 422


# ── settings.py ──────────────────────────────────────────────────────────────

def test_save_settings(client):
    # Preference-only body — no secrets, no network.
    r = client.post("/api/settings", json={
        "caption_style": "bold",
        "music_genre": "cinematic",
        "caption_enabled": True,
    })
    assert r.status_code == 200
    assert r.json()["caption_style"] == "bold"


# ── channels.py ──────────────────────────────────────────────────────────────

def test_get_channel_videos(client, ids):
    # Seeded youtube channel + no API key → 400 from the handler (still <500).
    r = client.get(f"/api/channels/videos/{ids['channel_get']}")
    assert _ok(r)


def test_get_channel_videos_missing_404(client):
    r = client.get("/api/channels/videos/does-not-exist")
    assert r.status_code == 404
