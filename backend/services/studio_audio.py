# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Video Studio — "My audio" as a project.

The user's recording becomes an `input.mode="audio"` project whose scenes speak in that recording.
The doc rules live in `studio_doc`, the cuts in `own_audio.cut_lines`, the edit rules in `studio_ops`;
this module is the plumbing around them:

  create  — the checked file COPIED into the project (a Library source is never consumed) and an
            empty audio project.
  listen  — Whisper → cut_lines → one exact-length mp3 per scene, installed in the take cache under
            `VoiceSpec.own_audio(...)` keys.
  land    — the scenes written in ONE write, only while the project still has none.

Listening is local and free. "Is it listening?" is answered from the Job row (`studio_audio`,
pending/running), so a crashed or restarted app leaves no mark behind: the boot sweep fails the
job and the editor offers Try again.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

from backend.config import settings
from backend.core.exceptions import JobCancelledError, StudioConflictError, StudioMediaError
from backend.database import AsyncSessionLocal
from backend.models.job import Job
from backend.services import own_audio
from backend.services import studio_doc as sd
from backend.services.own_audio import AudioRefused

logger = logging.getLogger(__name__)

JOB_TYPE = "studio_audio"
QUALITY = "balanced"
WHISPER_LIMIT_S = 20 * 60               # decode time, never the model download (fetched before)
# What an upload may be named: a recording, or a video whose sound is the recording (ffmpeg drops
# the picture).
UPLOAD_SUFFIXES = frozenset({".mp3", ".m4a", ".aac", ".ogg", ".flac", ".wav", ".mp4", ".mov", ".webm"})
_MASTER_RATE = "44100"
_LANDING_ATTEMPTS = 5

TOO_SHORT = f"This recording is under {int(sd.AUDIO_MIN_S)} seconds. Choose a longer one."
TOO_LONG = f"This recording is over {int(sd.AUDIO_MAX_S // 60)} minutes. Trim it, or choose a shorter one."
MAX_BYTES = 200 * 1024 * 1024
TOO_LARGE = f"This file is over {MAX_BYTES // (1024 * 1024)} MB. Choose a smaller one."
NO_AUDIO = "This file has no sound we can read. Choose a voice recording, or a video with speech in it."
BAD_TYPE = "Choose an audio file (mp3, m4a, wav, aac, ogg, flac) or a video with speech (mp4, mov, webm)."
EMPTY = "That file is empty. Choose another one."
LISTEN_FAILED = "Listening didn't finish. Try again."
TOOK_TOO_LONG = "Listening to this recording took too long. Try again, or use a shorter one."
CANCELLED = "Listening was stopped. Try again when you're ready."
SOURCE_GONE = "The recording for this project isn't on this computer any more. Start again with the file."
NOT_WAITING = "This project already has its scenes."
BUSY = "We're already listening to this recording."
# A refusal is about the file itself: trying the same file again can't help.
FINAL_CODES = frozenset({"no_speech", "too_short", "too_long", "too_large", "no_audio", "source_gone"})


# ── the file ────────────────────────────────────────────────────────────────

def source_dir(project_id: str) -> Path:
    from backend.services import studio_media
    return studio_media.project_assets_dir(project_id).parent / "source"


def source_file(project_id: str) -> Path | None:
    """The project's own copy of the recording, or None when it is gone."""
    try:
        folder = source_dir(project_id)
    except StudioMediaError:
        return None
    if not folder.is_dir():
        return None
    found = sorted(p for p in folder.iterdir() if p.is_file() and p.stem == "audio" and not p.name.startswith("."))
    return found[0] if found else None


def _file_id(path: Path) -> str:
    """The same bytes are the same recording (so its take keys repeat)."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return f"au_{digest.hexdigest()[:16]}"


async def check_file(path: Path) -> float:
    """The length of a recording we can take, or AudioRefused (`too_large` / `no_audio` /
    `too_short` / `too_long`): before anything is made."""
    from backend.services.ffmpeg_service import has_audio_stream
    from backend.services.video_utils import probe_duration
    size = await asyncio.to_thread(lambda: path.stat().st_size if path.is_file() else 0)
    if size > MAX_BYTES:
        raise AudioRefused("too_large", TOO_LARGE)
    if size <= 0 or not await has_audio_stream(path):
        raise AudioRefused("no_audio", NO_AUDIO)
    seconds = float(await asyncio.to_thread(probe_duration, path, 0.0))
    if seconds <= 0:                                  # a "stream" with nothing in it: not a short recording
        raise AudioRefused("no_audio", NO_AUDIO)
    if seconds < sd.AUDIO_MIN_S - sd.AUDIO_LENGTH_SLACK_S:
        raise AudioRefused("too_short", TOO_SHORT)
    if seconds > sd.AUDIO_MAX_S + sd.AUDIO_LENGTH_SLACK_S:
        raise AudioRefused("too_long", TOO_LONG)
    return seconds


def _copy_in(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f".{dest.name}.part")
    try:
        shutil.copyfile(src, part)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)


async def create(src: Path, name: str, *, aspect_ratio: str = "9:16", visual_style: str = "cinematic",
                 settings_overrides: dict | None = None, user_id: str = "local"):
    """An empty audio project holding its own COPY of `src` (which is read, never moved or changed).
    AudioRefused before anything exists; a copy that fails takes the project back."""
    from backend.services import studio_store as store
    src = Path(src)
    suffix = src.suffix.lower() if src.suffix.isascii() and 1 < len(src.suffix) <= 8 else ".bin"
    seconds = await check_file(src)                  # whatever its name: it must hold sound we can read
    file_id = await asyncio.to_thread(_file_id, src)
    title = sd.normalise_line(Path(name or "").stem)[:120] or "Your audio"
    row = await store.create_audio_project(
        audio={"audio_id": file_id, "title": title, "duration": round(seconds, 3)}, title=title,
        aspect_ratio=aspect_ratio if aspect_ratio in sd.ASPECTS else "9:16", visual_style=visual_style,
        settings_overrides=settings_overrides, user_id=user_id)
    try:
        await asyncio.to_thread(_copy_in, src, source_dir(row.id) / f"audio{suffix}")
    except BaseException:
        logger.warning("studio audio: the recording couldn't be copied into %s", row.id[:8], exc_info=True)
        try:
            await store.delete_project(row.id, user_id)
        except Exception:  # noqa: BLE001 — the copy's failure is the answer
            logger.warning("studio audio: removing %s after a failed copy failed", row.id[:8], exc_info=True)
        raise
    return row


# ── the job row is the listening state ──────────────────────────────────────

async def _jobs(project_id: str, *, live_only: bool) -> list:
    q = select(Job.id, Job.status, Job.error_message, Job.input_json, Job.output_json).where(Job.job_type == JOB_TYPE)
    if live_only:
        q = q.where(Job.status.in_(["pending", "running"]))
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(q.order_by(Job.created_at.desc()).limit(400))).all()
    out = []
    for row in rows:
        try:
            given = json.loads(row.input_json or "{}") or {}
        except ValueError:
            continue
        if given.get("project_id") == project_id:
            out.append(row)
    return out


async def live_job_id(project_id: str) -> str | None:
    """The pending/running listening job of this project, or None."""
    rows = await _jobs(project_id, live_only=True)
    return rows[0].id if rows else None


def _failure(row) -> dict:
    code = "failed"
    try:
        refused = (json.loads(row.output_json or "{}") or {}).get("refused") if row is not None else None
        if isinstance(refused, str) and refused:
            code = refused
    except ValueError:
        pass
    if row is not None and row.status == "cancelled":
        return {"code": "cancelled", "message": CANCELLED, "retry": True}
    message = (row.error_message if row is not None and row.status == "failed" else None) or LISTEN_FAILED
    if message.startswith("Server restarted"):                 # the boot sweep's words are for the Activity log
        message = LISTEN_FAILED
    return {"code": code, "message": message[:300], "retry": code not in FINAL_CODES}


async def envelope_fields(project_id: str, doc: dict) -> dict:
    """What an audio project's envelope adds to `derived`: `audio_listening`, `audio_job_id`,
    `audio_failed` {code, message, retry} | None."""
    if doc.get("scenes"):
        return {"audio_listening": False, "audio_job_id": None, "audio_failed": None}
    rows = await _jobs(project_id, live_only=False)
    newest = rows[0] if rows else None
    if newest is not None and newest.status in ("pending", "running"):
        return {"audio_listening": True, "audio_job_id": newest.id, "audio_failed": None}
    failed = _failure(newest)
    if failed["retry"] and source_file(project_id) is None:
        failed = {"code": "source_gone", "message": SOURCE_GONE, "retry": False}
    return {"audio_listening": False, "audio_job_id": None, "audio_failed": failed}


# ── listening ───────────────────────────────────────────────────────────────

def _install_take(key: str, cut: Path, *, audio_id: str, text: str, duration: float, words: list) -> None:
    """One cut into the take cache under its key: audio first, then the sidecar that makes it a take
    (voice_take_service's order). Its words are the ones Whisper heard, so nothing re-times it."""
    from backend.services import voice_take_service as vts
    folder = vts.cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    mp3, meta_path = folder / f"{key}.mp3", folder / f"{key}.json"
    part = folder / f"{key}.part.mp3"
    try:
        shutil.copyfile(cut, part)
        vts._write_take(part, mp3, meta_path, {
            "key": key, "version": vts.TAKE_VERSION, "provider": vts.OWN_AUDIO_PROVIDER, "voice": audio_id,
            "text": text, "chars": len(text), "duration": round(float(duration), 3), "created_at": time.time(),
            "words": [{"word": w[0], "start": float(w[1]), "end": float(w[2])} for w in words],
            "words_version": vts.WORDS_VERSION})
    finally:
        part.unlink(missing_ok=True)


def _cut_args(master: Path, start: float, end: float, out: Path) -> list[str]:
    from backend.services.audio_utils import MP3_EXACT_DURATION
    return ["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(master), "-vn", "-ac", "1",
            "-c:a", "libmp3lame", "-q:a", "4", *MP3_EXACT_DURATION, str(out)]


async def listen(src: Path, audio_id: str, *, on_step: Callable | None = None, on_fraction: Callable | None = None,
                 should_stop: Callable | None = None, user_id: str = "local") -> tuple[list[dict], str | None]:
    """What the recording says, cut and stored: ([{text, start, end, duration, key, words}], language).

    Whisper runs on its own pool, bounded by `WHISPER_LIMIT_S` of decode (a decode nobody waits for
    stops at its next segment); ffmpeg on the default pool, every call with its own timeout. The
    caller fetches a first-run model BEFORE calling this, so the limit never counts a download.
    `on_step(text)` is awaited; `on_fraction(0..1)` is called from the Whisper thread;
    `await should_stop()` true → JobCancelledError between steps."""
    from backend.services.video_utils import probe_duration, run_ffmpeg
    from backend.services.whisper_service import whisper_service

    async def step(text: str) -> None:
        if should_stop is not None and await should_stop():
            raise JobCancelledError("Listening was cancelled.")
        if on_step is not None:
            await on_step(text)

    await asyncio.to_thread(settings.TMP_DIR.mkdir, parents=True, exist_ok=True)
    tmp = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="studio_audio_", dir=str(settings.TMP_DIR)))
    try:
        await step("Opening your audio")
        seconds = await check_file(src)
        # One decoded copy: cuts from PCM land on the sample, whatever the upload's container was.
        master, speech = tmp / "master.wav", tmp / "speech.wav"
        done = await asyncio.to_thread(run_ffmpeg, ["-i", str(src), "-vn", "-ac", "1", "-ar", _MASTER_RATE,
                                                    "-c:a", "pcm_s16le", str(master)], timeout=300)
        if done.returncode != 0 or not master.is_file():
            raise AudioRefused("no_audio", NO_AUDIO)
        done = await asyncio.to_thread(run_ffmpeg, ["-i", str(master), "-ac", "1", "-ar", "16000",
                                                    "-c:a", "pcm_s16le", str(speech)], timeout=300)
        if done.returncode != 0 or not speech.is_file():
            raise AudioRefused("no_audio", NO_AUDIO)
        await step("Listening")
        try:
            transcript = await asyncio.wait_for(whisper_service.transcribe(
                speech, language=None, quality=QUALITY, on_progress=on_fraction, user_id=user_id,
                skip_if_no_speech=True), timeout=WHISPER_LIMIT_S)            # no speech: refused in ~1 s
        except asyncio.TimeoutError:
            raise RuntimeError(TOOK_TOO_LONG) from None
        lines = own_audio.cut_lines(transcript, seconds, audio_id)          # AudioRefused: no_speech / too_long
        await step("Cutting your sentences")
        for i, ln in enumerate(lines):
            if should_stop is not None and await should_stop():
                raise JobCancelledError("Listening was cancelled.")
            out = tmp / f"cut_{i:02d}.mp3"
            cut = await asyncio.to_thread(run_ffmpeg, _cut_args(master, ln["start"], ln["end"], out), timeout=120)
            real = await asyncio.to_thread(probe_duration, out, 0.0) if cut.returncode == 0 else 0.0
            if real <= 0:                                                   # probe it, never trust the exit code
                raise RuntimeError(f"cut {i} of the recording came back empty")
            ln["duration"] = round(min(real, ln["end"] - ln["start"] + 0.1), 3)
            await asyncio.to_thread(_install_take, ln["key"], out, audio_id=audio_id, text=ln["text"],
                                    duration=ln["duration"], words=ln["words"])
        language = transcript.get("language")
        return lines, language if isinstance(language, str) else None
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp, True)


async def land(project_id: str, lines: list[dict], language: str | None, user_id: str = "local"):
    """The scenes in ONE write, only while the project is still the empty audio project that asked
    for them (a second landing changes nothing → None). StudioNotFoundError when it was deleted."""
    from backend.services import studio_store as store
    for _attempt in range(_LANDING_ATTEMPTS):
        row = await store.get_project(project_id, user_id)
        doc = json.loads(row.doc_json)
        if not sd.is_audio_mode(doc) or doc["scenes"]:
            return None
        audio = doc["input"]["audio"]
        raw = {**doc, "scenes": [sd.own_audio_scene(ln, audio["audio_id"]) for ln in lines]}
        if language and language.strip():
            raw["input"] = {**doc["input"], "audio": {**audio, "language": language.strip()[:12]}}
        try:
            row, _ = await store.write_ops(project_id, [{"op": "replace_doc", "doc": raw}], if_revision=row.revision,
                                           internal=True, user_id=user_id)
            return row
        except StudioConflictError:
            continue
    raise StudioConflictError("This project changed in the meantime.")
