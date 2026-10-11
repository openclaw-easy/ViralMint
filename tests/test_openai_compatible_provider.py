# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The OpenAI-compatible provider (#43): any server that speaks OpenAI's
/chat/completions at a user-supplied base URL — Ollama, LM Studio, vLLM,
llama.cpp, DeepSeek, Groq, a corporate gateway.

What's pinned: the base URL is the configuration (the key is optional), a
model name is required, the request goes to that URL with classic
`max_tokens`, and the .env path uses CUSTOM_AI_* — never OPENAI_BASE_URL,
which the OpenAI SDK would read on its own. The SDK is stubbed; nothing
here calls out.
"""
from types import SimpleNamespace
from unittest.mock import patch
import sys

import pytest

from backend.core import ai_provider as ap
from backend.core.ai_provider import (
    AIClient, AIProvider, get_ai_client, normalize_base_url,
    _resolve_user_provider_and_key,
)
from backend.core.crypto import encrypt
from backend.core.exceptions import AIKeyMissingError

LOCAL = "http://localhost:11434/v1"


def _env(**over):
    base = dict(ANTHROPIC_API_KEY="", OPENAI_API_KEY="", OPENROUTER_API_KEY="",
                CUSTOM_AI_BASE_URL="", CUSTOM_AI_API_KEY="", CUSTOM_AI_MODEL="")
    base.update(over)
    return patch.multiple(ap.settings, **base)


def _user(**kw):
    d = dict(ai_provider="openai_compatible", ai_api_key_encrypted=None,
             ai_model="qwen3:14b", ai_base_url=LOCAL)
    d.update(kw)
    return SimpleNamespace(**d)


# ── base URL validation ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, want", [
    ("http://localhost:11434/v1/", "http://localhost:11434/v1"),
    ("  https://api.deepseek.com  ", "https://api.deepseek.com"),
    ("", ""), (None, ""),
])
def test_normalize_base_url(raw, want):
    assert normalize_base_url(raw) == want


@pytest.mark.parametrize("raw", ["localhost:11434/v1", "ftp://host/v1", "http://", "just text"])
def test_normalize_base_url_rejects_non_http(raw):
    with pytest.raises(ValueError):
        normalize_base_url(raw)


# ── resolving the user's settings ────────────────────────────────────────────

def test_a_base_url_without_a_key_counts_as_configured():
    assert _resolve_user_provider_and_key(_user()) == (AIProvider.OPENAI_COMPATIBLE, "")


def test_the_optional_key_is_decrypted():
    s = _user(ai_api_key_encrypted=encrypt("gsk-123"))
    assert _resolve_user_provider_and_key(s) == (AIProvider.OPENAI_COMPATIBLE, "gsk-123")


def test_without_a_base_url_it_is_not_configured():
    assert _resolve_user_provider_and_key(_user(ai_base_url="")) == (None, None)
    assert _resolve_user_provider_and_key(_user(ai_base_url=None)) == (None, None)


def test_settings_win_over_env_keys():
    with _env(ANTHROPIC_API_KEY="env-key"):
        c = get_ai_client(_user())
    assert c.provider == AIProvider.OPENAI_COMPATIBLE
    assert c.base_url == LOCAL and c.model == "qwen3:14b" and c.api_key == ""


def test_a_missing_model_is_a_clear_configuration_error():
    with _env(), pytest.raises(AIKeyMissingError, match="model name"):
        get_ai_client(_user(ai_model=None))


def test_without_a_base_url_the_env_path_still_applies():
    with _env(OPENAI_API_KEY="oai"):
        c = get_ai_client(_user(ai_base_url=""))
    assert c.provider == AIProvider.OPENAI


# ── the .env path ────────────────────────────────────────────────────────────

def test_env_custom_server_is_the_last_fallback():
    with _env(CUSTOM_AI_BASE_URL="http://127.0.0.1:1234/v1/", CUSTOM_AI_MODEL="local-model"):
        c = get_ai_client(None)
    assert c.provider == AIProvider.OPENAI_COMPATIBLE
    assert c.base_url == "http://127.0.0.1:1234/v1" and c.model == "local-model"


def test_env_keys_for_named_providers_win_over_the_custom_server():
    with _env(OPENROUTER_API_KEY="ork", CUSTOM_AI_BASE_URL=LOCAL, CUSTOM_AI_MODEL="m"):
        assert get_ai_client(None).provider == AIProvider.OPENROUTER


def test_env_custom_server_without_a_model_errors():
    with _env(CUSTOM_AI_BASE_URL=LOCAL), pytest.raises(AIKeyMissingError):
        get_ai_client(None)


def test_the_env_names_do_not_collide_with_the_openai_sdk():
    # The OpenAI SDK reads OPENAI_BASE_URL itself; using that name would
    # silently redirect the plain OpenAI provider too.
    assert not hasattr(ap.settings, "OPENAI_BASE_URL")


# ── the request itself ───────────────────────────────────────────────────────

class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def _chunk(text):
    if text is None:
        return SimpleNamespace(choices=[])
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


class _FakeOpenAI:
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.create_kwargs = None
        _FakeOpenAI.instances.append(self)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.create_kwargs = kwargs
        return _FakeStream([_chunk(None), _chunk("Hel"), _chunk("lo")])


@pytest.fixture()
def fake_openai(monkeypatch):
    _FakeOpenAI.instances = []
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=_FakeOpenAI))
    return _FakeOpenAI


async def test_the_request_goes_to_the_users_server(fake_openai):
    c = AIClient(AIProvider.OPENAI_COMPATIBLE, "", model="qwen3:14b", base_url=LOCAL)
    out = await c.chat([{"role": "user", "content": "hi"}], system="be brief", max_tokens=300)

    assert out == "Hello"                     # the empty keepalive chunk is skipped
    sdk = fake_openai.instances[0]
    assert sdk.kwargs["base_url"] == LOCAL
    assert sdk.kwargs["api_key"]              # SDK needs non-empty; a placeholder is sent
    sent = sdk.create_kwargs
    assert sent["model"] == "qwen3:14b" and sent["max_tokens"] == 300
    assert "max_completion_tokens" not in sent
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}


async def test_a_real_key_is_sent_when_given(fake_openai):
    c = AIClient(AIProvider.OPENAI_COMPATIBLE, "gsk-123", model="m", base_url="https://api.groq.com/openai/v1")
    await c.chat([{"role": "user", "content": "q"}])
    assert fake_openai.instances[0].kwargs["api_key"] == "gsk-123"


def test_the_ui_registry_lists_the_provider():
    from backend.api.config import _DEFAULTS
    entry = _DEFAULTS["model_registry"]["openai_compatible"]
    assert entry == {"default_model": "", "models": []}
    assert set(_DEFAULTS["model_registry"]) == {p.value for p in AIProvider}
