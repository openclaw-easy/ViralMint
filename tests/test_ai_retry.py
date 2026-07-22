# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend.core.ai_retry — the AI-assisted error-recovery helpers.

These wrap get_ai_client().chat() to repair failed URLs, refine empty
searches, fix malformed JSON/action blocks, and salvage unparsed API
responses. Every helper must degrade gracefully (return None on any AI
error) and never raise. The AI client is fully mocked — no network.
"""
from unittest.mock import AsyncMock, patch

import pytest

from backend.core import ai_retry


def _mock_ai(response=None, raise_exc=None):
    """Patch get_ai_client so .chat() returns `response` or raises `raise_exc`."""
    client = AsyncMock()
    if raise_exc is not None:
        client.chat.side_effect = raise_exc
    else:
        client.chat.return_value = response
    return patch.object(ai_retry, "get_ai_client", return_value=client)


# ── ai_fix_url ─────────────────────────────────────────────────────────────

async def test_fix_url_returns_corrected():
    with _mock_ai("https://youtube.com/watch?v=abc"):
        out = await ai_retry.ai_fix_url("youtube.com/watch?v=abc", "bad url")
    assert out == "https://youtube.com/watch?v=abc"


async def test_fix_url_strips_quotes_and_backticks():
    with _mock_ai('  "`https://x.com/v`"  '):
        out = await ai_retry.ai_fix_url("http://old.com/v", "err")
    assert out == "https://x.com/v"


async def test_fix_url_skip_returns_none():
    with _mock_ai("SKIP"):
        assert await ai_retry.ai_fix_url("https://x.com", "private video") is None


async def test_fix_url_identical_returns_none():
    same = "https://x.com/v"
    with _mock_ai(same):
        assert await ai_retry.ai_fix_url(same, "err") is None


async def test_fix_url_non_url_returns_none():
    with _mock_ai("just some words"):
        assert await ai_retry.ai_fix_url("https://x.com", "err") is None


async def test_fix_url_ai_error_returns_none():
    with _mock_ai(raise_exc=RuntimeError("no key")):
        assert await ai_retry.ai_fix_url("https://x.com", "err") is None


# ── ai_refine_search ───────────────────────────────────────────────────────

async def test_refine_search_returns_refined():
    with _mock_ai('"cat videos"'):
        out = await ai_retry.ai_refine_search("youtube", "obscure niche term")
    assert out == "cat videos"


async def test_refine_search_same_as_niche_returns_none():
    with _mock_ai("Cooking"):
        assert await ai_retry.ai_refine_search("youtube", "cooking") is None


async def test_refine_search_too_long_returns_none():
    with _mock_ai("x" * 150):
        assert await ai_retry.ai_refine_search("youtube", "n") is None


async def test_refine_search_ai_error_returns_none():
    with _mock_ai(raise_exc=ValueError("boom")):
        assert await ai_retry.ai_refine_search("youtube", "n") is None


# ── ai_fix_json ────────────────────────────────────────────────────────────

async def test_fix_json_parses_object():
    with _mock_ai('{"a": 1}'):
        out = await ai_retry.ai_fix_json('{a:1}', "expecting property name")
    assert out == {"a": 1}


async def test_fix_json_strips_markdown_fence():
    with _mock_ai('```json\n{"a": 2}\n```'):
        out = await ai_retry.ai_fix_json("broken", "err")
    assert out == {"a": 2}


async def test_fix_json_unparseable_returns_none():
    with _mock_ai("still not json"):
        assert await ai_retry.ai_fix_json("broken", "err") is None


async def test_fix_json_ai_error_returns_none():
    with _mock_ai(raise_exc=RuntimeError("x")):
        assert await ai_retry.ai_fix_json("broken", "err") is None


# ── ai_parse_api_response ──────────────────────────────────────────────────

async def test_parse_api_returns_list():
    with _mock_ai('[{"aweme_id": "1"}, {"aweme_id": "2"}]'):
        out = await ai_retry.ai_parse_api_response("<html>", "youtube", "cats")
    assert isinstance(out, list) and len(out) == 2


async def test_parse_api_empty_list_returns_none():
    with _mock_ai("[]"):
        assert await ai_retry.ai_parse_api_response("<html>", "tiktok", "k") is None


async def test_parse_api_strips_fence():
    with _mock_ai('```\n[{"aweme_id": "9"}]\n```'):
        out = await ai_retry.ai_parse_api_response("raw", "reddit", "k")
    assert out == [{"aweme_id": "9"}]


async def test_parse_api_ai_error_returns_none():
    with _mock_ai(raise_exc=Exception("net")):
        assert await ai_retry.ai_parse_api_response("raw", "youtube", "k") is None


# ── ai_fix_action ──────────────────────────────────────────────────────────

async def test_fix_action_returns_dict_with_type():
    with _mock_ai('{"type": "start_scout", "niche": "cats"}'):
        out = await ai_retry.ai_fix_action('{type:start_scout}', "err")
    assert out == {"type": "start_scout", "niche": "cats"}


async def test_fix_action_missing_type_returns_none():
    with _mock_ai('{"niche": "cats"}'):
        assert await ai_retry.ai_fix_action("broken", "err") is None


async def test_fix_action_strips_fence():
    with _mock_ai('```json\n{"type": "download_url", "url": "u"}\n```'):
        out = await ai_retry.ai_fix_action("broken", "err")
    assert out["type"] == "download_url"


async def test_fix_action_ai_error_returns_none():
    with _mock_ai(raise_exc=RuntimeError("x")):
        assert await ai_retry.ai_fix_action("broken", "err") is None
