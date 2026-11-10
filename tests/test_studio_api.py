# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""/api/studio — projects, edits, render checks and voice takes.

A FastAPI app with only the studio router, on the suite's isolated DATA_DIR
(tests/conftest.py). The synthesiser is faked and the job dispatcher captured,
so the voice runner is driven explicitly. No network, no Whisper.
"""
import asyncio
import hashlib

import pytest

SCRIPT = "Most people save money the wrong way. They wait until the end of the month. Flip it and pay yourself first."
OPENAI = {"provider": "openai_tts", "voice": "nova", "label": "Nova"}
EDGE = {"provider": "edge_tts", "voice": "en-US-AndrewMultilingualNeural", "label": "Andrew"}



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
def captured(monkeypatch, tmp_path):
    """Fake synthesis + capture dispatched runners instead of running them."""
    from backend.core import task_runner
    from backend.services import voice_take_service as vts

    runners = []
    monkeypatch.setattr(task_runner, "dispatch_light", runners.append)
    monkeypatch.setattr(task_runner, "dispatch", runners.append)
    monkeypatch.setattr(vts, "cache_dir", lambda: tmp_path / "voice")
    monkeypatch.setattr(vts, "probe_audio_duration", lambda _p: 1.8)
    monkeypatch.setattr(vts, "_trim_edges", lambda _p: None)

    async def no_words(path, text):
        return None

    monkeypatch.setattr(vts, "_word_timings", no_words)
    calls = []

    async def fake_synth(text, spec, out):
        calls.append(text)
        out.write_bytes(b"ID3-take")

    monkeypatch.setattr(vts, "_synthesise", fake_synth)
    return runners, calls


def create(client, script=SCRIPT, **extra):
    r = client.post("/api/studio/projects", json={"start": {"mode": "script", "script": script, **extra}})
    assert r.status_code == 200, r.text
    return r.json()["project"]


def patch(client, pid, rev, *ops):
    return client.patch(f"/api/studio/projects/{pid}", json={"if_revision": rev, "ops": list(ops)})


def test_create_and_read_a_project(client):
    p = create(client, title="Money habits")
    assert p["title"] == "Money habits" and p["revision"] == 1 and p["status"] == "draft"
    assert len(p["doc"]["scenes"]) == 3
    assert all(s["visual"]["kind"] == "stock" and s["visual"]["query"] for s in p["doc"]["scenes"])
    assert p["derived"]["total"] > 0
    assert set(p["checks"]) == {"blockers", "warnings"} and p["checks"]["blockers"] == []
    assert any("voiced yet" in w for w in p["checks"]["warnings"])        # unvoiced → voiced at render
    assert not {"quote", "balance_cents", "stored_keys"} & set(p)
    s = p["doc"]["settings"]
    assert s["captions"]["style"] == "urban" and s["captions"]["hook"] is True
    assert s["voice"]["provider"] == "edge_tts"
    got = client.get(f"/api/studio/projects/{p['id']}").json()["project"]
    assert got["doc"] == p["doc"]


def test_create_refuses_empty_and_oversized_scripts(client):
    assert client.post("/api/studio/projects", json={"start": {"script": "  "}}).status_code == 400
    huge = " ".join(("word " * 70).strip() + "." for _ in range(30))
    r = client.post("/api/studio/projects", json={"start": {"script": huge}})
    assert r.status_code == 400 and "too long" in r.json()["detail"]["message"]


def test_edits_are_revision_checked(client):
    p = create(client)
    uid = p["doc"]["scenes"][0]["uid"]
    r = patch(client, p["id"], 1, {"op": "set_line", "uid": uid, "line": "A brand new opening."})
    assert r.status_code == 200
    body = r.json()
    assert body["project"]["revision"] == 2 and body["changes"][0]["op"] == "set_line"
    stale = patch(client, p["id"], 1, {"op": "set_title", "title": "Lost update"})
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "revision_conflict" and detail["project"]["revision"] == 2


def test_bad_edits_are_400_and_change_nothing(client):
    p = create(client)
    uid = p["doc"]["scenes"][0]["uid"]
    r = patch(client, p["id"], 1, {"op": "voice_result", "uid": uid, "voice": {"state": "ready"}})
    assert r.status_code == 400 and "own endpoint" in r.json()["detail"]["message"]
    forged = p["doc"] | {"scenes": [p["doc"]["scenes"][0] | {"voice": {
        "state": "ready", "take_key": hashlib.sha256(b"x").hexdigest(), "duration": 2}}]}
    r = patch(client, p["id"], 1, {"op": "replace_doc", "doc": forged})
    assert r.status_code == 400 and "unknown voice take" in r.json()["detail"]["message"]
    assert client.get(f"/api/studio/projects/{p['id']}").json()["project"]["revision"] == 1


def test_an_empty_scene_is_a_render_blocker_in_the_envelope(client):
    p = create(client)
    r = patch(client, p["id"], 1, {"op": "insert_scene", "after_uid": None, "line": ""})
    assert r.status_code == 200, r.text
    blockers = r.json()["project"]["checks"]["blockers"]
    assert blockers and "no line yet" in blockers[0]


def test_list_and_delete(client):
    p = create(client, title="Deletable")
    items = client.get("/api/studio/projects", params={"q": "Deletable"}).json()["items"]
    assert any(i["id"] == p["id"] for i in items)
    assert client.delete(f"/api/studio/projects/{p['id']}").json() == {"ok": True}
    assert client.get(f"/api/studio/projects/{p['id']}").status_code == 404
    assert client.delete(f"/api/studio/projects/{p['id']}").status_code == 404


def test_voice_is_recorded_once_and_served(client, captured):
    runners, calls = captured
    p = create(client)

    ok = client.post(f"/api/studio/projects/{p['id']}/voice", json={"scene_uids": "all"})
    assert ok.status_code == 200 and ok.json()["job_id"]
    assert {s["voice"]["state"] for s in ok.json()["project"]["doc"]["scenes"]} == {"generating"}
    assert any("still being voiced" in b for b in ok.json()["project"]["checks"]["blockers"])

    again = client.post(f"/api/studio/projects/{p['id']}/voice", json={"scene_uids": "all"})
    assert again.json()["job_id"] is None                # already generating — single-flight

    asyncio.run(runners.pop(0))
    done = client.get(f"/api/studio/projects/{p['id']}").json()["project"]
    voices = [s["voice"] for s in done["doc"]["scenes"]]
    assert all(v["state"] == "ready" and v["duration"] == 1.8 for v in voices)
    assert len(calls) == 3
    assert done["checks"]["blockers"] == []
    assert not any("voice" in w for w in done["checks"]["warnings"])

    audio = client.get(f"/api/studio/voice/{voices[0]['take_key']}")
    assert audio.status_code == 200 and audio.content == b"ID3-take"
    assert client.get(f"/api/studio/voice/{voices[0]['take_key']}/meta").json()["duration"] == 1.8
    assert client.get("/api/studio/voice/not-a-key").status_code == 404

    # Change the voice and back: the takes are simply the scenes' own again.
    rev = done["revision"]
    other = {**EDGE, "voice": "en-US-AvaMultilingualNeural", "label": "Ava"}
    rev = patch(client, p["id"], rev, {"op": "set_setting", "path": "voice", "value": other}).json()["project"]["revision"]
    back = patch(client, p["id"], rev, {"op": "set_setting", "path": "voice", "value": EDGE}).json()["project"]
    assert {s["voice"]["state"] for s in back["doc"]["scenes"]} == {"ready"}

    # Edit a line and undo the edit by hand: its take is cached, so re-voicing records nothing.
    uid, line = back["doc"]["scenes"][0]["uid"], back["doc"]["scenes"][0]["line"]
    rev = patch(client, p["id"], back["revision"], {"op": "set_line", "uid": uid, "line": "Something else."}).json()["project"]["revision"]
    edited = patch(client, p["id"], rev, {"op": "set_line", "uid": uid, "line": line}).json()["project"]
    assert edited["doc"]["scenes"][0]["voice"]["state"] == "stale"
    again = client.post(f"/api/studio/projects/{p['id']}/voice", json={"scene_uids": "all"})
    assert again.status_code == 200
    asyncio.run(runners.pop(0))
    assert len(calls) == 3


def test_a_keyed_voice_without_its_key_is_refused_up_front(client, captured, monkeypatch):
    from backend.config import settings
    runners, calls = captured
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    p = create(client)
    p = patch(client, p["id"], 1, {"op": "set_setting", "path": "voice", "value": OPENAI}).json()["project"]
    r = client.post(f"/api/studio/projects/{p['id']}/voice", json={"scene_uids": "all"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "needs_key"
    assert "OPENAI_API_KEY" in r.json()["detail"]["message"]
    after = client.get(f"/api/studio/projects/{p['id']}").json()["project"]
    assert not runners and not calls
    assert "generating" not in {s["voice"]["state"] for s in after["doc"]["scenes"]}


def test_a_keyed_voice_records_once_its_key_is_set(client, captured, monkeypatch):
    from backend.config import settings
    runners, calls = captured
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    p = create(client)
    p = patch(client, p["id"], 1, {"op": "set_setting", "path": "voice", "value": OPENAI}).json()["project"]
    r = client.post(f"/api/studio/projects/{p['id']}/voice", json={"scene_uids": "all"})
    assert r.status_code == 200 and r.json()["job_id"]
    asyncio.run(runners.pop(0))
    done = client.get(f"/api/studio/projects/{p['id']}").json()["project"]
    assert {s["voice"]["state"] for s in done["doc"]["scenes"]} == {"ready"} and len(calls) == 3


def test_a_take_that_finishes_after_the_line_changed_does_not_land(client, captured):
    runners, _calls = captured
    p = create(client, script="One line to voice. Another line here.")
    uid = p["doc"]["scenes"][0]["uid"]
    r = client.post(f"/api/studio/projects/{p['id']}/voice", json={"scene_uids": [uid]})
    assert r.status_code == 200
    rev = r.json()["project"]["revision"]
    patch(client, p["id"], rev, {"op": "set_line", "uid": uid, "line": "The line changed while recording."})
    asyncio.run(runners.pop(0))
    scene = client.get(f"/api/studio/projects/{p['id']}").json()["project"]["doc"]["scenes"][0]
    assert scene["voice"]["state"] != "ready"


def test_a_write_that_changes_nothing_keeps_the_revision(client):
    """A blur right after ⌘↵ re-sends the same line: valid, but not a new
    revision — otherwise every such echo is a phantom undo step."""
    p = create(client)
    uid = p["doc"]["scenes"][0]["uid"]
    same = patch(client, p["id"], 1, {"op": "set_line", "uid": uid, "line": p["doc"]["scenes"][0]["line"]})
    assert same.status_code == 200 and same.json()["changes"] == [] and same.json()["project"]["revision"] == 1
    changed = patch(client, p["id"], 1, {"op": "set_line", "uid": uid, "line": "Something new."})
    assert changed.json()["project"]["revision"] == 2 and changed.json()["changes"]


def test_the_poll_gets_a_cheap_unchanged_answer_until_the_revision_moves(client):
    p = client.post("/api/studio/projects", json={"start": {"script": "One line only here."}}).json()["project"]
    same = client.get(f"/api/studio/projects/{p['id']}?known_revision={p['revision']}").json()
    assert same == {"unchanged": True, "revision": p["revision"], "status": "draft", "render_alive": False}
    client.patch(f"/api/studio/projects/{p['id']}", json={"if_revision": p["revision"], "ops": [
        {"op": "set_title", "title": "Renamed"}]})
    moved = client.get(f"/api/studio/projects/{p['id']}?known_revision={p['revision']}").json()
    assert "project" in moved and moved["project"]["title"] == "Renamed"
    assert client.get("/api/studio/projects/nope?known_revision=1").status_code == 404


def test_an_unsupported_voice_provider_gives_way_to_the_free_default(client):
    r = client.post("/api/studio/projects", json={"start": {"script": "One line here."},
                                                  "settings": {"voice": {"provider": "voxcpm", "voice": "v1",
                                                                         "label": "Gone"}}})
    assert r.status_code == 200, r.text
    voice = r.json()["project"]["doc"]["settings"]["voice"]
    assert voice["provider"] == "edge_tts" and voice["voice"] != "v1" and voice["label"] != "Gone"
