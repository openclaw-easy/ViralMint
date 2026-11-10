# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Smart Video Studio — the op layer.

`apply_ops(doc, ops, ctx)` is PURE: it deep-copies the document, applies every
op in order and validates the result. All-or-nothing — one rejected op raises
StudioOpError and the caller's document is untouched. The router persists the
result under optimistic concurrency (revision).

Rules this layer owns:
  * Targets are STABLE scene uids, never positions.
  * Edits are client ops. Jobs (voice takes, planning, the render) have their
    own endpoints and write back through the internal ops `voice_result` /
    `visual_result` / `plan_result`, which land ONLY if the scene still wants
    the result — checked here, atomically with the write.
  * Structural ops keep the scene's own footage (split/insert borrow free
    stock); bind replaces a visual; replace_doc (undo) may not invent a voice
    take or a clip the project doesn't have.
  * Server trust: a stock candidate binds only through the server's own copy
    of a search result (`ctx.resolve_candidate`), a clip only through an asset
    already in the project.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from backend.core.exceptions import StudioDocError, StudioOpError
from backend.services import studio_doc as sd

CLIENT_OPS = frozenset({
    "set_line", "insert_scene", "remove_scene", "reorder_scenes", "split_scene", "merge_scenes",
    "bind_visual", "set_visual_fields", "clear_visual",
    "set_setting", "set_aspect", "set_title", "replace_doc",
})
INTERNAL_OPS = frozenset({"voice_result", "visual_result", "plan_result", "music_default"})
MAX_OPS_PER_WRITE = 100

_SETTING_PATHS = frozenset({
    "visual_style", "voice", "captions.enabled", "captions.style", "captions.hook", "captions.hook_text",
    "music", "transition", "tail_gap_s", "post",
})
_MUSIC_KEYS = frozenset({
    "enabled", "source", "track", "track_name", "genre", "volume_db", "offset_s", "fade_s",
})


@dataclass(frozen=True)
class OpContext:
    """What the pure layer may ask about the world. Defaults trust nothing."""
    take_known: Callable[[str], bool] = lambda _key: False
    project_assets: dict = field(default_factory=dict)    # asset_id → uploaded asset
    resolve_candidate: Callable[[dict], dict | None] = lambda _candidate: None


@dataclass
class OpResult:
    doc: dict
    title: str | None
    created: list[str]
    changes: list[dict]


def apply_ops(doc: dict, ops: list, ctx: OpContext | None = None, *,
              title: str | None = None, internal: bool = False) -> OpResult:
    if not isinstance(ops, list) or not ops:
        raise StudioOpError("Send at least one edit.")
    if len(ops) > MAX_OPS_PER_WRITE:
        raise StudioOpError(f"At most {MAX_OPS_PER_WRITE} edits per request.")
    ctx = ctx or OpContext()
    work = copy.deepcopy(doc)
    state = {"title": title}
    created: list[str] = []
    changes: list[dict] = []
    allowed = CLIENT_OPS | INTERNAL_OPS if internal else CLIENT_OPS
    for op in ops:
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            raise StudioOpError("Each edit needs an `op` name.")
        name = op["op"]
        if name not in allowed:
            if name in INTERNAL_OPS:
                raise StudioOpError(f"{name} is not an edit — that work has its own endpoint.")
            raise StudioOpError(f"Unknown edit {name!r}.")
        _HANDLERS[name](work, op, ctx, state, created, changes)
    try:
        # Defence in depth: whatever the ops did, the result is a valid document.
        final = sd.validate_doc(work, settle=False)
    except StudioDocError as e:
        raise StudioOpError(str(e)) from e
    return OpResult(final, state["title"], created, changes)


# ── helpers ─────────────────────────────────────────────────────────────────

def _index(doc: dict, uid, name: str) -> int:
    for i, scene in enumerate(doc["scenes"]):
        if scene["uid"] == uid:
            return i
    raise StudioOpError(f"{name}: that scene no longer exists.")


def _stale_voice(scene: dict) -> None:
    """A recorded take is kept (stale) for undo; an in-flight one with no
    earlier take just stops being wanted — its result won't land."""
    voice = scene.get("voice") or {}
    if voice.get("take_key"):
        scene["voice"] = {k: v for k, v in voice.items() if k not in ("error", "take_voice")} | {"state": "stale"}
    else:
        scene["voice"] = {"state": "none"}


def _voice_moved(scene: dict, before: tuple, after: tuple) -> None:
    """The voice changed. A ready take goes stale and remembers its voice, so
    switching back makes it ready again: same take, same line, nothing to redo.
    A line edit clears the memory (_stale_voice), and so does a new take."""
    voice = scene.get("voice") or {}
    if voice.get("state") == "stale" and voice.get("take_voice"):
        if voice["take_voice"] == list(after):
            scene["voice"] = {k: v for k, v in voice.items() if k != "take_voice"} | {"state": "ready"}
        return
    was_ready = voice.get("state") == "ready"
    _stale_voice(scene)
    if was_ready:
        scene["voice"]["take_voice"] = list(before)


def _library_name(track) -> bool:
    """A bare music-library filename — never a path."""
    return (isinstance(track, str) and bool(track) and len(track) <= 255 and "/" not in track
            and "\\" not in track and ".." not in track and not track.startswith(".") and "\x00" not in track)


def _clip(value, limit: int):
    return sd.normalise_line(value)[:limit] or None if isinstance(value, str) else None


def _cap(scenes: list) -> None:
    if len(scenes) >= sd.SCENE_MAX:
        raise StudioOpError(f"A video can have at most {sd.SCENE_MAX} scenes.")


# ── client ops ──────────────────────────────────────────────────────────────

def _set_line(doc, op, ctx, st, created, changes):
    i = _index(doc, op.get("uid"), "set_line")
    line = sd.normalise_line(op.get("line"))
    if not line:
        raise StudioOpError("A scene needs a line. Delete the scene instead.")
    if len(line) > sd.LINE_MAX:
        raise StudioOpError(f"A scene line can be at most {sd.LINE_MAX} characters — split this scene.")
    scene = doc["scenes"][i]
    if line == scene["line"]:
        return
    scene["line"] = line
    _stale_voice(scene)
    changes.append({"op": "set_line", "uid": scene["uid"]})


def _insert_scene(doc, op, ctx, st, created, changes):
    scenes = doc["scenes"]
    after = op.get("after_uid")
    at = _index(doc, after, "insert_scene") + 1 if after else len(scenes)
    _cap(scenes)
    line = sd.normalise_line(op.get("line"))
    if len(line) > sd.LINE_MAX:
        raise StudioOpError(f"A scene line can be at most {sd.LINE_MAX} characters — split this scene.")
    near = scenes[at - 1] if at >= 1 else (scenes[at] if at < len(scenes) else None)
    # An inserted scene can start with NO line (not voiced or rendered until
    # written) and borrows stock from its neighbour.
    scene = sd.make_scene(line, query=(near or {}).get("visual", {}).get("query"),
                          mood=(near or {}).get("mood"))
    scenes.insert(at, scene)
    created.append(scene["uid"])
    changes.append({"op": "insert_scene", "uid": scene["uid"]})


def _remove_scene(doc, op, ctx, st, created, changes):
    if len(doc["scenes"]) <= 1:
        raise StudioOpError("A video needs at least one scene.")
    i = _index(doc, op.get("uid"), "remove_scene")
    removed = doc["scenes"].pop(i)
    changes.append({"op": "remove_scene", "uid": removed["uid"]})


def _reorder_scenes(doc, op, ctx, st, created, changes):
    order = op.get("order")
    ids = [s["uid"] for s in doc["scenes"]]
    if not isinstance(order, list) or len(order) != len(ids) or sorted(map(str, order)) != sorted(ids):
        raise StudioOpError("reorder_scenes: the order must list every scene exactly once.")
    by_id = {s["uid"]: s for s in doc["scenes"]}
    doc["scenes"] = [by_id[uid] for uid in order]
    changes.append({"op": "reorder_scenes"})


def _join_all(parts: list[str]) -> str:
    out = ""
    for part in parts:
        out = sd.join_sentences(out, part)
    return out


def _split_scene(doc, op, ctx, st, created, changes):
    i = _index(doc, op.get("uid"), "split_scene")
    scenes = doc["scenes"]
    _cap(scenes)
    scene = scenes[i]
    line = scene["line"]
    at_char = op.get("at_char")
    if isinstance(at_char, int) and not isinstance(at_char, bool) and 0 < at_char < len(line):
        spaces = [j for j, ch in enumerate(line) if ch == " "]
        cut = min(spaces, key=lambda j: abs(j - at_char)) if spaces else at_char
        first, second = line[:cut].strip(), line[cut:].strip()
    else:
        parts = sd.split_sentences(line)
        if len(parts) >= 2:
            mid = math.ceil(len(parts) / 2)
            first, second = _join_all(parts[:mid]), _join_all(parts[mid:])
        elif " " not in line and len(line) >= 8:
            # CJK has no spaces: cut at the clause mark nearest the middle,
            # else at the middle character (otherwise a Chinese line would be
            # "too short to split" at any length).
            marks = [j + 1 for j, ch in enumerate(line[:-1]) if ch in "，、；：,;:—"]
            mid = len(line) // 2
            cut = min(marks, key=lambda j: abs(j - mid)) if marks else mid
            first, second = line[:cut].strip(), line[cut:].strip()
        else:
            words = line.split(" ")
            if len(words) < 4:
                raise StudioOpError("This line is too short to split.")
            mid = math.ceil(len(words) / 2)
            first, second = " ".join(words[:mid]), " ".join(words[mid:])
    if not first or not second:
        raise StudioOpError("This line is too short to split.")
    scene["line"] = first
    _stale_voice(scene)
    # The second half is stock with the same query (placeholder → the render
    # or a re-plan fills it).
    new = sd.make_scene(second, mood=scene.get("mood"),
                        visual=sd.stock_visual(scene["visual"].get("query"), scene["visual"], bind_top=False))
    scenes.insert(i + 1, new)
    created.append(new["uid"])
    changes.append({"op": "split_scene", "uid": scene["uid"], "created": new["uid"]})


def _merge_scenes(doc, op, ctx, st, created, changes):
    i = _index(doc, op.get("uid"), "merge_scenes")
    scenes = doc["scenes"]
    if i >= len(scenes) - 1:
        raise StudioOpError("There is no next scene to merge with.")
    if op.get("next_uid") is not None and scenes[i + 1]["uid"] != op["next_uid"]:
        raise StudioOpError("The scene order changed — try merging again.")
    line = sd.normalise_line(sd.join_sentences(scenes[i]["line"], scenes[i + 1]["line"]))
    if len(line) > sd.LINE_MAX:
        raise StudioOpError(f"The merged line would be over {sd.LINE_MAX} characters.")
    scenes[i]["line"] = line
    _stale_voice(scenes[i])
    removed = scenes.pop(i + 1)
    changes.append({"op": "merge_scenes", "uid": scenes[i]["uid"], "removed": removed["uid"]})


def _bind_visual(doc, op, ctx, st, created, changes):
    i = _index(doc, op.get("uid"), "bind_visual")
    scene = doc["scenes"][i]
    prev = scene["visual"]
    visual = op.get("visual")
    if not isinstance(visual, dict):
        raise StudioOpError("bind_visual: send the visual to bind.")
    kind = visual.get("kind")
    # Replace, carrying only what belongs to the SCENE.
    base = {
        "query": _clip(visual.get("query"), 100) or prev.get("query"),
        "alternates": prev.get("alternates") or [],
        "fit": visual.get("fit") if visual.get("fit") in sd.FITS else (prev.get("fit") or "loop"),
        "pinned": visual.get("pinned") is not False,
        "asset": None, "reason": None,
    }
    if kind == "stock":
        candidate = visual.get("candidate")
        if candidate is None:
            new = {**base, "kind": "stock", "state": "placeholder"}
        else:
            canonical = ctx.resolve_candidate(candidate) if isinstance(candidate, dict) else None
            if canonical is None:
                raise StudioOpError("That clip isn't one of the search results for this project — search again.")
            new = {**base, "kind": "stock", "state": "bound", "asset": dict(canonical),
                   # The scene keeps its own query: a clip can come from another
                   # scene's search, and a rewrite made the bin re-search under
                   # the cursor.
                   "query": base["query"] or canonical.get("query")}
    elif kind == "user_clip":
        asset = ctx.project_assets.get(visual.get("asset_id")) if isinstance(visual.get("asset_id"), str) else None
        if asset is None:
            raise StudioOpError("That clip isn't in this project — add it first.")
        new = {**base, "kind": "user_clip", "state": "bound", "asset": dict(asset)}
    else:
        raise StudioOpError(f"bind_visual: unknown visual kind {kind!r}.")
    scene["visual"] = new
    changes.append({"op": "bind_visual", "uid": scene["uid"], "kind": kind})


def _set_visual_fields(doc, op, ctx, st, created, changes):
    i = _index(doc, op.get("uid"), "set_visual_fields")
    visual = doc["scenes"][i]["visual"]
    fields = op.get("fields")
    if not isinstance(fields, dict):
        raise StudioOpError("set_visual_fields: send the fields to change.")
    if "query" in fields:
        visual["query"] = _clip(fields["query"], 100) or visual.get("query")
    if "fit" in fields:
        if fields["fit"] not in sd.FITS:
            raise StudioOpError(f"fit must be one of {', '.join(sd.FITS)}.")
        visual["fit"] = fields["fit"]
    if "pinned" in fields:
        visual["pinned"] = fields["pinned"] is True
    changes.append({"op": "set_visual_fields", "uid": doc["scenes"][i]["uid"]})


def _clear_visual(doc, op, ctx, st, created, changes):
    i = _index(doc, op.get("uid"), "clear_visual")
    scene = doc["scenes"][i]
    scene["visual"] = sd.stock_visual(scene["visual"].get("query"), scene["visual"])
    changes.append({"op": "clear_visual", "uid": scene["uid"]})


def _set_setting(doc, op, ctx, st, created, changes):
    path, value = op.get("path"), op.get("value")
    if path not in _SETTING_PATHS:
        raise StudioOpError(f"set_setting: {path} is not editable.")
    settings = doc["settings"]
    if path == "voice":
        if not isinstance(value, dict) or value.get("provider") not in sd.TTS_PROVIDERS:
            raise StudioOpError("set_setting voice: choose a supported voice.")
        before = sd.voice_identity(settings)
        # Replaced, not merged: no instructions left over from the last voice.
        settings["voice"] = {k: value.get(k) for k in ("provider", "voice", "label", "model", "instructions")}
        after = sd.voice_identity(settings)
        if after != before:
            for scene in doc["scenes"]:
                _voice_moved(scene, before, after)
    elif path == "music":
        if not isinstance(value, dict):
            raise StudioOpError("set_setting music: send the fields to change.")
        if "source" in value and value["source"] not in sd.MUSIC_SOURCES:
            raise StudioOpError("set_setting music: music comes from your library.")
        if "track" in value and value["track"] is not None and not _library_name(value["track"]):
            raise StudioOpError("set_setting music: that isn't a track in your library.")
        settings["music"] = {**settings["music"], **{k: v for k, v in value.items() if k in _MUSIC_KEYS}}
    elif path == "captions.enabled":
        if not isinstance(value, bool):
            raise StudioOpError("captions.enabled must be true or false.")
        settings["captions"]["enabled"] = value
    elif path == "captions.style":
        if not isinstance(value, str) or not value.strip():
            raise StudioOpError("Choose a caption style.")
        settings["captions"]["style"] = value.strip()[:40]
    elif path == "captions.hook":
        if not isinstance(value, bool):
            raise StudioOpError("captions.hook must be true or false.")
        settings["captions"]["hook"] = value
    elif path == "captions.hook_text":
        # Free text burned as the opening title; blank/None = the first line.
        if value is not None and not isinstance(value, str):
            raise StudioOpError("The title must be text.")
        settings["captions"]["hook_text"] = sd.normalise_hook_text(value)
    elif path == "visual_style":
        if value not in sd.VISUAL_STYLES:
            raise StudioOpError("Unknown visual style.")
        settings["visual_style"] = value
    elif path == "transition":
        if value not in sd.TRANSITIONS:
            raise StudioOpError("Unknown transition.")
        settings["transition"] = value
    elif path == "post":
        # Finishing steps (render_post): each key true/false = this project's
        # choice, null = follow Settings. Only the keys sent change.
        if not isinstance(value, dict) or not value:
            raise StudioOpError("set_setting post: send the finishing steps to change.")
        for key, v in value.items():
            if key in sd.POST_TOGGLES:
                if v is not None and not isinstance(v, bool):
                    raise StudioOpError(f"set_setting post: {key} must be true, false or null.")
            elif key == "caption_emoji":
                if v is not None and v not in sd.CAPTION_EMOJI_STYLES:
                    raise StudioOpError("set_setting post: unknown emoji style.")
            else:
                raise StudioOpError(f"set_setting post: {key} is not a finishing step.")
        settings["post"] = sd.normalise_post({**settings.get("post", sd.default_post()), **value})
    elif path == "tail_gap_s":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= sd.TAIL_GAP_MAX:
            raise StudioOpError(f"The pause after each line must be between 0 and {sd.TAIL_GAP_MAX}s.")
        settings["tail_gap_s"] = float(value)
    changes.append({"op": "set_setting", "path": path})


def _set_aspect(doc, op, ctx, st, created, changes):
    if op.get("ratio") not in sd.ASPECTS:
        raise StudioOpError("Unsupported aspect ratio.")
    doc["aspect_ratio"] = op["ratio"]
    changes.append({"op": "set_aspect", "ratio": op["ratio"]})


def _set_title(doc, op, ctx, st, created, changes):
    title = sd.normalise_line(op.get("title"))[:120]
    if title:
        st["title"] = title
        changes.append({"op": "set_title"})


def _replace_doc(doc, op, ctx, st, created, changes):
    """Undo/redo restore: validated like client input; may not invent a voice
    take or a clip this project doesn't have."""
    # Which scenes were WAITING in the snapshot, read before `settle` erases
    # it: an undo taken while a take was in flight must not throw away that
    # result once it has landed.
    raw_scenes = (op.get("doc") or {}).get("scenes") if isinstance(op.get("doc"), dict) else None
    waited_voice = set()
    for rs in raw_scenes if isinstance(raw_scenes, list) else []:
        if isinstance(rs, dict) and isinstance(rs.get("uid"), str):
            if (rs.get("voice") or {}).get("state") == "generating":
                waited_voice.add(rs["uid"])
    try:
        new = sd.validate_doc(op.get("doc"), take_known=ctx.take_known, settle=True)
    except StudioDocError as e:
        raise StudioOpError(f"replace_doc: {e}") from e
    current = {s["uid"]: s for s in doc["scenes"]}
    identity_unchanged = list(sd.voice_identity(new["settings"])) == list(sd.voice_identity(doc["settings"]))
    for scene in new["scenes"]:
        visual = scene["visual"]
        if visual["kind"] == "user_clip" and (visual.get("asset") or {}).get("asset_id") not in ctx.project_assets:
            # The clip was removed from the project since this snapshot was
            # taken. Refusing here would jam undo/redo for good (every restore
            # older than the removal names the clip), so the scene goes back to
            # free stock — exactly what removing the clip did (clear_visual).
            scene["visual"] = sd.stock_visual(visual.get("query"), visual)
        # Settling a restored snapshot is for ITS stale spinners. A scene that is
        # voicing on the server RIGHT NOW keeps that state when the restore asks
        # for the same line in the same voice — otherwise an undo during Voice
        # all cancelled work in flight: the job skipped scenes and the take
        # couldn't land.
        live = current.get(scene["uid"])
        if live is None:
            continue
        same_voice_request = identity_unchanged and live["line"] == scene["line"] and scene["line"]
        live_voice = live["voice"].get("state")
        # Still in flight → keep waiting. Landed since the snapshot was taken
        # → keep the take the snapshot was waiting for (resetting it made
        # scenes go silent after ⌘Z).
        if same_voice_request and (live_voice == "generating"
                                   or (live_voice == "ready" and scene["uid"] in waited_voice)):
            scene["voice"] = copy.deepcopy(live["voice"])
    doc.clear()
    doc.update(new)
    changes.append({"op": "replace_doc"})


# ── internal ops (job results) ──────────────────────────────────────────────

def _voice_result(doc, op, ctx, st, created, changes):
    """Land a voice state. A `ready` take lands only if the scene is still
    generating the SAME line in the SAME voice it was started for; otherwise
    it is dropped here (the take stays in the cache for the next time)."""
    uid, voice = op.get("uid"), op.get("voice")
    scene = next((s for s in doc["scenes"] if s["uid"] == uid), None)
    if scene is None or not isinstance(voice, dict):
        return
    expect = op.get("expect")
    if expect is not None:
        # The take belongs to a line in a voice; it lands only while the scene
        # still HAS that line in that voice (a voice job or a render recorded it).
        still = (
            scene["line"] == expect.get("line")
            and list(sd.voice_identity(doc["settings"])) == list(expect.get("voice") or [])
        )
        if not still:
            return
    scene["voice"] = {**scene["voice"], **voice}
    changes.append({"op": "voice_result", "uid": uid, "state": scene["voice"].get("state")})


def _visual_result(doc, op, ctx, st, created, changes):
    """Land a visual state (the render replacing a dead stock link) — only
    while the scene's visual still matches `expect`."""
    uid, visual = op.get("uid"), op.get("visual")
    scene = next((s for s in doc["scenes"] if s["uid"] == uid), None)
    if scene is None or not isinstance(visual, dict):
        return
    expect = op.get("expect")
    if isinstance(expect, dict) and any(scene["visual"].get(k) != v for k, v in expect.items()):
        return          # the scene moved on while the render ran
    scene["visual"] = {**scene["visual"], **visual}
    changes.append({"op": "visual_result", "uid": uid, "state": scene["visual"].get("state")})


def _plan_result(doc, op, ctx, st, created, changes):
    """Land a planned visual — only on a scene that is still OPEN (not pinned
    or your own clip) and whose line is still the line that was planned.
    Planning never overrides a choice."""
    uid, visual = op.get("uid"), op.get("visual")
    scene = next((s for s in doc["scenes"] if s["uid"] == uid), None)
    if scene is None or not isinstance(visual, dict) or visual.get("kind") != "stock":
        return
    if op.get("expect_line") is not None and scene["line"] != op["expect_line"]:
        return
    if sd.is_fixed_visual(scene):
        return
    scene["visual"] = visual
    if isinstance(op.get("mood"), str):
        scene["mood"] = op["mood"][:20]
    changes.append({"op": "plan_result", "uid": uid})


def _music_default(doc, op, ctx, st, created, changes):
    """A concrete library track for a bed that is on but has none — set only
    while that is still true in the document being written, so it can never
    overwrite a track (or "off") the user chose meanwhile."""
    track = op.get("track")
    if not isinstance(track, str) or not _library_name(track):
        return
    music = doc["settings"]["music"]
    if not music.get("enabled") or music.get("track"):
        return
    music.update({"track": track[:200], "track_name": str(op.get("track_name") or track)[:200]})
    if isinstance(op.get("genre"), str) and op["genre"]:
        music["genre"] = op["genre"][:40]
    changes.append({"op": "music_default", "track": music["track"]})


_HANDLERS = {
    "set_line": _set_line, "insert_scene": _insert_scene, "remove_scene": _remove_scene,
    "reorder_scenes": _reorder_scenes, "split_scene": _split_scene, "merge_scenes": _merge_scenes,
    "bind_visual": _bind_visual, "set_visual_fields": _set_visual_fields, "clear_visual": _clear_visual,
    "set_setting": _set_setting, "set_aspect": _set_aspect,
    "set_title": _set_title, "replace_doc": _replace_doc,
    "voice_result": _voice_result, "visual_result": _visual_result, "plan_result": _plan_result,
    "music_default": _music_default,
}
assert set(_HANDLERS) == CLIENT_OPS | INTERNAL_OPS
