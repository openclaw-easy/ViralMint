# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Edge TTS — free Microsoft voices via edge-tts package. Zero cost, no API key."""
import logging
import re
from pathlib import Path

from backend.config import settings
from backend.core.exceptions import VoiceGenerationError

logger = logging.getLogger(__name__)

# Best natural-sounding Edge TTS voices (multilingual variants sound more human)
RECOMMENDED_VOICES = {
    # English — US (4 multilingual + 3 classic)
    "en-US-AndrewMultilingualNeural": "Andrew (Male, natural)",
    "en-US-AvaMultilingualNeural": "Ava (Female, natural)",
    "en-US-BrianMultilingualNeural": "Brian (Male, warm)",
    "en-US-EmmaMultilingualNeural": "Emma (Female, clear)",
    "en-US-AriaNeural": "Aria (Female, classic)",
    "en-US-GuyNeural": "Guy (Male, classic)",
    "en-US-JennyNeural": "Jenny (Female, friendly)",
    # English — AU / GB
    "en-AU-WilliamMultilingualNeural": "William (Male, Australian)",
    "en-GB-RyanNeural": "Ryan (Male, British)",
    "en-GB-SoniaNeural": "Sonia (Female, British)",
    # Multilingual-tier European voices
    "fr-FR-VivienneMultilingualNeural": "Vivienne (Female, French)",
    "fr-FR-RemyMultilingualNeural": "Remy (Male, French)",
    "de-DE-SeraphinaMultilingualNeural": "Seraphina (Female, German)",
    "de-DE-FlorianMultilingualNeural": "Florian (Male, German)",
    "it-IT-GiuseppeMultilingualNeural": "Giuseppe (Male, Italian)",
    "pt-BR-ThalitaMultilingualNeural": "Thalita (Female, Brazilian Portuguese)",
    # Multilingual-tier Korean
    "ko-KR-HyunsuMultilingualNeural": "Hyunsu (Male, Korean)",
    # Major non-English markets without a Multilingual variant
    "es-ES-XimenaNeural": "Ximena (Female, Spanish)",
    "es-MX-DaliaNeural": "Dalia (Female, Mexican Spanish)",
    "ja-JP-NanamiNeural": "Nanami (Female, Japanese)",
    "zh-CN-XiaoxiaoNeural": "Xiaoxiao (Female, Mandarin)",
}

# Default voice — AndrewMultilingual is significantly more natural than AriaNeural
DEFAULT_VOICE = "en-US-AndrewMultilingualNeural"


def _add_natural_pauses(text: str) -> str:
    """
    Add natural pauses to script text using punctuation.
    Edge TTS interprets ellipsis and dashes as pauses, so we convert
    pause-worthy spots into punctuation the engine will respect.
    """
    processed = text

    # Add ellipsis after "..." for dramatic pause (engine respects these)
    processed = re.sub(r'\.{3,}\s*', '... ', processed)

    # Add a dash pause before emphasis transitions
    for phrase in ["But ", "However ", "Now ", "Here's the thing"]:
        lower = phrase.lower()
        # Match both capitalized and lowercase
        processed = processed.replace(phrase, f"— {phrase}")
        processed = processed.replace(lower, f"— {lower}")

    # Clean up any double-dash artifacts
    processed = processed.replace("— — ", "— ")

    return processed


async def generate_voice(
    text: str,
    voice_id: str = None,
    output_path: Path = None,
    use_ssml: bool = True,
) -> Path:
    """Generate speech using Edge TTS (free, no API key needed)."""
    if voice_id is None:
        voice_id = DEFAULT_VOICE
    if output_path is None:
        output_path = settings.AUDIO_DIR / f"edge_{hash(text) & 0xFFFFFFFF:08x}.mp3"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import edge_tts

        # Add natural pauses via punctuation and use native rate/pitch
        processed_text = _add_natural_pauses(text) if use_ssml else text
        communicate = edge_tts.Communicate(
            processed_text,
            voice_id,
            rate="-5%",
            pitch="+2Hz",
        )
        await communicate.save(str(output_path))

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise VoiceGenerationError("Edge TTS returned empty audio")

        logger.info(f"Edge TTS generated: {output_path} ({output_path.stat().st_size / 1024:.0f}KB, voice={voice_id})")
        return output_path

    except VoiceGenerationError:
        raise
    except Exception as e:
        raise VoiceGenerationError(f"Edge TTS generation failed: {e}")


async def get_voice_list() -> list[dict]:
    """List available Edge TTS voices, with recommended ones first."""
    try:
        import edge_tts
        voices = await edge_tts.list_voices()
        result = []
        recommended_ids = set(RECOMMENDED_VOICES.keys())

        # Add recommended voices first
        for v in voices:
            if v["ShortName"] in recommended_ids:
                result.append({
                    "voice_id": v["ShortName"],
                    "name": f"⭐ {RECOMMENDED_VOICES[v['ShortName']]}",
                    "category": v.get("Gender", "Unknown"),
                    "locale": v.get("Locale", ""),
                })

        # Then add all others
        for v in voices:
            if v["ShortName"] not in recommended_ids:
                result.append({
                    "voice_id": v["ShortName"],
                    "name": v["FriendlyName"],
                    "category": v.get("Gender", "Unknown"),
                    "locale": v.get("Locale", ""),
                })

        return result
    except Exception as e:
        logger.error(f"Failed to list Edge TTS voices: {e}")
        return []
