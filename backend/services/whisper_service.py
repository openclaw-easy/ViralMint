# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Singleton faster-whisper transcription service."""
import asyncio
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

WHISPER_QUALITY_MAP = {
    "fast":     "base",
    "balanced": "small",
    "accurate": "medium",
    "best":     "large-v3",
}

# Approximate download sizes for first-time use
WHISPER_MODEL_SIZES = {
    "base":     "~150 MB",
    "small":    "~500 MB",
    "medium":   "~1.5 GB",
    "large-v3": "~3 GB",
}


# Models heavy enough that keeping them resident after a one-off job is a real
# memory cost (~1.5 GB / ~3 GB RSS). These get evicted after an idle period;
# base/small are cheap enough to keep hot for the process lifetime.
_HEAVY_MODELS = {"medium", "large-v3"}
_IDLE_EVICT_SECONDS = 600  # 10 min

# Min seconds between on_progress emissions during a streaming transcription.
# Module-level so tests can shrink it instead of monkeypatching time.monotonic
# (which asyncio's event loop also uses — patching it breaks loop timing).
_PROGRESS_EMIT_INTERVAL = 3.0

# Only transcriptions of audio AT LEAST this long take the serialization gate.
# The gate exists to stop two BATCH transcriptions (analyze + clip extraction
# on 30-minute-plus audio) from thrashing the machine. Short interactive
# transcriptions must NEVER wait on it — Smart Video and the caption tools
# align captions on ~1-minute TTS output mid-pipeline, and head-of-line
# blocking those behind a 30-minute batch job would stall them for tens of
# minutes. Short jobs run concurrently exactly as before the gate existed:
# they interleave inside ctranslate2 at generation-window granularity and
# finish fast. Unknown duration (ffprobe failed) counts as short — the safe
# default is the old concurrent behaviour.
_SERIALIZE_MIN_SECONDS = 120.0

# ── First-run model download ─────────────────────────────────────────────────
#
# The installer ships faster-whisper's CODE but no WEIGHTS: `WhisperModel(name)`
# fetches them from HuggingFace into DATA_DIR/whisper-cache the first time a
# quality tier is used. That is 150 MB - 3 GB of silence unless somebody says
# so, and exactly ONE of the many call sites did (the analyzer): every tool job,
# the clip extractor and both request handlers sat on a frozen step or a blank
# spinner for minutes on a fresh install. Worse, `transcribe()` ran the download
# on the EVENT LOOP — `load()` is synchronous and was called outside a thread —
# so the whole backend froze with it: no WebSocket, no progress, no other
# request served for the length of the download.
#
# `ensure_model()` below is the single door: it loads off the loop, announces a
# cold download exactly once per call, records the outcome for the Settings
# poller, and maps a failed fetch to a typed, user-readable error instead of a
# raw huggingface_hub traceback.
_download_state: dict[str, dict] = {}   # quality -> {"downloading": bool, "error": str|None}
_ensure_locks: dict[str, asyncio.Lock] = {}


def download_state(quality: str) -> dict:
    """{"downloading", "error"} for a quality tier — never raises."""
    st = _download_state.get(quality) or {}
    return {"downloading": bool(st.get("downloading")), "error": st.get("error")}


def job_download_notice(job_id: str, user_id: str = "local", pct: float = 5.0):
    """An `on_download` callback that reports the first-run fetch as JOB progress.

    Use this from anything that owns a Job row: the user is already watching a
    progress bar, so the honest thing is to name the wait there rather than
    leaving the bar parked on "Transcribing audio…" for four minutes. Writes the
    DB row AND the WS event — the UI listens on WS, the /api/jobs poll reads the
    row, and a first-run download is long enough that a user will reload the page
    part-way through it.
    """
    async def _notify(model_name: str, size: str):
        from backend.agents.job_helper import update_job_status
        from backend.core.ws_manager import ws_manager
        step = (
            f"Downloading the speech-recognition model ({model_name}, {size}) — "
            "one-time setup, this can take a few minutes…"
        )
        try:
            await update_job_status(job_id, "running", progress_pct=pct, current_step=step)
            await ws_manager.send_progress(job_id, pct, step, user_id)
        except Exception:  # a notice must never break the work it describes
            logger.debug("whisper download notice failed for job %s", job_id, exc_info=True)
    return _notify


def _toast_download_notice(user_id: str = "local"):
    """The DEFAULT `on_download` — a constraint warning (rule #14).

    Not every caller owns a Job: the voice-clip and staged-audio endpoints
    transcribe inside a request handler with nothing but a spinner on screen. A
    toast is the one surface that reaches the user from there. Deliberately the
    DEFAULT rather than an opt-in, so a call site added later degrades to "says
    something" instead of "says nothing" — the failure mode this whole change
    exists to remove.
    """
    async def _notify(model_name: str, size: str):
        from backend.core.ws_manager import ws_manager
        try:
            await ws_manager.send_constraint_warning(
                constraint="whisper_model_download",
                severity="warning",
                message=(
                    f"Preparing speech recognition — downloading the {model_name} "
                    f"model ({size}). One-time setup; this step may take a few minutes."
                ),
                user_id=user_id,
            )
        except Exception:
            logger.debug("whisper download toast failed", exc_info=True)
    return _notify


class WhisperService:
    _model = None
    _loaded_quality = None
    _lock = threading.Lock()
    _last_used = 0.0
    _evict_task = None
    # Serializes the transcription COMPUTE (not the model load) for long
    # audio. The model is created with ctranslate2's default num_workers=1,
    # so two concurrent transcribe() calls never truly ran in parallel
    # anyway — they queued invisibly inside the library while BOTH calling
    # threads burned CPU on audio decode + word-timestamp assembly,
    # saturating the machine. An explicit gate makes the queueing observable
    # and keeps the waiting job's decode work from starting until it can
    # actually run.
    _transcribe_gate = threading.Lock()

    @classmethod
    def _hub_dir(cls) -> Path:
        """Resolve the HuggingFace hub cache directory.

        MUST honour ``HF_HOME``: it's the standard way to relocate the model
        cache, and anyone self-hosting on a machine where ``$HOME`` is small or
        ephemeral will have set it. Hardcoding ``~/.cache/huggingface`` made
        ``is_model_cached()`` read a tree that faster-whisper never writes to in
        that setup, so freshly-downloaded models were reported as missing and a
        second full copy got downloaded.
        """
        hf_home = os.environ.get("HF_HOME")
        base = Path(hf_home) if hf_home else Path.home() / ".cache" / "huggingface"
        return base / "hub"

    @classmethod
    def is_model_cached(cls, quality: str = "balanced") -> bool:
        """Check if the Whisper model is already downloaded in the HF cache."""
        model_name = WHISPER_QUALITY_MAP.get(quality, "small")
        model_dir = cls._hub_dir() / f"models--Systran--faster-whisper-{model_name}"
        if not model_dir.exists():
            return False
        # Check for .incomplete files — model is still downloading
        blobs = model_dir / "blobs"
        if blobs.exists():
            for f in blobs.iterdir():
                if f.suffix == ".incomplete":
                    return False
        return True

    @classmethod
    def load(cls, quality: str = "balanced"):
        with cls._lock:
            if cls._model is None or cls._loaded_quality != quality:
                from faster_whisper import WhisperModel
                model_name = WHISPER_QUALITY_MAP.get(quality, "small")
                logger.info(f"Loading Whisper model: {model_name}")
                cls._model = WhisperModel(
                    model_name,
                    device="cpu",
                    compute_type="int8",
                )
                cls._loaded_quality = quality
                logger.info(f"Whisper model '{model_name}' loaded")
            cls._last_used = time.monotonic()
            return cls._model

    @classmethod
    async def ensure_model(cls, quality: str = "balanced", on_download=None,
                           user_id: str = "local"):
        """Load `quality` OFF the event loop, announcing a first-run download.

        Every path that needs Whisper goes through here (`transcribe()` calls it
        itself), so there is one place that:
          * keeps the download AND the model load off the loop — `load()` is
            synchronous and, uncached, blocks for the whole HuggingFace fetch;
          * tells the user it is happening, exactly once per call, BEFORE the
            wait rather than after it;
          * records `downloading` / `error` so the Settings poller can stop on a
            real failure instead of reporting "still in progress" forever;
          * raises WhisperModelUnavailableError — a typed, user-readable failure
            — rather than leaking a huggingface_hub traceback.

        `on_download(model_name, size)` may be sync or async; None selects the
        constraint-warning toast. It fires ONLY when the weights are absent, so
        the warm path (every run after the first) costs one `Path.exists()`.
        """
        model_name = WHISPER_QUALITY_MAP.get(quality, "small")
        if cls.is_model_cached(quality):
            return await asyncio.to_thread(cls.load, quality)

        # Notify BEFORE taking the lock: a second job arriving during the
        # download must get its own notice, not sit silently behind the first.
        size = WHISPER_MODEL_SIZES.get(model_name, "")
        notify = on_download if on_download is not None else _toast_download_notice(user_id)
        try:
            result = notify(model_name, size)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.debug("whisper on_download notice raised", exc_info=True)

        lock = _ensure_locks.setdefault(quality, asyncio.Lock())
        async with lock:
            # Re-check: we may have just waited out another caller's download.
            if cls.is_model_cached(quality):
                return await asyncio.to_thread(cls.load, quality)
            logger.info(
                "Whisper %s model not cached — downloading (%s) into %s",
                model_name, size or "unknown size", cls._hub_dir(),
            )
            _download_state[quality] = {"downloading": True, "error": None}
            try:
                model = await asyncio.to_thread(cls.load, quality)
            except Exception as e:
                msg = str(e)[:300] or type(e).__name__
                _download_state[quality] = {"downloading": False, "error": msg}
                logger.error("Whisper %s model download/load failed: %s", model_name, msg)
                from backend.core.exceptions import WhisperModelUnavailableError
                raise WhisperModelUnavailableError(
                    f"Couldn't download the speech-recognition model "
                    f"({model_name}, {size}). Check your internet connection and "
                    f"try again."
                ) from e
            finally:
                # Cancelling the job that happened to trigger the download
                # raises CancelledError, which is NOT an Exception and so skips
                # the handler above — without this the tier would report
                # "downloading" forever. A cancel is not a failure, so it clears
                # the flag without setting an error.
                if _download_state.get(quality, {}).get("downloading"):
                    _download_state[quality] = {"downloading": False, "error": None}
            logger.info("Whisper %s model ready", model_name)
            return model

    @classmethod
    def unload(cls) -> bool:
        """Drop the resident model. Returns True if something was freed."""
        with cls._lock:
            if cls._model is None:
                return False
            freed = cls._loaded_quality
            cls._model = None
            cls._loaded_quality = None
        import gc
        gc.collect()
        logger.info("Whisper model '%s' evicted (idle)", freed)
        return True

    @classmethod
    async def _evict_when_idle(cls):
        """Free a heavy model once it has gone unused for _IDLE_EVICT_SECONDS.

        Without this, a single "best"-quality analyze pins ~3 GB of RSS for the
        lifetime of the process — and the app sits in the tray holding it.
        """
        try:
            while True:
                with cls._lock:
                    quality = cls._loaded_quality
                    last = cls._last_used
                if quality is None:
                    return
                if WHISPER_QUALITY_MAP.get(quality) not in _HEAVY_MODELS:
                    return
                idle = time.monotonic() - last
                if idle >= _IDLE_EVICT_SECONDS:
                    cls.unload()
                    return
                await asyncio.sleep(min(60, _IDLE_EVICT_SECONDS - idle + 1))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let the sweeper kill anything
            logger.debug("Whisper idle-evict sweeper stopped: %s", e)

    @classmethod
    def _arm_evictor(cls):
        if WHISPER_QUALITY_MAP.get(cls._loaded_quality) not in _HEAVY_MODELS:
            return
        if cls._evict_task is not None and not cls._evict_task.done():
            return
        try:
            cls._evict_task = asyncio.get_running_loop().create_task(cls._evict_when_idle())
        except RuntimeError:
            pass  # no running loop (sync context) — skip, not worth failing over

    async def transcribe(
        self,
        audio_path: str | Path,
        language: str = None,
        quality: str = "balanced",
        timing_only: bool = False,
        on_progress=None,
        on_download=None,
        user_id: str = "local",
    ) -> dict:
        """
        Transcribe audio file. Returns:
        { "text": "...", "language": "en", "segments": [...] }

        `quality` is explicit on purpose. It used to be implicit — transcribe()
        called load() with no argument, which meant two things went wrong at
        once: a caller that had deliberately pre-loaded a bigger model (the
        analyzer does, for its `whisper_quality` setting) had it immediately
        thrown away and RE-loaded as "small", silently ignoring the user's
        choice and paying for the load twice; and conversely, whatever model an
        unrelated earlier job left resident could be reused by a later one.

        `timing_only=True` selects a greedy + VAD decode. Use it when the caller
        only needs word timings for caption alignment over clean, single-speaker
        audio (e.g. our own TTS output) rather than a publishable transcript —
        it is substantially faster on CPU for a negligible accuracy cost on that
        kind of input. Leave it False for anything the user will read.

        `on_progress(fraction)` (optional, sync, 0.0–0.99) fires from the
        worker thread as segments stream out of the decoder, throttled to one
        call every ~3 s. Callers bridging to async progress emitters should use
        `asyncio.run_coroutine_threadsafe` with a loop captured BEFORE the call
        (same pattern as yt-dlp's progress hook). Transcription used to consume
        the whole segment generator in one gulp, so a 40-minute audio showed a
        frozen progress bar for the entire 15–30 minute transcription — which
        is indistinguishable from a hung job.
        """
        # Pre-check: confirm the file actually has an audio stream before
        # invoking whisper. Without this, faster-whisper crashes with a
        # cryptic "tuple index out of range" when given a silent file
        # (e.g. video-only mp4 from a downloader that missed the audio
        # track).
        from backend.services.ffmpeg_service import has_audio_stream
        if not await has_audio_stream(Path(audio_path)):
            raise ValueError(f"No audio stream found in {audio_path}")

        # NOT `self.load(quality)`: that is a synchronous call which, on a cold
        # cache, downloads 150 MB - 3 GB inline and froze the whole event loop
        # with it. ensure_model() does it in a thread and announces the wait.
        model = await self.ensure_model(
            quality, on_download=on_download, user_id=user_id)

        # Duration serves two decisions: the progress fraction denominator AND
        # whether this transcription is long enough to take the batch gate.
        # ~50 ms of ffprobe next to a transcription that runs seconds to tens
        # of minutes.
        from backend.services.video_utils import probe_duration
        total_duration = await asyncio.to_thread(probe_duration, Path(audio_path))

        def _run():
            # Gate only BATCH-length audio (see _SERIALIZE_MIN_SECONDS —
            # short/interactive transcriptions must never queue here).
            gate = (type(self)._transcribe_gate
                    if total_duration >= _SERIALIZE_MIN_SECONDS else None)
            if gate is not None and not gate.acquire(blocking=False):
                logger.info(
                    "Whisper busy — %s (%.0fs) waits for the active batch "
                    "transcription to finish",
                    Path(audio_path).name, total_duration,
                )
                gate.acquire()
            try:
                segments_gen, info = model.transcribe(
                    str(audio_path),
                    beam_size=1 if timing_only else 5,
                    vad_filter=timing_only,
                    language=language,
                    word_timestamps=True,
                )
                # Stream the generator instead of list()-ing it so progress can
                # be reported as the decode advances through the audio.
                segments = []
                last_emit = 0.0
                best_fraction = 0.0
                for s in segments_gen:
                    segments.append(s)
                    if on_progress is not None and total_duration > 0:
                        now = time.monotonic()
                        if now - last_emit >= _PROGRESS_EMIT_INTERVAL:
                            # Monotonic guard: a segment with end=None maps to
                            # fraction 0.0 — emitting it would walk the bar
                            # backwards mid-transcription.
                            fraction = min(0.99, float(s.end or 0.0) / total_duration)
                            if fraction > best_fraction:
                                best_fraction = fraction
                                last_emit = now
                                try:
                                    on_progress(fraction)
                                except Exception:
                                    pass  # progress UI must never break transcription
            finally:
                if gate is not None:
                    gate.release()
            text = " ".join([s.text.strip() for s in segments])
            return {
                "text": text,
                "language": info.language,
                "language_probability": info.language_probability,
                "segments": [
                    {
                        "start": s.start,
                        "end": s.end,
                        "text": s.text.strip(),
                        "words": [
                            {"word": w.word, "start": float(w.start), "end": float(w.end), "probability": float(w.probability)}
                            for w in (s.words or [])
                        ],
                    }
                    for s in segments
                ],
            }

        try:
            return await asyncio.to_thread(_run)
        finally:
            type(self)._last_used = time.monotonic()
            type(self)._arm_evictor()


whisper_service = WhisperService()
