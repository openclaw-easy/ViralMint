# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Edge TTS — free Microsoft voices via edge-tts package. Zero cost, no API key."""
import asyncio
import inspect
import logging
import os
import random
import re
import shutil
import tempfile
import time
import weakref
from pathlib import Path

from backend.config import settings
from backend.core.exceptions import JobCancelledError, VoiceGenerationError

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


_PAUSE_WORDS = re.compile(r"(?<![\w—])(But |However |Now |Here's the thing)", re.IGNORECASE)


def _add_natural_pauses(text: str) -> str:
    """
    Add natural pauses to script text using punctuation.
    Edge TTS interprets ellipsis and dashes as pauses, so we convert
    pause-worthy spots into punctuation the engine will respect.
    """
    processed = text

    # Add ellipsis after "..." for dramatic pause (engine respects these)
    processed = re.sub(r'\.{3,}\s*', '... ', processed)

    # Add a dash pause before emphasis transitions — whole words only: a plain
    # substring replace turned "know " into "k— now " and "debut " into
    # "de— but " (Edge then spoke "okay, now" / "done but").
    processed = _PAUSE_WORDS.sub(lambda m: f"— {m.group(1)}", processed)    # the phrase keeps its own case

    # Clean up any double-dash artifacts
    processed = processed.replace("— — ", "— ")

    return processed


# Microsoft's endpoint refuses a share of websocket handshakes with 403 (and
# the odd 429/5xx/reset, or an empty "No audio was received" stream).
# Measured on the hosted variant: refusals are independent per handshake at
# ~0.39 — so a 12-scene Studio draft lost a take or two almost every time.
# Capping concurrent connections at 2 and retrying a transient failure up to
# 8 times takes the per-line loss to ≈ 0.05%. Waiting only happens on a
# refusal (worst case ~17 s).
_MAX_CONCURRENT = 2
_MAX_ATTEMPTS = 8
_RETRY_DELAYS_S = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0)
# One semaphore PER event loop: an asyncio.Semaphore binds to the first loop
# that waits on it and raises in any other (each asyncio.run is a new loop).
_edge_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _edge_slots.get(loop)
    if sem is None:
        sem = _edge_slots[loop] = asyncio.Semaphore(_MAX_CONCURRENT)
    return sem


def _is_transient(err: Exception) -> bool:
    """Worth another attempt: a refused/throttled handshake, a dropped
    connection, a timeout, or edge-tts's own "no audio" on an empty stream.
    A bad voice id / bad SSML (ValueError etc.) fails the same way every time."""
    try:
        import aiohttp
        if isinstance(err, aiohttp.WSServerHandshakeError):
            return err.status in (403, 408, 429) or err.status >= 500
        if isinstance(err, (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError)):
            return True
    except ImportError:  # pragma: no cover — aiohttp ships with edge-tts
        pass
    if isinstance(err, (asyncio.TimeoutError, ConnectionError)):
        return True
    return type(err).__name__ in ("NoAudioReceived", "WebSocketError")


# A long script used to be one silent, all-or-nothing call: a flat progress
# bar for many minutes, then "No audio was received" and the WHOLE script
# again, up to 8 times. Measured on the real service, its speed swings
# (0.40x speech on repeated text, 0.81x on varied, 1.7x at other times), so a
# long script can be 10+ silent minutes with nothing wrong, and a request can
# answer NoAudioReceived in seconds. What the library does on its own:
#   * save() is one all-or-nothing call, so a bad chunk threw away every good
#     one, and it says nothing while it runs;
#   * its receive_timeout does NOT reach the websocket: a server that goes
#     quiet hangs `async for` until the far end closes
#     (tests/test_edge_tts_long_script.py, against a silent local server).
# So a long text goes out in our own sentence-sized parts: each has its own
# attempts and a bound on MOVEMENT (no audio for _STALL_S is a stall), the
# parts already made are kept, and the whole text has a ceiling.
_MP3_BITS_PER_S = 48_000   # edge-tts always asks for audio-24khz-48kbitrate-mono-mp3
_MAX_PART_CHARS = 1500
_MAX_PART_BYTES = 3600     # under the library's own 4096, so it never cuts a part mid-sentence
_STALL_S = 45.0            # measured on the real service: audio gaps up to ~14 s
_MAX_STALLS = 2            # a part that goes quiet twice won't be saved by six more waits
_TOTAL_BASE_S = 180.0
_TOTAL_PER_CHAR_S = 0.4    # the slowest measured (0.40x speech) is 0.14 s/char
_WATCH_S = 1.0
_SPOKEN_CHARS_PER_S = 17.0  # only for the bar inside a part; a part's end is what's exact
_SENTENCE_END = re.compile(r"(?<=[.!?\n])\s+|(?<=[。！？…])")


def _fits(text: str) -> bool:
    return len(text) <= _MAX_PART_CHARS and len(text.encode("utf-8")) <= _MAX_PART_BYTES


def _split_long(piece: str) -> list[str]:
    """A sentence too long for one part: at a comma or space, else anywhere."""
    out, cur = [], ""
    for word in re.findall(r"[^,;:，；：、\s]*(?:[,;:，；：、]\s*|\s+|$)", piece):
        if not word:
            continue
        if _fits(cur + word):
            cur += word
            continue
        if cur:
            out.append(cur)
        cur = word
        while not _fits(cur):
            cut = min(_MAX_PART_CHARS, _MAX_PART_BYTES // 4)
            out.append(cur[:cut])
            cur = cur[cut:]
    return out + ([cur] if cur else [])


def split_into_parts(text: str) -> list[str]:
    """The text in parts that end at a sentence wherever they can. Short text is one part, untouched."""
    if _fits(text):
        return [text]
    parts, cur = [], ""
    for sentence in _SENTENCE_END.split(text):
        if not sentence:
            continue
        joined = f"{cur} {sentence}" if cur and cur[-1].isascii() else cur + sentence
        if _fits(joined):
            cur = joined
            continue
        if cur:
            parts.append(cur)
        cur = ""
        if _fits(sentence):
            cur = sentence
        else:
            *whole, cur = _split_long(sentence)
            parts.extend(whole)
    return [p for p in parts + [cur] if p.strip()]


class _OverallTimeout(Exception):
    """The whole text ran past its ceiling."""


async def _stop_requested(should_stop) -> bool:
    if should_stop is None:
        return False
    answer = should_stop()
    return bool(await answer if inspect.isawaitable(answer) else answer)


async def _stream_part(text: str, voice_id: str, path: Path, *, clock: dict, should_stop, on_audio) -> None:
    """One attempt at one part: raises the library's errors, and TimeoutError when the audio stops
    moving. The stream runs in its own task so a silent socket can be watched and walked away from."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice_id, rate="-5%", pitch="+2Hz")
    last_audio = time.monotonic()
    written = 0

    async def pump():
        nonlocal last_audio, written
        with open(path, "wb") as f:
            async for message in communicate.stream():
                if message.get("type") == "audio":
                    f.write(message["data"])
                    written += len(message["data"])
                    last_audio = time.monotonic()
                    on_audio(written)

    task = asyncio.ensure_future(pump())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_WATCH_S)
            if done:
                task.result()
                return
            now = time.monotonic()
            if await _stop_requested(should_stop):
                raise JobCancelledError("Voice generation cancelled")
            if now >= clock["deadline"]:
                raise _OverallTimeout()
            if now - last_audio >= _STALL_S:
                raise asyncio.TimeoutError(f"no audio for {int(_STALL_S)} s")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _make_part(text: str, voice_id: str, path: Path, *, clock: dict, should_stop, on_audio) -> None:
    """A part, with its own attempts. Waiting only happens on a refusal or a stall."""
    stalls = 0
    async with _slots():
        # The ceiling's clock starts at the first slot, never while queued behind other lines.
        clock["deadline"] = clock["deadline"] or time.monotonic() + clock["ceiling"]
        for attempt in range(_MAX_ATTEMPTS):
            try:
                await _stream_part(text, voice_id, path, clock=clock, should_stop=should_stop, on_audio=on_audio)
                if path.exists() and path.stat().st_size > 0:
                    return
                raise VoiceGenerationError("Edge TTS returned empty audio")
            except (JobCancelledError, _OverallTimeout, VoiceGenerationError):
                raise
            except asyncio.TimeoutError:
                stalls += 1
                if stalls >= _MAX_STALLS or attempt == _MAX_ATTEMPTS - 1:
                    raise
                err = "went quiet"
            except Exception as e:
                if not _is_transient(e) or attempt == _MAX_ATTEMPTS - 1:
                    raise
                err = str(e)[:80]
            delay = _RETRY_DELAYS_S[min(attempt, len(_RETRY_DELAYS_S) - 1)]
            logger.info("Edge TTS attempt %d/%d failed (%s) — retrying in %.1fs",
                        attempt + 1, _MAX_ATTEMPTS, err, delay)
            await asyncio.sleep(delay * (0.75 + random.random() * 0.5))


async def generate_voice(
    text: str,
    voice_id: str = None,
    output_path: Path = None,
    use_ssml: bool = True,
    on_progress=None,
    should_stop=None,
) -> Path:
    """Generate speech using Edge TTS (free, no API key needed).

    `on_progress(fraction, part, parts)` reports real movement (audio received); `should_stop()`
    (sync or async) is watched while it runs and raises JobCancelledError."""
    if voice_id is None:
        voice_id = DEFAULT_VOICE
    if output_path is None:
        output_path = settings.AUDIO_DIR / f"edge_{hash(text) & 0xFFFFFFFF:08x}.mp3"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    workdir = None
    try:
        # Add natural pauses via punctuation and use native rate/pitch
        processed_text = _add_natural_pauses(text) if use_ssml else text
        parts = split_into_parts(processed_text)
        total_chars = max(1, sum(len(p) for p in parts))
        ceiling = _TOTAL_BASE_S + _TOTAL_PER_CHAR_S * total_chars
        clock = {"ceiling": ceiling, "deadline": None}
        workdir = Path(tempfile.mkdtemp(prefix=f".{output_path.stem}.", dir=output_path.parent))
        made: list[Path] = []
        done_chars = 0
        high_water = 0.0
        for i, part in enumerate(parts):
            def on_audio(audio_bytes, _i=i, _part=part, _done=done_chars):
                nonlocal high_water
                if on_progress is None:
                    return
                spoken_chars = audio_bytes * 8 / _MP3_BITS_PER_S * _SPOKEN_CHARS_PER_S
                # A retried part starts its bytes again; the bar doesn't walk back.
                high_water = max(high_water, (_done + min(spoken_chars, len(_part))) / total_chars)
                on_progress(high_water, _i + 1, len(parts))
            piece = workdir / f"{i:05d}.mp3"
            try:
                await _make_part(part, voice_id, piece, clock=clock, should_stop=should_stop, on_audio=on_audio)
            except _OverallTimeout:
                raise VoiceGenerationError(
                    f"Microsoft's free voice was too slow: {i} of {len(parts)} parts in "
                    f"{int(ceiling // 60)} minutes, so we stopped. Try again later, or a shorter script."
                ) from None
            except asyncio.TimeoutError:
                raise VoiceGenerationError(
                    f"Microsoft's free voice stopped sending audio on part {i + 1} of {len(parts)}. "
                    "Try again in a moment, or pick another voice."
                ) from None
            made.append(piece)
            done_chars += len(part)

        def join():
            joined = workdir / "joined.mp3"
            with open(joined, "wb") as out:
                for piece in made:
                    with open(piece, "rb") as f:
                        shutil.copyfileobj(f, out)
            os.replace(joined, output_path)
        await asyncio.to_thread(join)

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise VoiceGenerationError("Edge TTS returned empty audio")

        logger.info(f"Edge TTS generated: {output_path} ({output_path.stat().st_size / 1024:.0f}KB, voice={voice_id}, parts={len(parts)})")
        return output_path

    except (VoiceGenerationError, JobCancelledError):
        raise
    except Exception as e:
        raise VoiceGenerationError(f"Edge TTS generation failed: {e}")
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


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
