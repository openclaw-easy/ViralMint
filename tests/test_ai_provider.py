# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend.core.ai_provider — the BYOK AI client factory.

Covers get_ai_client() resolution order (user BYOK → env → error),
_resolve_user_provider_and_key() decryption/validation, PROVIDER_DEFAULTS
model selection, and AIClient.chat() aggregating a mocked stream. No real
provider SDK is ever imported or called.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core import ai_provider as ap
from backend.core.ai_provider import (
    AIClient, AIProvider, PROVIDER_DEFAULTS, get_ai_client,
    _resolve_user_provider_and_key,
)
from backend.core.crypto import encrypt
from backend.core.exceptions import AIKeyMissingError


def _no_env_keys():
    """Patch all three env keys to empty so only the argument path is exercised."""
    return patch.multiple(
        ap.settings,
        ANTHROPIC_API_KEY="",
        OPENAI_API_KEY="",
        OPENROUTER_API_KEY="",
    )


# ── PROVIDER_DEFAULTS + AIClient construction ──────────────────────────────

def test_provider_defaults_cover_every_provider():
    for p in AIProvider:
        assert p in PROVIDER_DEFAULTS and PROVIDER_DEFAULTS[p]


def test_aiclient_uses_default_model_when_none():
    c = AIClient(AIProvider.ANTHROPIC, "sk-x")
    assert c.model == PROVIDER_DEFAULTS[AIProvider.ANTHROPIC]
    assert c.provider == AIProvider.ANTHROPIC and c.api_key == "sk-x"


def test_aiclient_honors_explicit_model():
    c = AIClient(AIProvider.OPENAI, "sk-y", model="gpt-custom")
    assert c.model == "gpt-custom"


# ── _resolve_user_provider_and_key ─────────────────────────────────────────

def test_resolve_none_when_no_settings():
    assert _resolve_user_provider_and_key(None) == (None, None)


def test_resolve_none_when_provider_or_key_missing():
    s = SimpleNamespace(ai_provider="", ai_api_key_encrypted="x")
    assert _resolve_user_provider_and_key(s) == (None, None)
    s2 = SimpleNamespace(ai_provider="anthropic", ai_api_key_encrypted=None)
    assert _resolve_user_provider_and_key(s2) == (None, None)


def test_resolve_none_when_provider_invalid():
    s = SimpleNamespace(ai_provider="not-a-provider", ai_api_key_encrypted=encrypt("k"))
    assert _resolve_user_provider_and_key(s) == (None, None)


def test_resolve_decrypts_valid_settings():
    s = SimpleNamespace(ai_provider="OpenAI", ai_api_key_encrypted=encrypt("secret-key"))
    provider, key = _resolve_user_provider_and_key(s)
    assert provider == AIProvider.OPENAI and key == "secret-key"


# ── get_ai_client resolution order ─────────────────────────────────────────

def test_raises_when_nothing_configured():
    with _no_env_keys(), pytest.raises(AIKeyMissingError):
        get_ai_client(None)


def test_env_anthropic_wins_first():
    with patch.multiple(ap.settings, ANTHROPIC_API_KEY="anthr", OPENAI_API_KEY="oai",
                        OPENROUTER_API_KEY="ork"):
        c = get_ai_client(None)
    assert c.provider == AIProvider.ANTHROPIC and c.api_key == "anthr"


def test_env_openai_when_anthropic_empty():
    with patch.multiple(ap.settings, ANTHROPIC_API_KEY="", OPENAI_API_KEY="oai",
                        OPENROUTER_API_KEY="ork"):
        c = get_ai_client(None)
    assert c.provider == AIProvider.OPENAI and c.api_key == "oai"


def test_env_openrouter_last_resort():
    with patch.multiple(ap.settings, ANTHROPIC_API_KEY="", OPENAI_API_KEY="",
                        OPENROUTER_API_KEY="ork"):
        c = get_ai_client(None)
    assert c.provider == AIProvider.OPENROUTER and c.api_key == "ork"


def test_user_byok_overrides_env_and_uses_user_model():
    s = SimpleNamespace(
        ai_provider="openrouter",
        ai_api_key_encrypted=encrypt("byok-key"),
        ai_model="anthropic/claude-custom",
    )
    with patch.multiple(ap.settings, ANTHROPIC_API_KEY="env-anthr"):
        c = get_ai_client(s)
    assert c.provider == AIProvider.OPENROUTER
    assert c.api_key == "byok-key"
    assert c.model == "anthropic/claude-custom"


def test_env_path_still_applies_user_model():
    s = SimpleNamespace(ai_provider="", ai_api_key_encrypted="", ai_model="my-model")
    with patch.multiple(ap.settings, ANTHROPIC_API_KEY="env-anthr"):
        c = get_ai_client(s)
    assert c.provider == AIProvider.ANTHROPIC and c.model == "my-model"


# ── chat() aggregates the provider stream ──────────────────────────────────

async def test_chat_aggregates_stream_and_routes_by_provider():
    c = AIClient(AIProvider.ANTHROPIC, "sk-x")

    async def _fake_stream(messages, system, max_tokens):
        for tok in ("Hel", "lo ", "world"):
            yield tok

    with patch.object(c, "_anthropic_stream", _fake_stream):
        out = await c.chat([{"role": "user", "content": "hi"}])
    assert out == "Hello world"


async def test_chat_stream_dispatches_to_openai_impl():
    c = AIClient(AIProvider.OPENAI, "sk-y")
    seen = {}

    async def _fake_openai(messages, system, max_tokens):
        seen["hit"] = True
        yield "x"

    with patch.object(c, "_openai_stream", _fake_openai):
        chunks = [t async for t in c.chat_stream([{"role": "user", "content": "q"}])]
    assert chunks == ["x"] and seen.get("hit")
