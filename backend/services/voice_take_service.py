# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Smart Video Studio — voice takes.

A take is one scene line spoken in one voice. Takes are content-addressed and
shared across projects (DATA_DIR/studio/cache/voice/<key>.mp3 + <key>.json):

    key = sha256(json([TAKE_VERSION, provider, model, voice, instructions, text]))

so the same line in the same voice is synthesised once, ever — editing one
line re-voices one scene, and an OpenAI take (the user's own key) is never
paid for twice.

Single-flight: concurrent requests for one key share ONE synthesis, and the
cache is re-checked inside the lock. A take is written atomically and only
after its audio probes as non-empty, so a failed or empty synthesis never
becomes a cached take.

Whether a finished take still belongs on its scene is NOT decided here: the
caller lands it through studio_ops' `voice_result` with `expect`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from backend.config import settings
from backend.core.exceptions import VoiceGenerationError
from backend.services.audio_utils import probe_audio_duration, trim_silence_edges_sync
from backend.services.tts_service import TTSProvider, normalise_tts_provider

logger = logging.getLogger(__name__)

TAKE_VERSION = "take-v1"
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class VoiceSpec:
    """The voice a take is recorded in — everything that changes the audio."""
    provider: str
    voice: str
    model: str | None = None
    instructions: str | None = None

    @classmethod
    def from_settings(cls, project_settings: dict) -> "VoiceSpec":
        v = (project_settings or {}).get("voice") or {}
        provider = normalise_tts_provider(v.get("provider") or TTSProvider.EDGE_TTS.value)
        if provider is None:
            raise VoiceGenerationError("That voice isn't supported — pick another.")
        return cls(provider=provider, voice=v.get("voice") or "",
                   model=v.get("model") or None, instructions=v.get("instructions") or None)


@dataclass(frozen=True)
class Take:
    key: str
    path: Path
    duration: float
    cached: bool


Synth = Callable[[str, VoiceSpec, Path], Awaitable[object]]


def take_key(spec: VoiceSpec, text: str) -> str:
    # JSON, not a joined string: an instruction containing the separator must
    # not be able to collide with a different (instructions, text) pair.
    payload = json.dumps([TAKE_VERSION, spec.provider, spec.model or "", spec.voice,
                          spec.instructions or "", text], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_dir() -> Path:
    return settings.STUDIO_DIR / "cache" / "voice"


def _paths(key: str) -> tuple[Path, Path]:
    d = cache_dir()
    return d / f"{key}.mp3", d / f"{key}.json"


def is_valid_key(key: object) -> bool:
    return isinstance(key, str) and bool(_KEY_RE.match(key))


def is_cached(key: object) -> bool:
    """True when this exact take exists — the `take_known` studio_ops trusts."""
    if not is_valid_key(key):
        return False
    mp3, meta = _paths(key)
    return mp3.is_file() and meta.is_file()


def read_meta(key: object) -> dict | None:
    if not is_cached(key):
        return None
    try:
        return json.loads(_paths(key)[1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def take_path(key: object) -> Path | None:
    return _paths(key)[0] if is_cached(key) else None


def spoken_text(line: str) -> str:
    """The text a line is actually synthesised as (whitespace-normalised) —
    what the take key hashes."""
    return " ".join((line or "").split())


def key_for_line(spec: VoiceSpec, line: str) -> str:
    return take_key(spec, spoken_text(line))


async def _synthesise(text: str, spec: VoiceSpec, out: Path) -> None:
    from backend.services.tts_service import generate_tts, tts_api_key
    produced = await generate_tts(
        text, provider=TTSProvider(spec.provider), voice_id=spec.voice or None,
        api_key=tts_api_key(spec.provider), output_path=out,
    )
    if produced and Path(produced) != out and Path(produced).is_file():
        os.replace(produced, out)


# Edge and OpenAI voices are network I/O: three takes at a time turns a
# six-scene draft from ~40 s into ~15 s. Shared by the voice job and the render.
VOICE_CONCURRENCY = 3


# One lock per key (single-flight). An entry is dropped once nobody holds or
# waits on it, so the map doesn't grow with every line ever voiced. Dropping it
# is safe because the take is written to the cache BEFORE the lock is released
# and every holder re-checks the cache inside the lock.
_locks: dict[str, asyncio.Lock] = {}


def _release_lock_entry(locks: dict[str, asyncio.Lock], key: str, lock: asyncio.Lock) -> None:
    if locks.get(key) is lock and not lock.locked() and not getattr(lock, "_waiters", None):
        locks.pop(key, None)


def _cached_take(key: str) -> Take | None:
    meta = read_meta(key)
    duration = (meta or {}).get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return None
    return Take(key=key, path=_paths(key)[0], duration=float(duration), cached=True)


def recorded_take(key: object) -> Take | None:
    """An already-recorded take by its key (free), or None if it isn't on disk."""
    return _cached_take(key) if is_valid_key(key) else None


async def ensure_take(spec: VoiceSpec, line: str, *, synth: Synth | None = None) -> Take:
    """The take for `line` in `spec`: from the cache or recorded once."""
    text = spoken_text(line)
    if not text:
        raise VoiceGenerationError("This scene has no line to voice yet.")
    key = take_key(spec, text)
    hit = _cached_take(key)
    if hit:
        return hit
    lock = _locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            hit = _cached_take(key)          # another request may have just recorded it
            if hit:
                return hit
            return await _record(key, spec, text, synth or _synthesise)
    finally:
        _release_lock_entry(_locks, key, lock)


def _write_take(part: Path, mp3: Path, meta_path: Path, meta: dict) -> None:
    tmp_meta = meta_path.with_name(f"{meta_path.name}.part")
    tmp_meta.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os.replace(part, mp3)
    os.replace(tmp_meta, meta_path)


def _trim_edges(part: Path) -> None:
    """Drop the synthesiser's leading/trailing silence in place (soft: the
    untrimmed take stays on any failure). Runs BEFORE the duration probe and
    the word timings, so both describe the trimmed file."""
    trimmed = part.with_name(f"{part.stem}.trim.mp3")
    try:
        if trim_silence_edges_sync(part, trimmed):
            os.replace(trimmed, part)
    finally:
        trimmed.unlink(missing_ok=True)


async def _record(key: str, spec: VoiceSpec, text: str, synth: Synth) -> Take:
    mp3, meta_path = _paths(key)
    mp3.parent.mkdir(parents=True, exist_ok=True)
    part = mp3.with_name(f"{key}.part.mp3")
    try:
        await synth(text, spec, part)
        if part.is_file():
            await asyncio.to_thread(_trim_edges, part)
        duration = await asyncio.to_thread(probe_audio_duration, part) if part.is_file() else 0.0
        if not duration or duration <= 0:
            # An unplayable file must never become a "take".
            logger.warning("voice take %s came back empty (provider=%s)", key[:12], spec.provider)
            raise VoiceGenerationError("The voice came back empty — try again.")
        meta = {
            "key": key, "version": TAKE_VERSION, "provider": spec.provider, "voice": spec.voice,
            "model": spec.model, "instructions": spec.instructions, "chars": len(text), "text": text,
            "duration": round(float(duration), 3), "created_at": time.time(),
        }
        await asyncio.to_thread(_write_take, part, mp3, meta_path, meta)
        return Take(key=key, path=mp3, duration=meta["duration"], cached=False)
    finally:
        part.unlink(missing_ok=True)


# ── details for the editor and the render: peaks + word timings ────────────

PEAK_COUNT = 200


def _decode_peaks(path: Path, count: int = PEAK_COUNT) -> list[float]:
    """Peak amplitude (0–1) of `count` equal slices — the timeline waveform.
    ffmpeg decodes to mono 8 kHz 16-bit PCM (argv, never a shell)."""
    import array

    from backend.services.video_utils import run_ffmpeg
    proc = run_ffmpeg(["-i", str(path), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], timeout=60, binary_stdout=True)
    data = proc.stdout or b""
    if proc.returncode != 0 or len(data) < 2:
        return []
    samples = array.array("h")
    samples.frombytes(data[: len(data) - len(data) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    step = max(1, len(samples) // count)
    peaks = []
    for i in range(0, len(samples), step):
        chunk = samples[i:i + step]
        peaks.append(round(max(abs(min(chunk)), abs(max(chunk))) / 32768, 3))
    return peaks[:count]


# Bump when the words a take's sidecar caches would come out different — the
# next take_details re-times the take once (local Whisper). 2 = Latin scripts
# align to the script too ("Did you know" had been captioned "Did you pay?"
# from a cached mis-hearing).
WORDS_VERSION = 2


def _words_current(meta: dict) -> bool:
    return "words" in meta and meta.get("words_version") == WORDS_VERSION


async def _word_timings(path: Path, text: str) -> list[dict] | None:
    """Word timings for captions (Whisper timing over the known text). None
    when Whisper can't run."""
    try:
        from backend.services.caption_timing import flat_words, timed_segments
        return flat_words(await timed_segments(path, text))
    except Exception:  # noqa: BLE001 — timings are an enhancement; the take stays usable
        logger.warning("voice take word timings unavailable", exc_info=True)
        return None


_detail_locks: dict[str, asyncio.Lock] = {}


def _write_meta(meta_path: Path, meta: dict) -> None:
    tmp = meta_path.with_name(f"{meta_path.name}.part")
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, meta_path)


async def take_details(key: object) -> dict | None:
    """The take's metadata with `peaks` and `words`, computed once on first
    request and cached in its sidecar (one computation per take at a time)."""
    meta = read_meta(key)
    if meta is None:
        return None
    if "peaks" in meta and _words_current(meta):
        return meta
    lock = _detail_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            meta = read_meta(key)
            if meta is None:
                return None
            mp3, meta_path = _paths(key)
            if "peaks" not in meta:
                meta["peaks"] = await asyncio.to_thread(_decode_peaks, mp3)
            words = meta["words"] if _words_current(meta) else await _word_timings(mp3, meta.get("text") or "")
            # A Whisper failure is NOT persisted: the next request (or the render)
            # tries again instead of that take never getting word-level captions.
            # (Stale words stay in the sidecar until fresh ones replace them.)
            stored = {**meta, **({"words": words, "words_version": WORDS_VERSION} if words is not None else {})}
            await asyncio.to_thread(_write_meta, meta_path, stored)
            meta = {**stored, "words": words if words is not None else meta.get("words")}
    finally:
        _release_lock_entry(_detail_locks, key, lock)
    return meta
