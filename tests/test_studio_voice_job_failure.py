# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""`run_studio_voice` waits for every take before it reports a failure.

It used a plain `asyncio.gather`: the first exception out of one scene ended
the job at once while its siblings kept recording behind a job already
reported failed, and `finally` settled scenes that were still being recorded.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

SCRIPT = ("A jar of coins sits on a wooden table in soft morning light by the window. "
          "A hand reaches in and drops one more shiny coin into the jar. "
          "The jar is carried to the bank on a rainy Monday morning in spring.")


@pytest.fixture
def voiced(monkeypatch):
    """A three-scene project, every scene marked generating, takes faked:
    scene 1 records at once and its landing write fails; later scenes take a moment."""
    import backend.agents.job_helper as jh
    from backend.core.tasks import studio as tasks
    from backend.core.ws_manager import ws_manager
    from backend.database import init_db
    from backend.services import studio_store as store
    from backend.services import voice_take_service as vts

    asyncio.run(init_db())
    row = asyncio.run(store.create_project(script=SCRIPT))
    doc = json.loads(row.doc_json)
    uids = [s["uid"] for s in doc["scenes"]]
    assert len(uids) == 3
    asyncio.run(store.write_ops(row.id, [
        {"op": "voice_result", "uid": u, "voice": {"state": "generating"}} for u in uids
    ], if_revision=None, internal=True))
    first_line = doc["scenes"][0]["line"]
    state = {"id": row.id, "uids": uids, "takes": [], "ended": []}

    async def ensure_take(spec, line, **_kw):
        if line != first_line:
            await asyncio.sleep(0.05)
        state["takes"].append(line)
        key = hashlib.sha256(line.encode()).hexdigest()
        path, meta = vts._paths(key)                     # the doc gate asks that a ready scene's take exists
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ID3 a take")
        meta.write_text("{}")
        return vts.Take(key=key, path=path, duration=2.0, cached=False)

    real_write = store.write_ops

    async def write_ops(project_id, ops, **kw):
        if any(o.get("uid") == uids[0] and (o.get("voice") or {}).get("state") == "ready" for o in ops):
            raise RuntimeError("database is locked")
        return await real_write(project_id, ops, **kw)

    async def end(job_id, job_type, status, user_id, **kw):
        state["ended"].append((status, len(state["takes"]), kw))

    monkeypatch.setattr(vts, "ensure_take", ensure_take)
    monkeypatch.setattr(store, "write_ops", write_ops)
    monkeypatch.setattr(tasks, "_end_job", end)
    monkeypatch.setattr(ws_manager, "send", AsyncMock())
    monkeypatch.setattr(jh, "update_job_status", AsyncMock())
    monkeypatch.setattr(jh, "job_cancelled", AsyncMock(return_value=False))
    return state


def _scenes(project_id):
    from backend.services import studio_store as store
    return json.loads(asyncio.run(store.get_project(project_id)).doc_json)["scenes"]


def test_a_failure_waits_for_the_takes_in_flight_and_lands_them(voiced, monkeypatch):
    from backend.core.tasks import studio as tasks
    from backend.services import voice_take_service as vts
    monkeypatch.setattr(vts, "VOICE_CONCURRENCY", 3)             # all three in flight together

    asyncio.run(tasks.run_studio_voice("job-v", voiced["id"], list(voiced["uids"]), "local"))

    (status, takes_done, kw), = voiced["ended"]                  # reported ONCE…
    assert status == "failed" and "database is locked" in kw["error"]
    assert takes_done == 3                                       # …after every take in flight finished
    assert kw["output"]["voiced"] == 2 and kw["output"]["failed"] == 0
    scenes = _scenes(voiced["id"])
    assert [s["voice"]["state"] for s in scenes[1:]] == ["ready", "ready"]   # the siblings' takes landed
    assert scenes[0]["voice"]["state"] != "generating"                       # no spinner left behind


def test_a_failure_stops_the_takes_that_have_not_started(voiced, monkeypatch):
    from backend.core.tasks import studio as tasks
    from backend.services import voice_take_service as vts
    monkeypatch.setattr(vts, "VOICE_CONCURRENCY", 1)             # one at a time: 2 and 3 are still queued

    asyncio.run(tasks.run_studio_voice("job-v", voiced["id"], list(voiced["uids"]), "local"))

    (status, takes_done, kw), = voiced["ended"]
    assert status == "failed" and takes_done == 1                # nothing new was recorded for a job that failed
    assert all(s["voice"]["state"] == "none" for s in _scenes(voiced["id"]))
