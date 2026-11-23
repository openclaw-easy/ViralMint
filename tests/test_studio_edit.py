# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""studio_edit: one sentence → validated ops on the SAME project. AI output
never reaches the document unchecked. The AI client is faked."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.services import studio_doc as sd
from backend.services import studio_edit

U = "edit-test"


class AI:
    def __init__(self, reply):
        self.reply = reply

    async def chat(self, messages, **kw):
        return self.reply


def _doc():
    return {"aspect_ratio": "9:16", "settings": sd.default_settings(),
            "scenes": [sd.make_scene(l) for l in ("Plants sleep in winter.", "Water less.", "Comment below.")]}


def test_only_valid_changes_become_ops(monkeypatch):
    from backend.services import caption_service, studio_media

    async def known(style):
        return style == "bold"

    async def search(pid, q, aspect):
        return [{"provider": "pexels", "provider_id": "77", "query": q}]

    monkeypatch.setattr(caption_service, "style_id_known", known)
    monkeypatch.setattr(studio_media, "search_candidates", search)
    doc = _doc()
    changes = [
        {"type": "line", "scene": 1, "text": "Your plants aren't dying. They're asleep."},
        {"type": "line", "scene": 9, "text": "no such scene"},
        {"type": "caption_style", "value": "bold"},
        {"type": "caption_style", "value": "made-up"},
        {"type": "look", "value": "neon"},
        {"type": "look", "value": "psychedelic"},
        {"type": "music_genre", "value": "upbeat"},
        {"type": "visual", "scene": 2, "query": "watering can close up"},
        {"type": "hook_text", "text": "Stop watering in winter"},
        {"type": "unknown"},
    ]
    ops, said = asyncio.run(studio_edit.to_ops("p", doc, changes))
    kinds = [(o["op"], o.get("path")) for o in ops]
    assert kinds == [("set_line", None), ("set_setting", "captions.style"), ("set_setting", "visual_style"),
                     ("set_setting", "music"), ("bind_visual", None), ("set_setting", "captions.hook_text")]
    assert ops[4]["visual"]["candidate"]["provider_id"] == "77" and len(said) == 6


def test_a_video_keeps_at_least_one_scene():
    doc = _doc()
    ops, _ = asyncio.run(studio_edit.to_ops("p", doc, [{"type": "remove", "scene": n} for n in (1, 2, 3)]))
    assert [o["op"] for o in ops] == ["remove_scene", "remove_scene"]


def test_unreadable_plans_are_a_sentence():
    with pytest.raises(studio_edit.EditError):
        asyncio.run(studio_edit.plan_changes(_doc(), "t", "x", AI("no json")))


def test_edit_project_writes_the_same_project(monkeypatch):
    from backend.database import init_db
    from backend.services import music_service
    from backend.services import studio_store as store

    async def no_track(*_a, **_k):
        return None
    monkeypatch.setattr(music_service, "select_music", no_track)
    asyncio.run(init_db())
    row = asyncio.run(store.create_project(script="Plants sleep in winter. Water less.", user_id=U))
    reply = json.dumps({"changes": [{"type": "line", "scene": 1, "text": "Your plants are asleep, not dying."},
                                    {"type": "title", "text": "Winter plant care"}],
                        "summary": "Punchier opening."})
    out = asyncio.run(studio_edit.edit_project(row.id, "make the hook punchier", user_id=U, ai_client=AI(reply)))
    after = asyncio.run(store.get_project(row.id, U))
    doc = json.loads(after.doc_json)
    assert doc["scenes"][0]["line"] == "Your plants are asleep, not dying."
    assert after.title == "Winter plant care" and after.id == row.id
    assert out["applied"] and out["summary"] == "Punchier opening." and out["project"]["id"] == row.id
    asyncio.run(store.delete_project(row.id, U))


def test_saved_settings_ignore_the_old_viral_default(monkeypatch):
    """A stored "viral" is the old column default nobody chose: it reads as unset."""
    import backend.database as database
    from backend.api import studio

    prefs = SimpleNamespace(tts_provider="openai_tts", preferred_tts_voice="nova", caption_style="viral",
                            caption_enabled=True)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, _stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: prefs)

    monkeypatch.setattr(database, "AsyncSessionLocal", Session)
    out = asyncio.run(studio.saved_settings())
    assert out["voice"] == {"provider": "openai_tts", "voice": "nova"}
    assert out["captions"] == {"enabled": True}


def test_the_video_ready_message_has_its_length():
    from backend.messaging.base import NotificationEvent
    from backend.messaging.manager import _build_payload
    p = _build_payload(NotificationEvent.VIDEO_GENERATED, {"title": "Plants", "duration_seconds": 31})
    assert "(31s)" in p.body and "Plants" in p.body


def test_a_render_claim_left_by_a_crash_does_not_lock_edit_in_words():
    """edit_project read the raw `rendering` status, so after a crash
    mid-render every edit was refused until someone rendered again. Every
    other door asks whether a render is actually alive."""
    from backend.database import init_db
    from backend.services import studio_store as store

    asyncio.run(init_db())
    row = asyncio.run(store.create_project(script="Plants sleep in winter. Water less.", user_id=U))
    asyncio.run(store.claim_render(row.id, U))
    store.release_render(row.id)                       # the process died: the claim is on the row, no job is alive
    assert asyncio.run(store.get_project(row.id, U)).status == "rendering"
    reply = json.dumps({"changes": [{"type": "title", "text": "Winter plant care"}], "summary": "Renamed."})
    out = asyncio.run(studio_edit.edit_project(row.id, "rename it", user_id=U, ai_client=AI(reply)))
    assert out["applied"] and asyncio.run(store.get_project(row.id, U)).title == "Winter plant care"
    asyncio.run(store.delete_project(row.id, U))


def test_a_live_render_still_refuses_edit_in_words():
    from backend.database import init_db
    from backend.services import studio_store as store

    asyncio.run(init_db())
    row = asyncio.run(store.create_project(script="Plants sleep in winter. Water less.", user_id=U))
    asyncio.run(store.claim_render(row.id, U))         # alive in this process
    try:
        with pytest.raises(studio_edit.EditError, match="rendering"):
            asyncio.run(studio_edit.edit_project(row.id, "rename it", user_id=U, ai_client=AI("{}")))
    finally:
        store.release_render(row.id)
        asyncio.run(store.finish_render(row.id, user_id=U))
        asyncio.run(store.delete_project(row.id, U))
