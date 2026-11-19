# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Filler detection in any language (backend/services/filler_words.py), and the silence pass that uses it.

"Remove silence & filler words" used to match an English word list, so a Chinese, Spanish or Japanese clip never
lost a filler, and "so" / "like" / "right" were cut with no context. The configured AI model now reads the clip's
own words; the code only cuts what it returns, and says so when it couldn't ask.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.services import filler_words as fw


class FakeAI:
    def __init__(self, answer):
        self.answer = answer          # callable(prompt) -> reply text, or an exception to raise
        self.prompts: list[str] = []

    async def chat(self, messages, system=None, max_tokens=2048):
        # Same signature as AIClient.chat: a call passing anything else would TypeError on a real client.
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer(prompt)


@pytest.fixture
def ai(monkeypatch):
    holder = {}

    def install(answer):
        fake = FakeAI(answer)

        def get_ai_client(user_settings=None, **kw):
            holder["settings"] = user_settings
            return fake
        monkeypatch.setattr("backend.core.ai_provider.get_ai_client", get_ai_client)
        holder["ai"] = fake
        return fake
    install.holder = holder
    return install


def words(*texts):
    return [{"text": t, "start": i, "end": i + 0.5} for i, t in enumerate(texts)]


# ── find_fillers ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_chinese_filler_is_found(ai):
    fake = ai(lambda p: '{"fillers": [1]}')
    w = words("我们", "嗯", "开始", "吧")
    assert await fw.find_fillers(w) == {1}
    assert "1 嗯" in fake.prompts[0] and "any language" in fake.prompts[0]


@pytest.mark.asyncio
async def test_the_users_own_ai_settings_are_used(ai):
    """BYOK: the key and model saved in Settings, not only an env key."""
    ai(lambda p: '{"fillers": []}')
    marker = object()
    await fw.find_fillers(words("um", "hi"), marker)
    assert ai.holder["settings"] is marker


@pytest.mark.asyncio
async def test_fenced_or_chatty_replies_parse(ai):
    ai(lambda p: 'Here you go:\n```json\n{"fillers": [0, 2]}\n```')
    assert await fw.find_fillers(words("um", "so", "uh", "this", "is", "fine", "ok", "yes", "and", "done")) == {0, 2}


@pytest.mark.asyncio
async def test_indices_out_of_range_or_not_ints_are_dropped(ai):
    ai(lambda p: '{"fillers": [1, 99, -1, "2", 1.5, true, 3.0]}')
    out = await fw.find_fillers(words(*[f"w{i}" for i in range(20)]))
    assert out == {1, 3}


@pytest.mark.asyncio
async def test_a_chunk_marked_too_heavily_is_not_trusted(ai):
    ai(lambda p: json.dumps({"fillers": list(range(6))}))
    assert await fw.find_fillers(words(*[f"w{i}" for i in range(20)])) == set()     # 6/20 > 25%


@pytest.mark.asyncio
async def test_a_short_chunk_is_not_judged_by_share(ai):
    """A clip's last few words can be half fillers ("um, uh, okay"); share means nothing at that size."""
    ai(lambda p: '{"fillers": [0, 1]}')
    assert await fw.find_fillers(words("um", "uh", "thanks", "everyone")) == {0, 1}


@pytest.mark.asyncio
async def test_the_model_sees_the_sentence_and_the_numbered_words(ai):
    fake = ai(lambda p: '{"fillers": []}')
    await fw.find_fillers(words("este", "libro", "es", "bueno"))
    assert "Text:\neste libro es bueno" in fake.prompts[0] and "1 libro" in fake.prompts[0]


@pytest.mark.asyncio
async def test_no_ai_key_is_none(monkeypatch):
    from backend.core.exceptions import AIKeyMissingError

    def no_key(*a, **kw):
        raise AIKeyMissingError("add a key in Settings")
    monkeypatch.setattr("backend.core.ai_provider.get_ai_client", no_key)
    assert await fw.find_fillers(words("um", "hi")) is None


@pytest.mark.asyncio
async def test_every_chunk_failing_is_none_and_one_failing_keeps_the_rest(ai, monkeypatch):
    ai(RuntimeError("provider down"))
    assert await fw.find_fillers(words("um", "hi")) is None

    monkeypatch.setattr(fw, "CHUNK_WORDS", 10)
    calls = {"n": 0}

    def flaky(prompt):
        calls["n"] += 1
        if "0 bad" in prompt:
            return "not json at all"
        return '{"fillers": [0]}'
    ai(flaky)
    w = words(*(["bad"] + [f"a{i}" for i in range(9)] + ["um"] + [f"b{i}" for i in range(9)]))
    assert await fw.find_fillers(w) == {10}
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_chunks_are_offset_and_capped(ai, monkeypatch):
    monkeypatch.setattr(fw, "CHUNK_WORDS", 10)
    monkeypatch.setattr(fw, "MAX_CHUNKS", 2)
    fake = ai(lambda p: '{"fillers": [1]}')
    out = await fw.find_fillers(words(*[f"w{i}" for i in range(35)]))
    assert out == {1, 11}                 # third and fourth chunks past the cap: silences only
    assert len(fake.prompts) == 2


@pytest.mark.asyncio
async def test_braces_in_words_dont_break_the_prompt(ai):
    ai(lambda p: '{"fillers": []}')
    assert await fw.find_fillers(words("{x}", "{{", "}")) == set()


@pytest.mark.asyncio
async def test_empty_is_empty():
    assert await fw.find_fillers([]) == set()


@pytest.mark.asyncio
async def test_a_slow_provider_is_not_checked_after_the_deadline(monkeypatch):
    monkeypatch.setattr(fw, "DEADLINE_S", 0.05)

    class Slow:
        async def chat(self, *a, **k):
            await asyncio.sleep(5)
    monkeypatch.setattr("backend.core.ai_provider.get_ai_client", lambda *a, **k: Slow())
    assert await fw.find_fillers(words("um", "hi")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ['{"fillers": "1"}', '{"fillers": null}', '{"other": [1]}', '"1"'])
async def test_an_answer_of_the_wrong_shape_is_not_checked(ai, reply):
    """A reply that isn't a list of numbers is "couldn't answer" (None: the caller says so), never "no fillers"."""
    ai(lambda p: reply)
    assert await fw.find_fillers(words("um", "hi")) is None


@pytest.mark.asyncio
async def test_a_bare_list_reply_is_read_too(ai):
    ai(lambda p: "[0]")
    assert await fw.find_fillers(words("um", "hi")) == {0}


@pytest.mark.asyncio
async def test_never_more_than_the_concurrency_limit_at_once(monkeypatch):
    monkeypatch.setattr(fw, "CHUNK_WORDS", 5)
    state = {"now": 0, "peak": 0, "calls": 0}

    class Counting:
        async def chat(self, *a, **k):
            state["now"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.01)
            state["now"] -= 1
            return '{"fillers": []}'
    monkeypatch.setattr("backend.core.ai_provider.get_ai_client", lambda *a, **k: Counting())
    assert await fw.find_fillers(words(*[f"w{i}" for i in range(50)])) == set()
    assert state["calls"] == 10 and state["peak"] == fw.CONCURRENCY


def test_the_prompt_keeps_meaning_and_prefers_leaving_a_filler_in():
    # The traps a real-model run caught before these lines existed.
    assert "A word that does grammar in its sentence is never a filler" in fw.FILLER_PROMPT
    for trap in ('"este libro"', '"那个人"', '"あの人"', '"do you know him"', '"like his dad"', '"吧"'):
        assert trap in fw.FILLER_PROMPT, trap
    assert "When unsure, do NOT mark it" in fw.FILLER_PROMPT
    assert "any language" in fw.FILLER_PROMPT and "Text:" in fw.FILLER_PROMPT


def test_no_english_word_list_is_left():
    import backend.services.clip_extractor as ce
    assert not hasattr(ce, "FILLER_WORDS")


# ── _remove_silence_and_fillers ─────────────────────────────────────────────

def _capture_select_expr(monkeypatch, ce):
    """Stop the pass at its ffmpeg call and hand back the keep expression it built."""
    kept = {}
    real_to_thread = ce.asyncio.to_thread

    async def to_thread(fn, *a, **k):
        if getattr(fn, "__name__", "") == "_run":
            cells = [c.cell_contents for c in (fn.__closure__ or ()) if isinstance(c.cell_contents, str)]
            kept["expr"] = next(c for c in cells if c.startswith("between("))
            raise RuntimeError("stop")
        return await real_to_thread(fn, *a, **k)
    monkeypatch.setattr(ce.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(ce, "_has_video_stream", lambda _p: True)
    monkeypatch.setattr("backend.services.video_utils.probe_duration", lambda *_a, **_k: 3.4)
    return kept


CHINESE = [{"start": 0.0, "end": 6.0, "text": "我们 嗯 那个 开始 吧", "words": [
    {"text": "我们", "start": 0.0, "end": 0.6}, {"text": "嗯", "start": 0.6, "end": 1.6},
    {"text": "那个", "start": 1.6, "end": 2.4}, {"text": "开始", "start": 2.4, "end": 3.0},
    {"text": "吧", "start": 3.0, "end": 3.4}]}]


@pytest.mark.asyncio
async def test_only_the_words_the_model_marked_are_cut(tmp_path, monkeypatch):
    from backend.services import clip_extractor as ce

    seen = {}

    async def marked(words, user_settings=None):
        seen["settings"] = user_settings
        return {i for i, w in enumerate(words) if w["text"] in ("嗯", "那个")}
    monkeypatch.setattr(fw, "find_fillers", marked)
    kept = _capture_select_expr(monkeypatch, ce)
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x" * 2048)
    marker = object()
    with pytest.raises(RuntimeError, match="stop"):
        await ce._remove_silence_and_fillers(clip, CHINESE, user_settings=marker)
    # 我们 kept (0-0.6 s); 嗯 + 那个 cut (0.6-2.4 s); 开始 吧 kept (2.4-3.4 s); 0.03 s pad either side.
    assert kept["expr"] == "between(t\\,0.000\\,0.630)+between(t\\,2.370\\,3.430)", kept["expr"]
    assert seen["settings"] is marker


@pytest.mark.asyncio
async def test_no_answer_means_silences_only_and_the_clipper_says_so(tmp_path, monkeypatch):
    from backend.services import clip_extractor as ce

    async def unavailable(words, user_settings=None):
        return None
    monkeypatch.setattr(fw, "find_fillers", unavailable)
    warned = []

    async def capture(**k):
        warned.append(k["constraint"])
    monkeypatch.setattr("backend.core.ws_manager.ws_manager.send_constraint_warning", capture)
    # No silence worth cutting and no fillers: the clip comes back untouched, with the notice.
    segments = [{"start": 0.0, "end": 2.0, "text": "um hello", "words": [
        {"text": "um", "start": 0.0, "end": 0.5}, {"text": "hello", "start": 0.5, "end": 2.0}]}]
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x" * 2048)
    out, segs = await ce._remove_silence_and_fillers(clip, segments, warn_user_id="local")
    assert out == clip and segs == segments          # "um" kept: nothing decided it was a filler
    assert warned == ["filler_check_unavailable"]


@pytest.mark.asyncio
async def test_a_cue_without_word_timings_is_never_cut_as_a_filler(tmp_path, monkeypatch):
    """Imported subtitles and YouTube subtitle tracks have no word timings, so a whole cue stands in as one
    "word". It is context for the model, never something to cut: it is a sentence."""
    from backend.services import clip_extractor as ce

    async def everything(words, user_settings=None):
        return set(range(len(words)))
    monkeypatch.setattr(fw, "find_fillers", everything)
    kept = _capture_select_expr(monkeypatch, ce)
    segments = [
        {"start": 0.0, "end": 1.0, "text": "a whole sentence from a subtitle track"},
        {"start": 1.0, "end": 3.4, "text": "um ok", "words": [
            {"text": "um", "start": 1.0, "end": 1.6}, {"text": "ok", "start": 2.8, "end": 3.4}]},
    ]
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x" * 2048)
    with pytest.raises(RuntimeError, match="stop"):
        await ce._remove_silence_and_fillers(clip, segments)
    # The cue (0-1 s) survives; both timed words were marked and are cut.
    assert kept["expr"] == "between(t\\,0.000\\,1.030)", kept["expr"]


@pytest.mark.asyncio
async def test_with_no_word_timings_at_all_nothing_is_asked(tmp_path, monkeypatch):
    from backend.services import clip_extractor as ce

    async def boom(words, user_settings=None):
        raise AssertionError("no word timings: there is nothing to ask about")
    monkeypatch.setattr(fw, "find_fillers", boom)
    warned = []

    async def capture(**k):
        warned.append(k["constraint"])
    monkeypatch.setattr("backend.core.ws_manager.ws_manager.send_constraint_warning", capture)
    segments = [{"start": 0.0, "end": 2.0, "text": "one cue"}, {"start": 2.1, "end": 4.0, "text": "another"}]
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x" * 2048)
    out, segs = await ce._remove_silence_and_fillers(clip, segments, warn_user_id="local")
    assert out == clip and segs == segments and warned == []
