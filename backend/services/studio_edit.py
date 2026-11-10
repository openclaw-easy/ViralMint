# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Edit a Smart Video project from a sentence (the editor, MCP).

"Make the hook stronger" or "use drone footage for the city scenes" shouldn't
mean a whole new video. This module turns one instruction into the editor's
own ops on the EXISTING project:

  line         set_line (the scene's voice goes stale and is re-recorded)
  title        set_title
  hook_text    the opening title (captions.hook_text)
  caption_style / look / music_genre    set_setting
  visual       a stock search + bind_visual (pinned: the user asked for it)
  remove       remove_scene

One AI call (the user's key) plans the change; everything it proposes is
validated here before it becomes an op — AI output never reaches the document
unchecked. A re-render is its own step (POST …/render).
"""
from __future__ import annotations

import json
import logging

from backend.core.exceptions import StudioError
from backend.core.prompt_rules import extract_json_text
from backend.services import studio_doc as sd

logger = logging.getLogger(__name__)

MAX_CHANGES = 49     # every line of a 40-scene video, plus settings


def _music_genres() -> tuple[str, ...]:
    from backend.services.music_service import MUSIC_GENRES
    return tuple(MUSIC_GENRES)


class EditError(StudioError):
    """The instruction couldn't become edits — said to the user as a sentence."""


def _prompt(doc: dict, title: str, instruction: str) -> str:
    s = doc["settings"]
    scenes = "\n".join(
        f"{i + 1}. {sc['line']}   [visual: {sc['visual']['kind']}"
        + (f", footage of “{sc['visual'].get('query')}”" if sc["visual"].get("query") else "") + "]"
        for i, sc in enumerate(doc["scenes"]))
    return f"""You edit a short narrated video. Turn the creator's request into a list of changes.

Title: {title}
Look: {s.get('visual_style')} · captions: {s['captions'].get('style')} · music: {s['music'].get('genre')}
Opening title: {s['captions'].get('hook_text') or '(the first line)'}
Scenes (number. narration [visual]):
{scenes}

Creator's request: "{instruction.strip()[:500]}"

Allowed changes (use only what the request needs; never invent facts):
- {{"type": "line", "scene": N, "text": "new narration for scene N"}}  (same language as the script, under {sd.LINE_MAX} chars)
- {{"type": "visual", "scene": N, "query": "3-6 English words of stock footage to show"}}
- {{"type": "remove", "scene": N}}
- {{"type": "title", "text": "project title"}}
- {{"type": "hook_text", "text": "on-screen opening title, under {sd.HOOK_TEXT_MAX} chars"}}
- {{"type": "caption_style", "value": "a caption style id, e.g. pop, viral, bold, minimal, karaoke"}}
- {{"type": "look", "value": one of {list(sd.VISUAL_STYLES)}}}
- {{"type": "music_genre", "value": one of {list(_music_genres())}}}

"Hook", "opening" or "first line" means scene 1's NARRATION (a "line" change for scene 1) — change the
on-screen opening title only when they mention a title or on-screen text.

Reply with JSON only: {{"changes": [...], "summary": "one sentence saying what you changed"}}
If the request can't be done with these changes, reply {{"changes": [], "summary": "why, in one sentence"}}."""


def _int(v) -> int | None:
    try:
        return int(v) if not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


async def plan_changes(doc: dict, title: str, instruction: str, ai_client) -> tuple[list[dict], str]:
    try:
        answer = await ai_client.chat(messages=[{"role": "user", "content": _prompt(doc, title, instruction)}],
                                      max_tokens=4000)
        data = json.loads(extract_json_text(answer or ""))
    except Exception as e:  # noqa: BLE001 — a sentence, never the exception
        logger.warning("studio edit: planning failed: %s", e)
        raise EditError("The AI couldn't plan that change — try saying it another way.") from e
    if not isinstance(data, dict):
        raise EditError("The AI couldn't plan that change — try saying it another way.")
    changes = [c for c in (data.get("changes") or []) if isinstance(c, dict)][:MAX_CHANGES]
    return changes, sd.normalise_line(data.get("summary"))[:300]


async def to_ops(project_id: str, doc: dict, changes: list[dict]) -> tuple[list[dict], list[str]]:
    """Validated studio ops + one plain sentence per change actually made."""
    from backend.services import caption_service, studio_media

    scenes = doc["scenes"]
    ops: list[dict] = []
    said: list[str] = []
    removed: set[str] = set()

    def scene_at(n):
        k = _int(n)
        return scenes[k - 1] if k and 1 <= k <= len(scenes) else None

    for c in changes:
        kind = c.get("type")
        sc = scene_at(c.get("scene"))
        if kind == "line" and sc and sc["uid"] not in removed:
            text = sd.normalise_line(c.get("text"))[: sd.LINE_MAX]
            if text and text != sc["line"]:
                ops.append({"op": "set_line", "uid": sc["uid"], "line": text})
                said.append(f"Scene {scenes.index(sc) + 1}: “{text}”")
        elif kind == "visual" and sc and sc["uid"] not in removed:
            query = sd.normalise_line(c.get("query"))[:100]
            if len(query) < 2:
                continue
            try:
                found = await studio_media.search_candidates(project_id, query, doc["aspect_ratio"])
            except Exception as e:  # noqa: BLE001 — one missing search never sinks the rest
                logger.info("studio edit: search %r failed: %s", query, e)
                found = []
            used = {(s["visual"].get("asset") or {}).get("provider_id") for s in scenes}
            pick = next((f for f in found if f.get("provider_id") not in used), found[0] if found else None)
            if pick is None:
                said.append(f"Scene {scenes.index(sc) + 1}: no footage found for “{query}” — kept the old clip")
                continue
            ops.append({"op": "bind_visual", "uid": sc["uid"], "visual": {"kind": "stock", "candidate": pick, "query": query}})
            said.append(f"Scene {scenes.index(sc) + 1}: new footage of “{query}”")
        elif kind == "remove" and sc and len(scenes) - len(removed) > 1 and sc["uid"] not in removed:
            removed.add(sc["uid"])
            ops.append({"op": "remove_scene", "uid": sc["uid"]})
            said.append(f"Removed scene {scenes.index(sc) + 1}")
        elif kind == "title":
            text = sd.normalise_line(c.get("text"))[:120]
            if text:
                ops.append({"op": "set_title", "title": text})
                said.append(f"Title: “{text}”")
        elif kind == "hook_text":
            text = sd.normalise_hook_text(c.get("text"))
            if text:
                ops.append({"op": "set_setting", "path": "captions.hook_text", "value": text})
                said.append(f"Opening title: “{text}”")
        elif kind == "caption_style":
            value = str(c.get("value") or "").strip()
            if value and await caption_service.style_id_known(value):
                ops.append({"op": "set_setting", "path": "captions.style", "value": value})
                said.append(f"Captions: {value}")
        elif kind == "look" and c.get("value") in sd.VISUAL_STYLES:
            ops.append({"op": "set_setting", "path": "visual_style", "value": c["value"]})
            said.append(f"Look: {c['value']}")
        elif kind == "music_genre" and c.get("value") in _music_genres():
            ops.append({"op": "set_setting", "path": "music",
                        "value": {"enabled": True, "source": "library", "genre": c["value"], "track": None, "track_name": None}})
            said.append(f"Music: {c['value']}")
    return ops, said


async def edit_project(project_id: str, instruction: str, *, user_id: str = "local", ai_client=None) -> dict:
    """Plan → validate → apply, as ONE write against the revision that was read.
    Returns {applied: [sentences], summary, project: summary}."""
    from backend.services import studio_music
    from backend.services import studio_store as store

    instruction = (instruction or "").strip()
    if not instruction:
        raise EditError("Say what you'd like changed.")
    row = await store.get_project(project_id, user_id)
    if row.status == "rendering":
        raise EditError("This video is rendering — edit it once the render finishes.")
    doc = json.loads(row.doc_json)
    if ai_client is None:
        from backend.core.ai_provider import get_ai_client
        from backend.services.render_post import load_user_settings
        try:
            ai_client = get_ai_client(await load_user_settings(user_id))
        except Exception as e:  # noqa: BLE001
            raise EditError("Add an AI key in Settings to edit with AI.") from e
    changes, summary = await plan_changes(doc, row.title, instruction, ai_client)
    ops, said = await to_ops(project_id, doc, changes)
    if not ops:
        return {"applied": [], "summary": summary or "Nothing to change.", "project": store.summary(row)}
    row, _ = await store.write_ops(project_id, ops, if_revision=row.revision, user_id=user_id)
    new_doc = json.loads(row.doc_json)
    music_op = await studio_music.default_music_op(new_doc)
    if music_op:
        row, _ = await store.write_ops(project_id, [music_op], if_revision=None, internal=True, user_id=user_id)
        new_doc = json.loads(row.doc_json)
    try:
        from backend.core.ws_manager import ws_manager
        await ws_manager.send({"type": "studio_project_updated", "project_id": project_id, "revision": row.revision},
                              user_id=user_id)
    except Exception:  # noqa: BLE001
        logger.debug("studio edit: update notice failed", exc_info=True)
    return {"applied": said, "summary": summary, "project": store.summary(row)}
