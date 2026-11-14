# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Custom exception hierarchy for ViralMint."""
import json
import logging as _logging


def safe_json_loads(raw, default=None, logger=None):
    """Parse JSON safely. Returns *default* on failure instead of raising."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        if logger:
            logger.warning("safe_json_loads failed: %s", exc)
        return default


class ViralMintError(Exception):
    """Base exception."""
    pass


# ── Scout errors ──────────────────────────────────────────────────────────────
class ScoutError(ViralMintError):
    pass

class PlatformUnavailableError(ScoutError):
    """Scout platform (TikTok, Douyin) is temporarily unavailable."""
    pass

class CookieExpiredError(ScoutError):
    """Session cookie is expired or invalid."""
    pass

class QuotaExceededError(ScoutError):
    """API quota (YouTube) exceeded."""
    pass


# ── Download errors ───────────────────────────────────────────────────────────
class DownloadError(ViralMintError):
    pass

class RateLimitError(DownloadError):
    """HTTP 429 — platform rate limiting."""
    pass

class VideoUnavailableError(DownloadError):
    """Video is private, deleted, or region-blocked."""
    pass

class BotDetectionError(DownloadError):
    """The site asked for a signed-in browser (a bot check or a login wall)."""
    pass

class ExtractorMissingError(DownloadError):
    """yt-dlp has no way into this page: no extractor (the generic one also
    failed), a bot-check page, or a 403 / 404 it couldn't get past."""
    pass

class NoCaptionsError(VideoUnavailableError):
    """The video exists but publishes no caption track that could be fetched
    (in any candidate language). Distinct from VideoUnavailableError so a
    caller can point at transcription instead of at the link."""
    pass

class DownloadTimeoutError(DownloadError):
    """The download exceeded its wall-clock cap and was abandoned.

    Like DownloadStalledError this is an ABANDONMENT: the worker thread is
    still running (Python cannot cancel it) and still owns the part-files in
    the output directory. Callers must treat it as terminal and must not
    start another attempt into the same output dir — see the note on
    `backend.download.progress.await_with_stall_guard`.
    """
    pass

class DownloadStalledError(DownloadError):
    """The transfer stopped moving bytes for longer than the stall budget.

    Distinct from a plain timeout: the wall clock had not run out, the
    download simply stopped making progress. yt-dlp's own retry budget
    (socket timeouts and retries with exponential backoff, times the
    format-fallback ladder) can burn well over an hour without delivering a
    single byte, and nothing in it measures progress.

    A DownloadError subclass so the batch runner's per-URL error collection
    and the UI error mapping route it without changes — but, like
    DownloadTimeoutError, it is TERMINAL inside the download engine (`backend.download.engine`): the
    abandoned worker thread is still running and still owns the part-files,
    so a retry into the same output directory would race it.
    """
    pass


# ── Job lifecycle ─────────────────────────────────────────────────────────────
class JobCancelledError(ViralMintError):
    """The user cancelled the job while it was running.

    Cancellation flips the Job row to "cancelled" (backend/api/jobs.py) but
    does NOT interrupt the coroutine — long pipelines must poll
    `job_helper.job_cancelled()` at phase boundaries and raise this to stop.
    Runners catch it separately from real failures: no `job_failed` WS event
    for something the user did on purpose, and the row keeps its "cancelled"
    status instead of being overwritten by a late "success"/"failed" write.
    """
    pass


# ── Generation errors ─────────────────────────────────────────────────────────
class GenerationError(ViralMintError):
    pass

class VoiceGenerationError(GenerationError):
    pass

class VideoGenerationError(GenerationError):
    pass


# ── Upload errors ─────────────────────────────────────────────────────────────
class UploadError(ViralMintError):
    pass

class UploadAuthError(UploadError):
    """OAuth token missing or expired."""
    pass


# ── AI errors ─────────────────────────────────────────────────────────────────
class AIProviderError(ViralMintError):
    pass

class AIKeyMissingError(AIProviderError):
    pass


# ── Motion Graphics ───────────────────────────────────────────────────────────

class WhisperModelUnavailableError(ViralMintError):
    """The local speech-recognition model isn't on disk and couldn't be fetched.

    Whisper WEIGHTS are not bundled in the installer (the installer-size
    budget) — faster-whisper downloads them from HuggingFace into
    ``DATA_DIR/whisper-cache`` the first time a quality tier is used. Before
    this class existed, a first-run user with no network (or a blocked HF host)
    got the raw huggingface_hub error wrapped in a generic 500 / "job failed",
    which reads as "transcription is broken" rather than "the one-time download
    didn't happen". The message is user-facing.

    ENVELOPE mirrors HyperFramesNotInstalledError so the on-demand-dependency
    gates share one shape.
    """

    ENVELOPE = {
        "ok": False,
        "error_code": "whisper_model_unavailable",
        "action_url": "/settings#advanced",
    }


class MotionGraphicsError(ViralMintError):
    """Base for the local motion-graphics (HyperFrames) render path."""


class HyperFramesNotInstalledError(MotionGraphicsError):
    """The on-demand HyperFrames plugin is missing.

    Raised by the install gate every motion endpoint pre-flights, so the caller
    can answer with an "install it from Settings" pointer instead of a stack
    trace. Distinct from MotionRenderError: nothing was attempted.

    ENVELOPE is what the API layer returns instead of a 500 — a structured
    answer the UI keys on to offer the install, rather than an error string
    someone has to read to understand. It is a normal 200 on purpose: "this
    feature needs a one-click install" is a state, not a failure.
    """

    ENVELOPE = {
        "ok": False,
        "error_code": "hyperframes_not_installed",
        "message": (
            "Motion Graphics isn't installed. "
            "Install it from Settings → Add-ons."
        ),
        "action_url": "/settings#motion-graphics",
    }


class MotionRenderError(MotionGraphicsError):
    """A render was attempted and did not produce a usable MP4."""


# ── Smart Video Studio ───────────────────────────────────────────────────────
class StudioError(ViralMintError):
    """Base for Studio project errors. The message is user-facing."""
    pass

class StudioDocError(StudioError):
    """A project document failed validation (hostile, corrupt or out of range)."""
    pass

class StudioOpError(StudioError):
    """An edit op was rejected — the message says why (400)."""
    pass

class StudioConflictError(StudioError):
    """The project moved on (stale revision, or a render already running) — 409."""
    pass

class StudioNotFoundError(StudioError):
    """No such Studio project (for this user) — 404."""
    pass

class StudioMediaError(StudioError):
    """A Studio media request can't be served (bad upload, blocked host, no keys) — 400."""
    pass
