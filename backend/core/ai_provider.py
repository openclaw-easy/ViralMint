# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
AI client factory — BYOK (Bring Your Own Key).

Supports Anthropic, OpenAI, OpenRouter, and any OpenAI-compatible server
(Ollama, LM Studio, vLLM, llama.cpp, DeepSeek, Groq, Together, a corporate
gateway, ...) at a base URL the user supplies. Keys come from `.env`
(system-wide default) or per-user `UserSettings` (overrides env). No
cloud-managed keys.

OpenRouter is a unified gateway that resells access to Claude / GPT /
Gemini / Llama / Mistral / etc. behind a single key. We talk to it via
the OpenAI-compatible chat-completions endpoint at
`https://openrouter.ai/api/v1`, with `model` set to a vendor-prefixed
slug like `anthropic/claude-opus-4.7`.
"""
from enum import Enum
from typing import AsyncIterator, Optional
from urllib.parse import urlparse
import logging

from backend.config import settings
from backend.core.crypto import decrypt_safe
from backend.core.exceptions import AIKeyMissingError

logger = logging.getLogger(__name__)


class AIProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    # Any server that speaks OpenAI's /chat/completions. It has no default
    # model and the key is optional (local servers usually take none).
    OPENAI_COMPATIBLE = "openai_compatible"


PROVIDER_DEFAULTS = {
    AIProvider.ANTHROPIC:  "claude-sonnet-4-6",
    AIProvider.OPENAI:     "gpt-5.4-mini",
    # OpenRouter BYOK users pay their own bill — default to a premium
    # model so the experience matches the gateway's value prop. Users
    # can pick a cheaper model in Settings → Model dropdown.
    AIProvider.OPENROUTER: "anthropic/claude-opus-4.7",
    # No sensible default — the model name depends entirely on the server.
    AIProvider.OPENAI_COMPATIBLE: "",
}

# The OpenAI SDK refuses an empty api_key, but local servers ignore it.
_NO_KEY_PLACEHOLDER = "not-needed"


def normalize_base_url(raw: Optional[str]) -> str:
    """Validate a user-supplied OpenAI-compatible base URL; return it without
    a trailing slash, or "" when empty.

    Raises ValueError for anything that isn't an http(s) URL with a host.
    The URL is used as given (e.g. `http://localhost:11434/v1`); we don't
    guess a `/v1` suffix because servers disagree on it.
    """
    url = (raw or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            "Base URL must start with http:// or https:// and include a host, "
            "e.g. http://localhost:11434/v1"
        )
    return url.rstrip("/")

# OpenRouter's optional analytics headers — show up on the public model
# leaderboard at openrouter.ai/rankings. Harmless to send; helps us
# track how much OSS-variant traffic flows through the gateway.
_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/openclaw-easy/ViralMint",
    "X-Title": "ViralMint (OSS)",
}


class AIClient:
    """Universal AI client — same interface regardless of underlying provider."""

    def __init__(
        self,
        provider: AIProvider,
        api_key: str,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.provider = provider
        self.api_key = api_key
        self.model = model or PROVIDER_DEFAULTS[provider]
        self.base_url = base_url or None
        if provider == AIProvider.OPENAI_COMPATIBLE and not (self.base_url and self.model):
            raise AIKeyMissingError(
                "The OpenAI-compatible provider needs a base URL and a model name. "
                "Set them in Settings → AI Provider, or CUSTOM_AI_BASE_URL and "
                "CUSTOM_AI_MODEL in your .env."
            )

    async def chat_stream(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """Yield text chunks as they stream in."""
        if self.provider == AIProvider.ANTHROPIC:
            async for chunk in self._anthropic_stream(messages, system, max_tokens):
                yield chunk
        elif self.provider == AIProvider.OPENAI:
            async for chunk in self._openai_stream(messages, system, max_tokens):
                yield chunk
        elif self.provider == AIProvider.OPENROUTER:
            async for chunk in self._openrouter_stream(messages, system, max_tokens):
                yield chunk
        elif self.provider == AIProvider.OPENAI_COMPATIBLE:
            async for chunk in self._compatible_stream(messages, system, max_tokens):
                yield chunk

    async def chat(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        max_tokens: int = 2048,
    ) -> str:
        """Non-streaming — returns full response string."""
        chunks = []
        async for chunk in self.chat_stream(messages, system, max_tokens):
            chunks.append(chunk)
        return "".join(chunks)

    # ── Provider implementations ──────────────────────────────────────────────

    async def _anthropic_stream(self, messages, system, max_tokens):
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=self.api_key)
        async with client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=system or "",
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def _openai_stream(self, messages, system, max_tokens):
        import openai
        client = openai.AsyncOpenAI(api_key=self.api_key, timeout=55.0)
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        # GPT-5+ and o-series reasoning models reject `max_tokens` and require
        # `max_completion_tokens` (OpenAI API change, late 2024). Older models
        # accept either, but we only ship gpt-5.x in the registry, so this
        # branch is the common path.
        m = self.model.lower()
        uses_completion_tokens = (
            m.startswith(("gpt-5", "o1", "o3", "o4"))
        )
        token_kwargs = (
            {"max_completion_tokens": max_tokens}
            if uses_completion_tokens
            else {"max_tokens": max_tokens}
        )

        stream = await client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            stream=True,
            **token_kwargs,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def _openrouter_stream(self, messages, system, max_tokens):
        """OpenRouter is OpenAI-compatible — same client, different base URL."""
        import openai
        client = openai.AsyncOpenAI(
            api_key=self.api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers=_OPENROUTER_HEADERS,
            timeout=55.0,
        )
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        # OpenRouter normalises max_tokens vs max_completion_tokens internally
        # for downstream models, so we always send max_tokens here regardless
        # of which underlying provider is being routed to.
        stream = await client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            stream=True,
            max_tokens=max_tokens,
        )
        async for chunk in stream:
            # Defensive: OpenRouter sometimes emits keepalive chunks with
            # an empty `choices` array before real tokens start flowing.
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


    async def _compatible_stream(self, messages, system, max_tokens):
        """A user-supplied OpenAI-compatible server. Sends the classic
        `max_tokens` — the parameter every compatible server accepts."""
        import openai
        client = openai.AsyncOpenAI(
            api_key=self.api_key or _NO_KEY_PLACEHOLDER,
            base_url=self.base_url,
            # Local models on CPU can be slow to first token.
            timeout=120.0,
        )
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        stream = await client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            stream=True,
            max_tokens=max_tokens,
        )
        async for chunk in stream:
            # Some servers emit keepalive / usage chunks with no choices.
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


# ── Factory ───────────────────────────────────────────────────────────────────

def _resolve_user_provider_and_key(user_settings) -> tuple[Optional[AIProvider], Optional[str]]:
    """Read provider preference and decrypted API key from UserSettings, if set."""
    if not user_settings:
        return None, None

    provider_str = (getattr(user_settings, "ai_provider", None) or "").lower().strip()
    encrypted_key = getattr(user_settings, "ai_api_key_encrypted", None)
    if not provider_str:
        return None, None

    try:
        provider = AIProvider(provider_str)
    except ValueError:
        return None, None

    api_key = (decrypt_safe(encrypted_key) or "") if encrypted_key else ""
    # An OpenAI-compatible server is configured by its base URL; the key is
    # optional. Every other provider needs a key to count as configured.
    if provider == AIProvider.OPENAI_COMPATIBLE:
        if (getattr(user_settings, "ai_base_url", None) or "").strip():
            return provider, api_key
        return None, None
    if not api_key:
        return None, None
    return provider, api_key


def get_ai_client(user_settings=None, **_kwargs) -> AIClient:
    """
    Return a configured AI client.

    Resolution order:
      1. UserSettings.ai_provider + UserSettings.ai_api_key_encrypted (BYOK from UI;
         for the OpenAI-compatible provider, UserSettings.ai_base_url + optional key)
      2. .env keys (ANTHROPIC_API_KEY → OPENAI_API_KEY → OPENROUTER_API_KEY →
         CUSTOM_AI_BASE_URL)
      3. Raise AIKeyMissingError with setup instructions.
    """
    user_model = getattr(user_settings, "ai_model", None) if user_settings else None

    # 1. User-configured BYOK
    user_provider, user_key = _resolve_user_provider_and_key(user_settings)
    if user_provider == AIProvider.OPENAI_COMPATIBLE:
        return AIClient(
            provider=user_provider,
            api_key=user_key or "",
            model=user_model,
            base_url=getattr(user_settings, "ai_base_url", None),
        )
    if user_provider and user_key:
        return AIClient(provider=user_provider, api_key=user_key, model=user_model)

    # 2. .env fallback. Order is by historical precedence, not preference —
    # whichever key is set in the env wins. Users who want a different
    # default should configure it in Settings.
    if settings.ANTHROPIC_API_KEY:
        return AIClient(
            provider=AIProvider.ANTHROPIC,
            api_key=settings.ANTHROPIC_API_KEY,
            model=user_model,
        )
    if settings.OPENAI_API_KEY:
        return AIClient(
            provider=AIProvider.OPENAI,
            api_key=settings.OPENAI_API_KEY,
            model=user_model,
        )
    if settings.OPENROUTER_API_KEY:
        return AIClient(
            provider=AIProvider.OPENROUTER,
            api_key=settings.OPENROUTER_API_KEY,
            model=user_model,
        )

    if settings.CUSTOM_AI_BASE_URL:
        return AIClient(
            provider=AIProvider.OPENAI_COMPATIBLE,
            api_key=settings.CUSTOM_AI_API_KEY,
            # The .env model wins over a Settings model picked for another provider.
            model=settings.CUSTOM_AI_MODEL or user_model,
            base_url=normalize_base_url(settings.CUSTOM_AI_BASE_URL),
        )

    raise AIKeyMissingError(
        "No AI provider configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
        "OPENROUTER_API_KEY, or CUSTOM_AI_BASE_URL + CUSTOM_AI_MODEL in your .env, "
        "or configure your provider and key in Settings."
    )
