# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""studio_doc — the Smart Video Studio project document.

Pure module: no DB, no network. Pins the script-packing contract (long
scripts are re-packed, never truncated) and `validate_doc` against hostile
JSON (unknown kinds/states, forged takes, file: / javascript: URLs).
"""
import asyncio
import copy
import hashlib
import json

import pytest

from backend.core.exceptions import StudioDocError
from backend.services import studio_doc as sd


def key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def scene(uid="s1", line="Hello there.", **over):
    base = {
        "uid": uid, "line": line, "voice": {"state": "none"},
        "visual": {"kind": "stock", "state": "placeholder", "query": "city"},
    }
    base.update(over)
    return base


def doc(*scenes, **over):
    d = {"aspect_ratio": "9:16", "settings": sd.default_settings(), "scenes": list(scenes) or [scene()]}
    d.update(over)
    return d


# ── scripts ─────────────────────────────────────────────────────────────────

def test_long_scripts_are_repacked_never_truncated():
    sentences = [f"This is sentence {i + 1} of a long voiceover script." for i in range(40)]
    lines = sd.lines_for_new_project(" ".join(sentences))
    assert len(lines) <= sd.SCENE_MAX
    assert " ".join(lines) == " ".join(sentences)
    assert all(len(line) <= sd.LINE_MAX for line in lines)


def test_a_script_too_long_for_one_video_is_refused():
    huge = " ".join(("word " * 70).strip() + "." for _ in range(sd.SCENE_MAX + 4))
    with pytest.raises(StudioDocError, match="too long"):
        sd.lines_for_new_project(huge)


def test_an_empty_script_is_refused():
    with pytest.raises(StudioDocError, match="at least one sentence"):
        sd.lines_for_new_project("   ")


def test_a_sentence_over_the_line_limit_splits_at_words():
    sentence = " ".join(["alpha"] * 150) + "."
    lines = sd.plan_lines(sentence)
    assert all(len(line) <= sd.LINE_MAX for line in lines)
    assert " ".join(lines) == sentence


def test_cjk_without_spaces_is_hard_cut_not_dropped():
    text = "今天" * 300 + "。"
    lines = sd.plan_lines(text)
    assert "".join(lines) == text
    assert all(len(line) <= sd.LINE_MAX for line in lines)


def test_short_scripts_group_one_or_two_sentences():
    lines = sd.plan_lines("One. Two. Three. Four. Five.")
    assert lines == ["One. Two.", "Three. Four.", "Five."]


def test_a_long_token_mid_sentence_is_split_not_dropped():
    """A token longer than LINE_MAX that isn't the sentence's last word must
    be split too — appended whole, it would later be cut at LINE_MAX and the
    rest silently lost."""
    url = "https://example.com/" + "a" * 430
    sentence = f"Read {url} before you start the rest of the steps today"
    lines = sd.lines_for_new_project(sentence)
    assert all(len(line) <= sd.LINE_MAX for line in lines)
    assert "".join(lines).replace(" ", "") == sentence.replace(" ", "")   # nothing lost


# ── validation ──────────────────────────────────────────────────────────────

def test_clean_document_keeps_known_fields_and_drops_the_rest():
    raw = doc(scene(extra="x", visual={"kind": "stock", "state": "placeholder", "query": "c", "evil": 1}), rogue=True)
    out = sd.validate_doc(raw)
    assert "rogue" not in out
    assert "extra" not in out["scenes"][0]
    assert "evil" not in out["scenes"][0]["visual"]
    assert out["v"] == sd.DOC_VERSION


@pytest.mark.parametrize("bad", [None, [], "doc", {"scenes": []}, {"scenes": "x", "settings": {}}])
def test_not_a_document(bad):
    with pytest.raises(StudioDocError):
        sd.validate_doc(bad)


def test_scene_cap_and_duplicate_ids():
    with pytest.raises(StudioDocError, match=f"at most {sd.SCENE_MAX}"):
        sd.validate_doc(doc(*[scene(uid=f"s{i}") for i in range(sd.SCENE_MAX + 1)]))
    with pytest.raises(StudioDocError, match="duplicate"):
        sd.validate_doc(doc(scene(uid="a"), scene(uid="a")))


def test_line_over_limit_is_an_error_not_a_silent_cut():
    with pytest.raises(StudioDocError, match="at most 400"):
        sd.validate_doc(doc(scene(line="x" * 401)))


def test_unknown_kinds_and_states_are_rejected():
    with pytest.raises(StudioDocError, match="unknown visual kind"):
        sd.validate_doc(doc(scene(visual={"kind": "deepfake", "state": "bound"})))
    with pytest.raises(StudioDocError, match="not a valid state"):
        sd.validate_doc(doc(scene(visual={"kind": "stock", "state": "generating"})))
    with pytest.raises(StudioDocError, match="not a valid state"):
        sd.validate_doc(doc(scene(visual={"kind": "user_clip", "state": "placeholder",
                                          "asset": {"asset_id": "as_1"}})))
    with pytest.raises(StudioDocError, match="unknown voice state"):
        sd.validate_doc(doc(scene(voice={"state": "singing"})))


def test_a_bound_stock_visual_without_an_asset_is_a_placeholder():
    out = sd.validate_doc(doc(scene(visual={"kind": "stock", "state": "bound", "query": "q"})))
    assert out["scenes"][0]["visual"]["state"] == "placeholder"
    assert out["scenes"][0]["visual"]["asset"] is None


def test_ready_voice_needs_a_known_take():
    k = key("a")
    raw = doc(scene(voice={"state": "ready", "take_key": k, "duration": 2.5}))
    assert sd.validate_doc(raw)["scenes"][0]["voice"]["take_key"] == k
    with pytest.raises(StudioDocError, match="unknown voice take"):
        sd.validate_doc(raw, take_known=lambda _k: False)
    with pytest.raises(StudioDocError, match="has no take"):
        sd.validate_doc(doc(scene(voice={"state": "ready", "take_key": "k_forged"})))


def test_settle_turns_inflight_states_into_settled_ones():
    k = key("prev")
    raw = doc(
        scene(uid="a", voice={"state": "generating"}),
        scene(uid="b", voice={"state": "generating", "take_key": k, "duration": 3}),
    )
    out = sd.validate_doc(raw)
    assert out["scenes"][0]["voice"] == {"state": "none"}
    assert out["scenes"][1]["voice"]["state"] == "stale"
    kept = sd.validate_doc(copy.deepcopy(raw), settle=False)
    assert kept["scenes"][0]["voice"]["state"] == "generating"
    assert kept["scenes"][1]["voice"]["state"] == "generating"


def test_a_scene_without_a_line_has_no_take():
    out = sd.validate_doc(doc(scene(line="", voice={"state": "ready", "take_key": key("x"), "duration": 1})))
    assert out["scenes"][0]["voice"] == {"state": "none"}


def test_urls_must_be_http_or_our_api():
    raw = doc(scene(visual={"kind": "stock", "state": "bound", "query": "q", "asset": {
        "provider": "pexels", "download_url": "file:///etc/passwd", "preview_url": "javascript:alert(1)",
        "thumbnail": "https://images.pexels.com/x.jpg",
    }}))
    asset = sd.validate_doc(raw)["scenes"][0]["visual"]["asset"]
    assert "download_url" not in asset and "preview_url" not in asset
    assert asset["thumbnail"].startswith("https://")


def test_malformed_alternates_are_dropped_not_fatal():
    out = sd.validate_doc(doc(scene(visual={"kind": "stock", "state": "placeholder", "query": "q", "alternates": {"0": "x"}})))
    assert out["scenes"][0]["visual"]["alternates"] == []


def test_user_clip_needs_an_asset_id():
    with pytest.raises(StudioDocError, match="your clip has no asset"):
        sd.validate_doc(doc(scene(visual={"kind": "user_clip", "state": "bound", "asset": {"title": "x"}})))


def test_settings_are_clamped_and_music_track_is_a_bare_filename():
    settings = sd.default_settings()
    settings["music"] |= {"volume_db": -99, "offset_s": 1e9, "track": "../../etc/passwd", "source": "ai"}
    settings["tail_gap_s"] = 5
    settings["voice"] = {"provider": "robot", "voice": None}
    settings["post"] = {"auto_zoom": "yes", "sfx": True, "caption_emoji": "fireworks", "rogue": 1}
    out = sd.validate_doc(doc(settings=settings))["settings"]
    assert out["music"]["volume_db"] == -30
    assert out["music"]["offset_s"] == 600
    assert out["music"]["track"] is None
    assert out["music"]["source"] == "library"
    assert out["tail_gap_s"] == sd.TAIL_GAP_MAX
    assert out["voice"]["provider"] == "edge_tts"
    assert out["post"] == {"auto_zoom": None, "sfx": True, "caption_emoji": None}


def test_missing_settings_is_an_error():
    with pytest.raises(StudioDocError, match="no settings"):
        sd.validate_doc({"scenes": [scene()]})


# ── timing ──────────────────────────────────────────────────────────────────

def test_only_a_ready_take_times_a_scene():
    k = key("t")
    d = sd.validate_doc(doc(
        scene(uid="a", voice={"state": "ready", "take_key": k, "duration": 2.0}),
        scene(uid="b", voice={"state": "stale", "take_key": k, "duration": 9.0}, line="Two words."),
    ))
    t = sd.derive_timeline(d)
    assert t["durations"][0] == pytest.approx(2.15)
    assert t["estimated"] == [False, True]
    assert t["durations"][1] == pytest.approx(sd.estimate_line_seconds("Two words.") + 0.15)
    assert t["starts"][1] == pytest.approx(2.15)


def test_enums_are_served_from_one_place():
    e = sd.enums()
    assert e["limits"]["line_max"] == 400 and e["limits"]["scene_max"] == 40
    assert set(e["visual_kinds"]) == {"stock", "user_clip"}
    assert e["music_sources"] == ["library"]


# ── voice providers ─────────────────────────────────────────────────────────

def test_openai_tts_is_its_own_provider_everywhere():
    """`openai_tts` is a real provider here (the user's own OpenAI key), not
    an alias — the document and the take's VoiceSpec keep it as sent."""
    from backend.services import voice_take_service as vts
    from backend.services.tts_service import normalise_tts_provider

    assert normalise_tts_provider("openai_tts") == "openai_tts" and normalise_tts_provider("nope") is None
    settings = sd.default_settings()
    settings["voice"] = {"provider": "openai_tts", "voice": "nova"}
    out = sd.validate_doc({"settings": settings, "scenes": [sd.make_scene("Hello there.", query="hello")]})
    assert out["settings"]["voice"]["provider"] == "openai_tts" and out["settings"]["voice"]["voice"] == "nova"
    spec = vts.VoiceSpec.from_settings({"voice": {"provider": "openai_tts", "voice": "nova"}})
    assert spec.provider == "openai_tts"


def test_provider_traits_are_one_answer_and_served_with_the_enums():
    from backend.services.tts_service import is_paid_per_take, uses_heavy_pool

    assert is_paid_per_take("openai_tts") and not is_paid_per_take("edge_tts")
    assert not uses_heavy_pool("edge_tts") and not uses_heavy_pool("openai_tts")
    traits = sd.enums()["voice_providers"]
    assert traits["openai_tts"] == {"paid_per_take": True, "needs_key": True, "heavy": False}
    assert traits["edge_tts"] == {"paid_per_take": False, "needs_key": False, "heavy": False}


def test_the_music_bed_fades_like_the_preview(monkeypatch, tmp_path):
    """The render mixes with the project's fade_s (the editor's preview uses
    it); the defaults stay 1 s in / 2 s out."""
    import subprocess

    from backend.services import music_service

    seen = []

    class Done:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **_k):
        seen.append(cmd[cmd.index("-filter_complex") + 1])
        return Done()
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(music_service, "probe_duration", lambda *_a, **_k: 20.0)
    voice, bed = tmp_path / "v.mp3", tmp_path / "m.mp3"
    voice.write_bytes(b"x")
    bed.write_bytes(b"x")
    asyncio.run(music_service.mix_audio(voice, bed, tmp_path / "o.mp3", fade_in_s=0.5, fade_out_s=0.5))
    asyncio.run(music_service.mix_audio(voice, bed, tmp_path / "o2.mp3"))
    assert "afade=t=in:d=0.50" in seen[0] and "afade=t=out:st=19.50:d=0.50" in seen[0]
    assert "afade=t=in:d=1.00" in seen[1] and "afade=t=out:st=18.00:d=2.00" in seen[1]


# ── new projects: voice overrides ───────────────────────────────────────────

def test_a_provider_only_voice_override_does_not_keep_the_free_voice():
    """{"provider": "openai_tts"} must not keep Edge's voice id and the label
    "Andrew" while the take speaks as another voice."""
    from backend.database import init_db
    from backend.services import studio_store as store
    from backend.services.studio_doc import DEFAULT_VOICES, TTSProvider
    asyncio.run(init_db())
    row = asyncio.run(store.create_project(script="One line to voice here.",
                                           settings_overrides={"voice": {"provider": "openai_tts"}}))
    voice = json.loads(row.doc_json)["settings"]["voice"]
    assert voice["provider"] == "openai_tts"
    assert voice["voice"] == DEFAULT_VOICES[TTSProvider.OPENAI_TTS]
    assert voice.get("label") != "Andrew"


def test_a_same_provider_voice_override_relabels_the_voice():
    """A caller that sends a different Edge voice with no label must not keep
    showing "Andrew" while every take speaks as the new voice."""
    from backend.database import init_db
    from backend.services import studio_store as store
    asyncio.run(init_db())
    row = asyncio.run(store.create_project(script="One line to voice here.", settings_overrides={
        "voice": {"provider": "edge_tts", "voice": "en-US-AvaMultilingualNeural", "instructions": None}}))
    voice = json.loads(row.doc_json)["settings"]["voice"]
    assert voice["voice"] == "en-US-AvaMultilingualNeural"
    assert voice["label"] == "Ava"
    # A caller that sends its own label keeps it.
    row = asyncio.run(store.create_project(script="One line to voice here.", settings_overrides={
        "voice": {"provider": "edge_tts", "voice": "en-US-EmmaMultilingualNeural", "label": "Emma (US)"}}))
    assert json.loads(row.doc_json)["settings"]["voice"]["label"] == "Emma (US)"


def test_create_project_carries_transition_and_post_overrides():
    from backend.database import init_db
    from backend.services import studio_store as store
    asyncio.run(init_db())
    row = asyncio.run(store.create_project(script="One line to voice here.", settings_overrides={
        "transition": "smooth", "post": {"auto_zoom": True, "caption_emoji": "nonsense"}}))
    settings = json.loads(row.doc_json)["settings"]
    assert settings["transition"] == "smooth"
    assert settings["post"]["auto_zoom"] is True
    assert settings["post"]["sfx"] is False                # the new-project default survives the merge
    assert settings["post"]["caption_emoji"] is None       # an unknown style is dropped, not stored


def test_voice_label_from_id():
    from backend.services.studio_store import _voice_label
    assert _voice_label("en-US-AvaMultilingualNeural") == "Ava"
    assert _voice_label("zh-CN-XiaoxiaoNeural") == "Xiaoxiao"
    assert _voice_label("Kore") == "Kore"
