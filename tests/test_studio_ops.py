# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""studio_ops — the Smart Video Studio edit layer.

Pure: documents in, documents out. Ops are all-or-nothing, target stable
scene uids, bind only what the server vouches for, and job results land only
while the scene still wants them.
"""
import copy
import hashlib

import pytest

from backend.core.exceptions import StudioOpError
from backend.services import studio_doc as sd
from backend.services.studio_ops import OpContext, apply_ops


def key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def stock(query="city", **over):
    return {"kind": "stock", "state": "placeholder", "query": query, **over}


def make_doc(lines, *, style="cinematic"):
    scenes = []
    for i, line in enumerate(lines):
        scenes.append({
            "uid": f"s{i + 1}", "line": line, "voice": {"state": "none"},
            "visual": stock(f"query {i + 1}"),
        })
    return sd.validate_doc({"settings": sd.default_settings(visual_style=style), "scenes": scenes})


def run(doc, *ops, ctx=None, internal=False):
    return apply_ops(doc, list(ops), ctx, internal=internal)


CLIP_CTX = OpContext(project_assets={"as_1": {"asset_id": "as_1", "title": "beach.mp4"}})


# ── contract basics ─────────────────────────────────────────────────────────

def test_ops_are_all_or_nothing_and_pure():
    d = make_doc(["One line here.", "Two line here."])
    before = copy.deepcopy(d)
    with pytest.raises(StudioOpError, match="needs a line"):
        run(d, {"op": "set_line", "uid": "s1", "line": "Changed."}, {"op": "set_line", "uid": "s2", "line": "  "})
    assert d == before


def test_job_results_are_not_client_ops():
    d = make_doc(["A line."])
    with pytest.raises(StudioOpError, match="own endpoint"):
        run(d, {"op": "voice_result", "uid": "s1", "voice": {"state": "ready"}})
    with pytest.raises(StudioOpError, match="Unknown edit"):
        run(d, {"op": "charge_card"})


def test_line_limits():
    d = make_doc(["A line."])
    with pytest.raises(StudioOpError, match="at most 400"):
        run(d, {"op": "set_line", "uid": "s1", "line": "x" * 401})


def test_editing_a_voiced_line_keeps_the_take_as_stale():
    d = make_doc(["A line."])
    d["scenes"][0]["voice"] = {"state": "ready", "take_key": key("a"), "duration": 2}
    out = run(d, {"op": "set_line", "uid": "s1", "line": "A new line."}).doc
    assert out["scenes"][0]["voice"]["state"] == "stale"
    assert out["scenes"][0]["voice"]["take_key"] == key("a")


# ── structural ops ──────────────────────────────────────────────────────────

def test_split_and_insert_add_stock_scenes():
    d = make_doc([f"Sentence {i} is here. And another thought follows it." for i in range(6)], style="card")
    for uid in [s["uid"] for s in d["scenes"][:5]]:
        d = run(d, {"op": "split_scene", "uid": uid}).doc
    res = run(d, {"op": "insert_scene", "after_uid": None, "line": ""})
    added = [s for s in res.doc["scenes"] if not s["uid"].startswith("s")]
    assert len(added) == 6
    assert all(s["visual"]["kind"] == "stock" for s in added)
    assert res.doc["scenes"][-1]["line"] == ""
    assert res.created == [res.doc["scenes"][-1]["uid"]]


def test_split_keeps_the_query_and_restales_only_the_first_half():
    d = make_doc(["First sentence here. Second sentence here."])
    d["scenes"][0]["voice"] = {"state": "ready", "take_key": key("x"), "duration": 3}
    out = run(d, {"op": "split_scene", "uid": "s1"}).doc
    first, second = out["scenes"]
    assert first["line"] == "First sentence here." and second["line"] == "Second sentence here."
    assert first["voice"]["state"] == "stale" and second["voice"] == {"state": "none"}
    assert second["visual"]["query"] == first["visual"]["query"]


def test_scene_cap_on_split_and_insert():
    d = make_doc([f"Line number {i} is long enough to split." for i in range(18)])
    with pytest.raises(StudioOpError, match="at most 18"):
        run(d, {"op": "split_scene", "uid": "s1"})
    with pytest.raises(StudioOpError, match="at most 18"):
        run(d, {"op": "insert_scene", "after_uid": "s1", "line": ""})


def test_merge_checks_the_next_scene_is_still_next():
    d = make_doc(["One.", "Two.", "Three."])
    with pytest.raises(StudioOpError, match="order changed"):
        run(d, {"op": "merge_scenes", "uid": "s1", "next_uid": "s3"})
    out = run(d, {"op": "merge_scenes", "uid": "s1", "next_uid": "s2"}).doc
    assert [s["line"] for s in out["scenes"]] == ["One. Two.", "Three."]


def test_remove_and_reorder_guards():
    d = make_doc(["Only."])
    with pytest.raises(StudioOpError, match="at least one"):
        run(d, {"op": "remove_scene", "uid": "s1"})
    d = make_doc(["One.", "Two."])
    with pytest.raises(StudioOpError, match="every scene exactly once"):
        run(d, {"op": "reorder_scenes", "order": ["s1", "s1"]})
    assert [s["uid"] for s in run(d, {"op": "reorder_scenes", "order": ["s2", "s1"]}).doc["scenes"]] == ["s2", "s1"]


# ── bind replaces, and binds only what the server vouches for ──────────────

def test_bind_replaces_the_old_kind_fields():
    d = make_doc(["A line."])
    d = run(d, {"op": "bind_visual", "uid": "s1", "visual": {"kind": "user_clip", "asset_id": "as_1"}}, ctx=CLIP_CTX).doc
    assert d["scenes"][0]["visual"]["asset"]["asset_id"] == "as_1"
    out = run(d, {"op": "bind_visual", "uid": "s1", "visual": {"kind": "stock", "query": "beach", "pinned": False}}).doc
    v = out["scenes"][0]["visual"]
    assert v["kind"] == "stock" and v["state"] == "placeholder" and v["asset"] is None
    assert v["pinned"] is False and v["query"] == "beach"


def test_bind_refuses_a_kind_this_build_has_no_renderer_for():
    d = make_doc(["A line."])
    with pytest.raises(StudioOpError, match="unknown visual kind"):
        run(d, {"op": "bind_visual", "uid": "s1", "visual": {"kind": "ai_clip", "prompt": "waves"}})


def test_stock_candidates_bind_only_through_the_server_copy():
    d = make_doc(["A line."])
    forged = {"provider": "pexels", "provider_id": "1", "download_url": "https://evil.example/x.mp4"}
    with pytest.raises(StudioOpError, match="search results"):
        run(d, {"op": "bind_visual", "uid": "s1", "visual": {"kind": "stock", "candidate": forged}})
    canonical = {"provider": "pexels", "provider_id": "1", "download_url": "https://videos.pexels.com/1.mp4", "query": "sea"}
    ctx = OpContext(resolve_candidate=lambda c: canonical if c.get("provider_id") == "1" else None)
    v = run(d, {"op": "bind_visual", "uid": "s1", "visual": {"kind": "stock", "candidate": forged}}, ctx=ctx).doc["scenes"][0]["visual"]
    assert v["state"] == "bound" and v["asset"]["download_url"] == canonical["download_url"]
    assert v["pinned"] is True


def _bind_from_search(d, query):
    canonical = {"provider": "pexels", "provider_id": "1", "download_url": "https://videos.pexels.com/1.mp4", "query": query}
    ctx = OpContext(resolve_candidate=lambda _c: canonical)
    op = {"op": "bind_visual", "uid": "s1", "visual": {"kind": "stock", "candidate": {"provider_id": "1"}}}
    return run(d, op, ctx=ctx).doc["scenes"][0]["visual"]


def test_use_keeps_the_scenes_search_query():
    """A clip can come from another scene's search. Binding it must not rewrite
    this scene's query: the bin re-searches on that and swaps the list under the
    cursor, so a double-click could bind a different clip."""
    v = _bind_from_search(make_doc(["One.", "Two."]), "another scene's words")
    assert v["query"] == "query 1"
    assert v["asset"]["query"] == "another scene's words"       # the clip still knows its own


def test_use_on_a_scene_with_no_query_takes_the_clips():
    d = make_doc(["One."])
    d["scenes"][0]["visual"]["query"] = None
    assert _bind_from_search(d, "sea")["query"] == "sea"


def _voiced_doc():
    d = make_doc(["One."])
    d["scenes"][0]["voice"] = {"state": "ready", "take_key": key("andrew-one"), "duration": 2.0}
    d["settings"]["voice"] = {"provider": "edge_tts", "voice": "en-US-AndrewNeural", "label": None, "model": None,
                              "instructions": None}
    return d


def _voice_op(name):
    return {"op": "set_setting", "path": "voice", "value": {"provider": "edge_tts", "voice": name}}


def _ctx():
    return OpContext(take_known=lambda k: k == key("andrew-one"))


def test_voice_away_and_back_restores_the_take():
    d = _voiced_doc()
    before = copy.deepcopy(d["scenes"][0]["voice"])
    away = run(d, _voice_op("en-US-AriaNeural"), ctx=_ctx()).doc
    assert away["scenes"][0]["voice"]["state"] == "stale"
    further = run(away, _voice_op("en-US-GuyNeural"), ctx=_ctx()).doc
    assert further["scenes"][0]["voice"]["state"] == "stale"
    back = run(further, _voice_op("en-US-AndrewNeural"), ctx=_ctx()).doc
    assert back["scenes"][0]["voice"] == before                 # ready, same take


def test_voice_back_does_not_restore_a_take_for_an_edited_line():
    away = run(_voiced_doc(), _voice_op("en-US-AriaNeural"), {"op": "set_line", "uid": "s1", "line": "Different."},
               ctx=_ctx()).doc
    back = run(away, _voice_op("en-US-AndrewNeural"), ctx=_ctx()).doc
    assert back["scenes"][0]["voice"]["state"] == "stale"


def test_a_line_edit_leaves_no_voice_marker():
    edited = run(_voiced_doc(), {"op": "set_line", "uid": "s1", "line": "Different."}, ctx=_ctx()).doc
    assert "take_voice" not in edited["scenes"][0]["voice"]


def test_the_voice_marker_survives_validation_only_on_a_stale_take():
    d = run(_voiced_doc(), _voice_op("en-US-AriaNeural"), ctx=_ctx()).doc
    assert sd.validate_doc(d)["scenes"][0]["voice"]["take_voice"][1] == "en-US-AndrewNeural"
    d["scenes"][0]["voice"]["state"] = "ready"
    assert "take_voice" not in sd.validate_doc(d)["scenes"][0]["voice"]


def test_user_clips_bind_only_from_project_assets():
    d = make_doc(["A line."])
    with pytest.raises(StudioOpError, match="isn't in this project"):
        run(d, {"op": "bind_visual", "uid": "s1", "visual": {"kind": "user_clip", "asset_id": "as_1"}})
    v = run(d, {"op": "bind_visual", "uid": "s1", "visual": {"kind": "user_clip", "asset_id": "as_1"}}, ctx=CLIP_CTX).doc["scenes"][0]["visual"]
    assert v["kind"] == "user_clip" and v["query"] == "query 1"   # the planner's query survives


def test_clear_visual_returns_to_unpinned_stock_with_the_scene_query():
    d = make_doc(["A line."])
    d = run(d, {"op": "bind_visual", "uid": "s1", "visual": {"kind": "user_clip", "asset_id": "as_1"}}, ctx=CLIP_CTX).doc
    v = run(d, {"op": "clear_visual", "uid": "s1"}).doc["scenes"][0]["visual"]
    assert v["kind"] == "stock" and v["query"] == "query 1" and v["pinned"] is False


# ── settings ────────────────────────────────────────────────────────────────

def test_voice_change_stales_takes_only_when_the_voice_really_changed():
    d = make_doc(["A line."])
    d["scenes"][0]["voice"] = {"state": "ready", "take_key": key("t"), "duration": 2}
    same = dict(d["settings"]["voice"])
    assert run(d, {"op": "set_setting", "path": "voice", "value": same}).doc["scenes"][0]["voice"]["state"] == "ready"
    other = {"provider": "openai_tts", "voice": "nova", "label": "Nova"}
    out = run(d, {"op": "set_setting", "path": "voice", "value": other}).doc
    assert out["scenes"][0]["voice"]["state"] == "stale"
    assert out["settings"]["voice"]["instructions"] is None


def test_setting_whitelist_and_values():
    d = make_doc(["A line."])
    with pytest.raises(StudioOpError, match="not editable"):
        run(d, {"op": "set_setting", "path": "voice.provider", "value": "x"})
    with pytest.raises(StudioOpError, match="between 0 and"):
        run(d, {"op": "set_setting", "path": "tail_gap_s", "value": 3})
    with pytest.raises(StudioOpError, match="from your library"):
        run(d, {"op": "set_setting", "path": "music", "value": {"source": "ai"}})
    for bad in ("../etc/passwd", "a/b.mp3", "a\\b.mp3", ".hidden.mp3"):
        with pytest.raises(StudioOpError, match="isn't a track"):
            run(d, {"op": "set_setting", "path": "music", "value": {"track": bad}})
    out = run(d, {"op": "set_setting", "path": "music", "value": {"track": "calm.mp3", "asset_id": "sneaky"}}).doc
    assert out["settings"]["music"]["track"] == "calm.mp3"
    assert "asset_id" not in out["settings"]["music"]          # unknown music keys never land


def test_post_settings_accept_only_finishing_steps():
    d = make_doc(["A line."])
    out = run(d, {"op": "set_setting", "path": "post", "value": {"auto_zoom": True}}).doc
    assert out["settings"]["post"]["auto_zoom"] is True and out["settings"]["post"]["sfx"] is False
    with pytest.raises(StudioOpError, match="not a finishing step"):
        run(d, {"op": "set_setting", "path": "post", "value": {"watermark": True}})
    with pytest.raises(StudioOpError, match="true, false or null"):
        run(d, {"op": "set_setting", "path": "post", "value": {"sfx": "on"}})
    with pytest.raises(StudioOpError, match="unknown emoji style"):
        run(d, {"op": "set_setting", "path": "post", "value": {"caption_emoji": "fireworks"}})


# ── replace_doc (undo/redo) ─────────────────────────────────────────────────

def test_replace_doc_rejects_an_invented_take_and_settles_inflight():
    d = make_doc(["One.", "Two."])
    forged = copy.deepcopy(d)
    forged["scenes"][0]["voice"] = {"state": "ready", "take_key": key("forged"), "duration": 3}
    with pytest.raises(StudioOpError, match="unknown voice take"):
        run(d, {"op": "replace_doc", "doc": forged})
    inflight = copy.deepcopy(d)
    inflight["scenes"][0]["voice"] = {"state": "generating"}
    out = run(d, {"op": "replace_doc", "doc": inflight}).doc
    assert out["scenes"][0]["voice"] == {"state": "none"}


def test_replace_doc_accepts_known_takes():
    d = make_doc(["One."])
    snap = copy.deepcopy(d)
    snap["scenes"][0]["voice"] = {"state": "ready", "take_key": key("ok"), "duration": 2}
    out = run(d, {"op": "replace_doc", "doc": snap}, ctx=OpContext(take_known=lambda k: k == key("ok"))).doc
    assert out["scenes"][0]["voice"]["state"] == "ready"


def test_replace_doc_degrades_a_removed_clip_to_stock_instead_of_jamming_undo():
    d = make_doc(["One.", "Two."])
    with_clip = run(d, {"op": "bind_visual", "uid": "s1", "visual": {"kind": "user_clip", "asset_id": "as_1"}}, ctx=CLIP_CTX).doc
    # The clip is removed from the project; an undo restores the snapshot that still names it.
    out = run(d, {"op": "replace_doc", "doc": with_clip}, ctx=OpContext()).doc
    v = out["scenes"][0]["visual"]
    assert v["kind"] == "stock" and v["pinned"] is False and v["query"] == "query 1"
    # With the clip still present, the snapshot restores as it was.
    back = run(d, {"op": "replace_doc", "doc": with_clip}, ctx=CLIP_CTX).doc
    assert back["scenes"][0]["visual"]["kind"] == "user_clip"


# ── job results land only if still wanted ──────────────────────────────────

def test_voice_result_lands_only_for_the_same_line_and_voice():
    d = make_doc(["Original line."])
    d = run(d, {"op": "voice_result", "uid": "s1", "voice": {"state": "generating"}}, internal=True).doc
    expect = {"line": "Original line.", "voice": list(sd.voice_identity(d["settings"]))}
    ready = {"state": "ready", "take_key": key("orig"), "duration": 2.1}

    edited = run(d, {"op": "set_line", "uid": "s1", "line": "Edited line."}).doc
    late = run(edited, {"op": "voice_result", "uid": "s1", "voice": ready, "expect": expect}, internal=True).doc
    assert late["scenes"][0]["voice"]["state"] != "ready"

    landed = run(d, {"op": "voice_result", "uid": "s1", "voice": ready, "expect": expect}, internal=True).doc
    assert landed["scenes"][0]["voice"]["state"] == "ready"


def test_visual_result_lands_only_while_the_visual_still_matches():
    d = make_doc(["A line."])
    failed = {"state": "failed", "reason": "the stock link is gone"}
    expect = {"kind": "stock", "query": "query 1"}
    moved = run(d, {"op": "set_visual_fields", "uid": "s1", "fields": {"query": "street"}}).doc
    stale = run(moved, {"op": "visual_result", "uid": "s1", "visual": failed, "expect": expect}, internal=True).doc
    assert stale["scenes"][0]["visual"]["state"] == "placeholder"
    landed = run(d, {"op": "visual_result", "uid": "s1", "visual": failed, "expect": expect}, internal=True).doc
    assert landed["scenes"][0]["visual"]["state"] == "failed"


def test_plan_result_lands_only_on_an_open_scene_with_the_planned_line():
    d = make_doc(["One.", "Two.", "Three."])
    d = run(d, {"op": "set_visual_fields", "uid": "s2", "fields": {"pinned": True}}).doc
    planned = {"kind": "stock", "state": "placeholder", "query": "planned"}
    out = run(d,
              {"op": "plan_result", "uid": "s1", "visual": planned, "expect_line": "One.", "mood": "calm"},
              {"op": "plan_result", "uid": "s2", "visual": planned, "expect_line": "Two."},
              {"op": "plan_result", "uid": "s3", "visual": planned, "expect_line": "An older line."},
              internal=True).doc
    assert [s["visual"]["query"] for s in out["scenes"]] == ["planned", "query 2", "query 3"]
    assert out["scenes"][0]["mood"] == "calm"


# ── music_default (a library bed that's on needs a concrete track) ──────────

def test_music_default_sets_a_track_only_while_none_is_chosen():
    doc = make_doc(["One line here."])
    op = {"op": "music_default", "track": "lofi_calm.mp3", "track_name": "lofi calm"}
    with pytest.raises(StudioOpError):
        run(doc, op)                                            # internal only — a client can't send it
    picked = run(doc, op, internal=True).doc["settings"]["music"]
    assert picked["track"] == "lofi_calm.mp3" and picked["track_name"] == "lofi calm"

    chosen = run(doc, {"op": "set_setting", "path": "music", "value": {"track": "mine.mp3", "track_name": "Mine"}}).doc
    assert run(chosen, op, internal=True).doc["settings"]["music"]["track"] == "mine.mp3"   # never overwrites a pick
    off = run(doc, {"op": "set_setting", "path": "music", "value": {"enabled": False}}).doc
    assert run(off, op, internal=True).doc["settings"]["music"]["track"] is None
    for bad in ("../etc/passwd", "a/b.mp3", ""):
        assert run(doc, {**op, "track": bad}, internal=True).doc["settings"]["music"]["track"] is None
