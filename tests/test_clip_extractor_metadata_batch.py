# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for the batched clip-metadata generator.

`_generate_clip_metadata_batch` replaces the per-clip-call pattern that
made N AI chat calls for an N-clip extraction. These tests lock in that:

  1. The batched path makes exactly ONE AI chat call regardless of N
     (the cost-correctness contract; if this breaks, the leak is back).
  2. Per-clip granularity is preserved: missing indices in the AI
     response return None for those slots so the downstream loop in
     `_process_clips_parallel` falls back to defaults and marks
     `metadata_status="fallback"` only for the failed clips, not all
     of them. The "AI meta failed" chip on ClipStudio.jsx depends on
     this.
  3. Hard failures (call raises, response not a list, zero valid
     entries) return None so the caller falls back to per-clip parallel
     calls — preserving today's robustness exactly.

The BYOK AI client is injected via `backend.core.ai_provider.get_ai_client`
so these tests never touch a real provider.
"""
from __future__ import annotations

import json

import pytest

from backend.services.clip_extractor import (
    CLIP_METADATA_BATCH_PROMPT,
    _default_metadata,
    _generate_clip_metadata_batch,
)


class _FakeAI:
    """Stand-in for the BYOK AIClient. Records every chat() call and returns
    whatever was queued. If the queue is empty, raises so a test that
    accidentally makes a second call fails loudly instead of hanging."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if not self._responses:
            raise AssertionError(
                "FakeAI: chat() called more times than responses queued — "
                "test expected only N calls"
            )
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture(autouse=True)
def _patch_ai_client(monkeypatch):
    """Inject a fake AI client so the batched generator never hits a real
    provider. The service does `from backend.core.ai_provider import
    get_ai_client` at call time, so patching the module attr is enough."""
    holder = {"ai": None}

    def _fake_get_ai_client(_user_settings):
        return holder["ai"]

    monkeypatch.setattr(
        "backend.core.ai_provider.get_ai_client", _fake_get_ai_client
    )
    yield holder


# ── Cost-correctness: ONE call regardless of N ───────────────────────────────


@pytest.mark.asyncio
async def test_one_call_for_one_clip(_patch_ai_client):
    """N=1 still goes through the batched path; no special single-clip case.
    Keeps the cost-correctness invariant trivial: ALWAYS one call."""
    _patch_ai_client["ai"] = _FakeAI([json.dumps([
        {"index": 0, "youtube_title": "T", "youtube_description": "D",
         "youtube_tags": ["a"], "tiktok_title": "TT"},
    ])])

    out = await _generate_clip_metadata_batch(
        [("clip title", "some transcript")], user_settings=None,
    )

    assert len(_patch_ai_client["ai"].calls) == 1
    assert out is not None and len(out) == 1
    assert out[0]["youtube_title"] == "T"


@pytest.mark.asyncio
async def test_one_call_for_thirty_four_clips(_patch_ai_client):
    """The 34-clip extraction case from the original bug report —
    must produce exactly ONE chat call, not 34. This is the headline
    cost-correctness contract. If this test ever needs to be relaxed,
    the metadata billing leak is back."""
    response = json.dumps([
        {
            "index": i,
            "youtube_title": f"Title {i}",
            "youtube_description": f"Desc {i}",
            "youtube_tags": [f"tag{i}"],
            "tiktok_title": f"TT {i}",
        }
        for i in range(34)
    ])
    _patch_ai_client["ai"] = _FakeAI([response])

    clips = [(f"clip {i}", f"transcript {i}") for i in range(34)]
    out = await _generate_clip_metadata_batch(clips, user_settings=None)

    assert len(_patch_ai_client["ai"].calls) == 1, (
        "Batched metadata must make ONE chat call for any N clips. "
        "If this fails, the per-clip billing leak is back."
    )
    assert out is not None and len(out) == 34
    for i in range(34):
        assert out[i]["youtube_title"] == f"Title {i}"


@pytest.mark.asyncio
async def test_empty_clips_list_skips_ai_call(_patch_ai_client):
    """Zero clips → zero calls, empty list out. Defends against a future
    refactor that lets an empty meta_inputs through."""
    _patch_ai_client["ai"] = _FakeAI([])  # would raise if called

    out = await _generate_clip_metadata_batch([], user_settings=None)

    assert out == []
    assert _patch_ai_client["ai"].calls == []


# ── Per-clip granularity: missing index → None (not a default-merged dict) ───


@pytest.mark.asyncio
async def test_missing_index_returns_none_for_that_slot(_patch_ai_client):
    """If the AI omits clip 1 from a 3-clip request, the result has
    None at index 1. The downstream loop's `not metadata` check converts
    None → `_default_metadata(...)` + `metadata_status="fallback"`.
    Returning a pre-merged dict here would silently mark the failed
    clip as "ai_generated" — the UI's warning chip would never light."""
    _patch_ai_client["ai"] = _FakeAI([json.dumps([
        {"index": 0, "youtube_title": "Got 0", "youtube_description": "",
         "youtube_tags": [], "tiktok_title": ""},
        # index 1 deliberately missing
        {"index": 2, "youtube_title": "Got 2", "youtube_description": "",
         "youtube_tags": [], "tiktok_title": ""},
    ])])

    out = await _generate_clip_metadata_batch(
        [("a", "x"), ("b", "y"), ("c", "z")], user_settings=None,
    )

    assert out is not None and len(out) == 3
    assert isinstance(out[0], dict) and out[0]["youtube_title"] == "Got 0"
    assert out[1] is None, "missing index must yield None — preserves UI granularity"
    assert isinstance(out[2], dict) and out[2]["youtube_title"] == "Got 2"


@pytest.mark.asyncio
async def test_ai_returns_items_in_wrong_order(_patch_ai_client):
    """Indices are the source of truth, not array position. The model
    occasionally returns 2-0-1 ordering; the batched function must
    re-bind by `index` so each clip gets its own metadata, not the
    one positionally next to it in the response."""
    _patch_ai_client["ai"] = _FakeAI([json.dumps([
        {"index": 2, "youtube_title": "Clip2", "youtube_description": "",
         "youtube_tags": [], "tiktok_title": ""},
        {"index": 0, "youtube_title": "Clip0", "youtube_description": "",
         "youtube_tags": [], "tiktok_title": ""},
        {"index": 1, "youtube_title": "Clip1", "youtube_description": "",
         "youtube_tags": [], "tiktok_title": ""},
    ])])

    out = await _generate_clip_metadata_batch(
        [("a", "x"), ("b", "y"), ("c", "z")], user_settings=None,
    )

    assert out is not None and len(out) == 3
    assert out[0]["youtube_title"] == "Clip0"
    assert out[1]["youtube_title"] == "Clip1"
    assert out[2]["youtube_title"] == "Clip2"


@pytest.mark.asyncio
async def test_ai_returns_extra_index_out_of_range(_patch_ai_client):
    """If the model invents an index=99 for a 2-clip request, it's
    silently dropped (out-of-range), not stored under any other slot."""
    _patch_ai_client["ai"] = _FakeAI([json.dumps([
        {"index": 0, "youtube_title": "real",
         "youtube_description": "", "youtube_tags": [], "tiktok_title": ""},
        {"index": 99, "youtube_title": "junk",
         "youtube_description": "", "youtube_tags": [], "tiktok_title": ""},
    ])])

    out = await _generate_clip_metadata_batch(
        [("a", "x"), ("b", "y")], user_settings=None,
    )

    assert out is not None and len(out) == 2
    assert out[0]["youtube_title"] == "real"
    assert out[1] is None, "out-of-range index 99 must not pollute another slot"


@pytest.mark.asyncio
async def test_partial_ai_entry_merges_with_defaults(_patch_ai_client):
    """If the model returns 3 of 4 fields, the missing one falls back
    to the default (matching the legacy per-clip `{**defaults, **parsed}`
    behaviour). Downstream readers always see all four fields."""
    _patch_ai_client["ai"] = _FakeAI([json.dumps([
        {"index": 0, "youtube_title": "AI-title"},
        # missing: youtube_description, youtube_tags, tiktok_title
    ])])

    out = await _generate_clip_metadata_batch(
        [("Source Title", "abc transcript")], user_settings=None,
    )

    assert out is not None and len(out) == 1
    entry = out[0]
    assert entry["youtube_title"] == "AI-title"
    # Missing fields fall back to per-clip defaults:
    assert entry["youtube_description"] == "abc transcript"[:200]
    assert entry["youtube_tags"] == []
    assert entry["tiktok_title"] == "Source Title"


@pytest.mark.asyncio
async def test_index_field_stripped_from_stored_entry(_patch_ai_client):
    """The `index` field is a routing hint — downstream consumers
    (DB columns, API serializers, frontend) don't expect it. Must be
    stripped before the dict is stored."""
    _patch_ai_client["ai"] = _FakeAI([json.dumps([
        {"index": 0, "youtube_title": "T", "youtube_description": "D",
         "youtube_tags": ["a"], "tiktok_title": "TT"},
    ])])

    out = await _generate_clip_metadata_batch(
        [("title", "text")], user_settings=None,
    )

    assert out is not None and "index" not in out[0]


# ── Hard failures: return None so caller falls back to per-clip ─────────────


@pytest.mark.asyncio
async def test_chat_call_raises_returns_none(_patch_ai_client):
    """Transient provider errors (network, timeout) bubble up as exceptions
    inside `ai.chat()`. The batched function must catch + return None
    so the caller falls back to N parallel per-clip calls — that
    fallback path is what preserves today's robustness exactly."""
    _patch_ai_client["ai"] = _FakeAI([RuntimeError("provider timeout")])

    out = await _generate_clip_metadata_batch(
        [("a", "x"), ("b", "y")], user_settings=None,
    )

    assert out is None, "chat() exception must return None to trigger caller fallback"


@pytest.mark.asyncio
async def test_response_not_a_list_returns_none(_patch_ai_client):
    """Model returns a bare string / number / unrelated dict — caller
    must fall back to per-clip retry, not stuff garbage downstream."""
    _patch_ai_client["ai"] = _FakeAI(['"hello"'])

    out = await _generate_clip_metadata_batch(
        [("a", "x")], user_settings=None,
    )

    assert out is None


@pytest.mark.asyncio
async def test_zero_valid_entries_returns_none(_patch_ai_client):
    """Response is a list but every item is structurally invalid
    (missing `index`, non-dict, etc.). No clip has metadata; fall
    back to per-clip retry."""
    _patch_ai_client["ai"] = _FakeAI([json.dumps([
        "not a dict",
        {"no_index_field": True},
        {"index": "not-an-int"},
    ])])

    out = await _generate_clip_metadata_batch(
        [("a", "x"), ("b", "y")], user_settings=None,
    )

    assert out is None


@pytest.mark.asyncio
async def test_empty_response_returns_none(_patch_ai_client):
    """Response is an empty array. No clip has metadata → fall back."""
    _patch_ai_client["ai"] = _FakeAI(["[]"])

    out = await _generate_clip_metadata_batch(
        [("a", "x"), ("b", "y")], user_settings=None,
    )

    assert out is None


# ── Defensive wrapper-shape parsing ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_response_wrapped_in_clips_key(_patch_ai_client):
    """Some models wrap the array in {"clips": [...]} instead of returning
    a bare array. The parser handles common wrapper keys so an otherwise-
    valid response isn't discarded over packaging."""
    _patch_ai_client["ai"] = _FakeAI([json.dumps({"clips": [
        {"index": 0, "youtube_title": "T", "youtube_description": "",
         "youtube_tags": [], "tiktok_title": ""},
    ]})])

    out = await _generate_clip_metadata_batch(
        [("a", "x")], user_settings=None,
    )

    assert out is not None and out[0]["youtube_title"] == "T"


@pytest.mark.asyncio
async def test_response_wrapped_in_results_key(_patch_ai_client):
    """Same defensive handling for {"results": [...]}."""
    _patch_ai_client["ai"] = _FakeAI([json.dumps({"results": [
        {"index": 0, "youtube_title": "R", "youtube_description": "",
         "youtube_tags": [], "tiktok_title": ""},
    ]})])

    out = await _generate_clip_metadata_batch(
        [("a", "x")], user_settings=None,
    )

    assert out is not None and out[0]["youtube_title"] == "R"


@pytest.mark.asyncio
async def test_markdown_fenced_response_parsed(_patch_ai_client):
    """Despite "no markdown fences" in the prompt, models sometimes
    wrap output in ```json blocks. _parse_json_response strips them
    before json.loads. Verify the batched path inherits that tolerance."""
    fenced = "```json\n" + json.dumps([
        {"index": 0, "youtube_title": "F", "youtube_description": "",
         "youtube_tags": [], "tiktok_title": ""},
    ]) + "\n```"
    _patch_ai_client["ai"] = _FakeAI([fenced])

    out = await _generate_clip_metadata_batch(
        [("a", "x")], user_settings=None,
    )

    assert out is not None and out[0]["youtube_title"] == "F"


# ── Token budget ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_tokens_scales_with_clip_count(_patch_ai_client):
    """A 1-clip request shouldn't reserve 8K tokens; a 30-clip request
    must reserve enough headroom for ~30 × 200 = 6K. The scaling
    formula is `min(8000, max(2000, N * 200 + 500))`."""
    _patch_ai_client["ai"] = _FakeAI([
        json.dumps([{"index": 0, "youtube_title": "T",
                     "youtube_description": "", "youtube_tags": [],
                     "tiktok_title": ""}]),
        json.dumps([
            {"index": i, "youtube_title": f"T{i}", "youtube_description": "",
             "youtube_tags": [], "tiktok_title": ""} for i in range(30)
        ]),
    ])

    await _generate_clip_metadata_batch([("a", "x")], user_settings=None)
    small_call = _patch_ai_client["ai"].calls[0]

    await _generate_clip_metadata_batch(
        [(f"t{i}", f"x{i}") for i in range(30)], user_settings=None,
    )
    big_call = _patch_ai_client["ai"].calls[1]

    assert small_call["max_tokens"] == 2000, (
        "1-clip request should hit the 2000 floor"
    )
    assert big_call["max_tokens"] == 30 * 200 + 500, (
        "30-clip request should reserve 30 × 200 + 500 = 6500 tokens"
    )


# ── Empty-transcript handling ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_transcript_passes_no_speech_placeholder(_patch_ai_client):
    """Silent clips have empty transcripts. Sending an empty string into
    the prompt would leave the model with nothing to ground its output
    on; we substitute "(no speech)" so the title alone drives metadata."""
    _patch_ai_client["ai"] = _FakeAI([json.dumps([
        {"index": 0, "youtube_title": "T", "youtube_description": "",
         "youtube_tags": [], "tiktok_title": ""},
    ])])

    await _generate_clip_metadata_batch(
        [("Silent clip title", "")], user_settings=None,
    )

    prompt = _patch_ai_client["ai"].calls[0]["messages"][0]["content"]
    assert "(no speech)" in prompt
    assert "Silent clip title" in prompt


# ── Prompt-format-string compatibility ──────────────────────────────────────


def test_batch_prompt_formats_cleanly():
    """The batch prompt must format without leaving stray placeholders.
    Mirrors the equivalent test for CLIP_SELECTION_PROMPT in
    test_clip_extractor_platform_bias.py."""
    out = CLIP_METADATA_BATCH_PROMPT.format(
        count=3,
        clips_block="Clip 0 | title: a\nTranscript: x",
    )
    assert "{count}" not in out
    assert "{clips_block}" not in out
    assert "3 viral short clips" in out


# ── Defaults contract (the fallback path the downstream loop hits) ──────────


def test_default_metadata_has_all_four_fields():
    """The downstream loop in _process_clips_parallel substitutes
    `_default_metadata(...)` whenever the batched function returns
    None for a slot. That default MUST have all four fields, otherwise
    DB writes and frontend renders see None where strings are expected."""
    out = _default_metadata("My Title", "Some transcript text" * 20)
    assert set(out.keys()) == {
        "youtube_title", "youtube_description", "youtube_tags", "tiktok_title",
    }
    assert out["youtube_title"] == "My Title"
    assert out["tiktok_title"] == "My Title"
    assert isinstance(out["youtube_tags"], list)
    assert len(out["youtube_description"]) <= 200
