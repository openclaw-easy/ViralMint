# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Multi-provider TTS service.
Unified interface for: OpenAI TTS, Edge TTS.
Same pattern as ai_provider.py — factory resolves the right backend.
"""
import logging
from enum import Enum
from pathlib import Path

from backend.config import settings
from backend.core.exceptions import VoiceGenerationError

logger = logging.getLogger(__name__)


class TTSProvider(str, Enum):
    OPENAI_TTS = "openai_tts"
    EDGE_TTS = "edge_tts"


PROVIDER_INFO = {
    TTSProvider.OPENAI_TTS: {"label": "OpenAI TTS",  "cost_1k_chars": 0.015, "quality": "standard", "needs_key": True},
    TTSProvider.EDGE_TTS:   {"label": "Edge TTS",    "cost_1k_chars": 0.0,   "quality": "basic",    "needs_key": False},
}

DEFAULT_VOICES = {
    TTSProvider.OPENAI_TTS: "alloy",                    # alloy|echo|fable|onyx|nova|shimmer
    TTSProvider.EDGE_TTS:   "en-US-AndrewMultilingualNeural",  # Natural multilingual voice
}


async def generate_tts(
    text: str,
    provider: TTSProvider = TTSProvider.EDGE_TTS,
    voice_id: str = None,
    api_key: str = "",
    output_path: Path = None,
    # Long free-voice scripts (edge_tts_service): real movement for the job's bar,
    # and a cancel that stops it. Ignored by the other providers.
    on_progress=None,
    should_stop=None,
) -> Path:
    """Generate speech using any supported TTS provider. Returns audio file path."""
    if output_path is None:
        output_path = settings.AUDIO_DIR / f"voice_{hash(text) & 0xFFFFFFFF:08x}.mp3"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    voice = voice_id or DEFAULT_VOICES.get(provider, "")

    info = PROVIDER_INFO[provider]
    if info["needs_key"] and not api_key:
        raise VoiceGenerationError(f"{info['label']} API key not configured")

    logger.info(f"Generating TTS with {info['label']}, voice={voice}, text_len={len(text)}")

    if provider == TTSProvider.OPENAI_TTS:
        from backend.services.openai_tts_service import generate_voice
        return await generate_voice(text, voice_id=voice, api_key=api_key, output_path=output_path)

    elif provider == TTSProvider.EDGE_TTS:
        from backend.services.edge_tts_service import generate_voice
        return await generate_voice(text, voice_id=voice, output_path=output_path,
                                    on_progress=on_progress, should_stop=should_stop)

    raise VoiceGenerationError(f"Unknown TTS provider: {provider}")


async def list_voices(provider: TTSProvider, api_key: str = "") -> list[dict]:
    """List available voices for a provider."""
    if provider == TTSProvider.OPENAI_TTS:
        return [
            {"voice_id": "alloy",   "name": "Alloy",   "category": "neutral"},
            {"voice_id": "echo",    "name": "Echo",     "category": "male"},
            {"voice_id": "fable",   "name": "Fable",    "category": "british"},
            {"voice_id": "onyx",    "name": "Onyx",     "category": "deep male"},
            {"voice_id": "nova",    "name": "Nova",     "category": "female"},
            {"voice_id": "shimmer", "name": "Shimmer",  "category": "soft female"},
        ]

    elif provider == TTSProvider.EDGE_TTS:
        from backend.services.edge_tts_service import get_voice_list
        return await get_voice_list()

    return []


def estimate_tts_cost(text: str, provider: TTSProvider) -> float:
    """Estimate TTS cost for a given text."""
    chars = len(text)
    cost_per_1k = PROVIDER_INFO[provider]["cost_1k_chars"]
    return round((chars / 1000) * cost_per_1k, 4)


# ── One answer per question about a provider (the Smart Video Studio reads
#    these; the frontend gets them through GET /api/studio/enums) ───────────

def normalise_tts_provider(value) -> str | None:
    """The canonical provider id for `value`, or None when it isn't one."""
    if isinstance(value, TTSProvider):
        return value.value
    if not isinstance(value, str):
        return None
    try:
        return TTSProvider(value).value
    except ValueError:
        return None


def is_paid_per_take(provider) -> bool:
    """Each synthesis request spends the user's own API key (OpenAI TTS) — a
    first draft never auto-voices with such a provider; the user clicks."""
    return normalise_tts_provider(provider) == TTSProvider.OPENAI_TTS.value


def uses_heavy_pool(provider) -> bool:
    """Every provider here is network I/O — none needs the heavy job pool."""
    return False


def provider_traits() -> dict:
    return {p.value: {"paid_per_take": is_paid_per_take(p), "needs_key": PROVIDER_INFO[p]["needs_key"],
                      "heavy": uses_heavy_pool(p)} for p in TTSProvider}


def tts_api_key(provider) -> str:
    """The key for a provider that needs one ("" for Edge, or unset) — the
    same `OPENAI_API_KEY` every other OpenAI TTS caller here reads."""
    if normalise_tts_provider(provider) != TTSProvider.OPENAI_TTS.value:
        return ""
    return settings.OPENAI_API_KEY or ""


# CJK-aware narration length estimation. Every "words ÷ 2.5/sec" heuristic
# breaks on whitespace-free scripts: an 881-character Chinese narration
# counted as ~1 "word". Ideographic scripts narrate per CHARACTER; the rate
# below is calibrated from a real render (≈3.6 chars/sec incl. punctuation).
# Keep in sync with frontend/src/utils/spokenLength.js (same formula).
_CJK_CHAR_RANGES = (
    (0x3040, 0x30FF),  # kana
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),  # han
    (0xAC00, 0xD7AF),  # hangul
)
LATIN_WORDS_PER_SEC = 2.5
CJK_CHARS_PER_SEC = 3.5


def estimate_spoken_seconds(text: str) -> float:
    """Estimated narration seconds for mixed Latin/CJK text (0 for empty)."""
    text = (text or "").strip()
    if not text:
        return 0.0
    cjk = 0
    cur: list[str] = []
    for ch in text:
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _CJK_CHAR_RANGES):
            cjk += 1
            cur.append(" ")
        else:
            cur.append(ch)
    words = len("".join(cur).split())
    return round(words / LATIN_WORDS_PER_SEC + cjk / CJK_CHARS_PER_SEC, 1)
