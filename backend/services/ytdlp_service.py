# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""yt-dlp download wrapper with error handling and rate-limit resilience."""
import asyncio
import glob
import logging
import platform
import re
import shutil
import time
from pathlib import Path
from backend.config import settings
from backend.core.exceptions import DownloadError, VideoUnavailableError, RateLimitError
from backend.core.http_utils import get_user_agent

logger = logging.getLogger(__name__)


def _yt_dlp_http_headers() -> dict:
    """Browser-like headers for yt-dlp requests — rotated UA + Google referer."""
    return {
        "User-Agent": get_user_agent(),
        "Referer": "https://www.google.com/",
    }


class _YtDlpLogger:
    """Redirect yt-dlp output to Python logging instead of stdout/stderr.
    Prevents Broken pipe (EPIPE) when stdout is closed (e.g. piped through head)."""
    def debug(self, msg):
        if msg.startswith('[download]'):
            logger.debug(msg)
        else:
            logger.debug(msg)

    def info(self, msg):
        logger.info(msg)

    def warning(self, msg):
        logger.warning(msg)

    def error(self, msg):
        logger.error(msg)


# curl-cffi browser impersonation — replays Chrome's TLS handshake (JA3
# fingerprint) so sites with TLS-aware bot defenses (Cloudflare/Akamai)
# can't fingerprint the client by handshake alone. Wrapped in try/except so
# a missing or version-incompatible curl-cffi NEVER breaks downloads —
# they just fall back to urllib's default fingerprint. Pair versions per
# requirements.txt (curl-cffi 0.10–0.14 with current yt-dlp).
_IMPERSONATE_TARGET = None
try:
    import curl_cffi  # noqa: F401  (presence test; yt-dlp loads its own handler)
    from yt_dlp.networking.impersonate import ImpersonateTarget as _ImpersonateTarget
    _IMPERSONATE_TARGET = _ImpersonateTarget("chrome")
except Exception as _e:  # noqa: BLE001
    logger.info("curl-cffi impersonation unavailable: %s — downloads use urllib fingerprint", _e)


def _yt_dlp_base_opts() -> dict:
    """Base options for all yt-dlp calls — quiet mode + logger redirect.
    Adds Chrome TLS impersonation when curl-cffi is importable (no-op
    fallback otherwise)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtDlpLogger(),
    }
    if _IMPERSONATE_TARGET is not None:
        opts["impersonate"] = _IMPERSONATE_TARGET
    return opts


def _yt_dlp_js_opts() -> dict:
    """Enable Node.js runtime + EJS challenge solver for YouTube extraction.
    Without these, yt-dlp ≥ 2026.03 may fail to extract video formats."""
    return {
        "js_runtimes": {"node": {}},
        "remote_components": {"ejs:github": {}},
    }


def _get_ytdlp_age_days() -> int:
    """Return how many days old the installed yt-dlp version is, or -1 if unknown."""
    import importlib.metadata
    from datetime import datetime
    try:
        version = importlib.metadata.version("yt-dlp")
        date_part = ".".join(version.split(".")[:3])
        release_date = datetime.strptime(date_part, "%Y.%m.%d")
        return (datetime.now() - release_date).days
    except Exception:
        return -1


# Track consecutive failures to trigger auto-update
_consecutive_download_failures = 0
_FAILURE_THRESHOLD_FOR_UPDATE = 3  # Auto-update after 3 consecutive failures
_last_update_attempt = 0.0
_UPDATE_COOLDOWN = 3600  # Don't try updating more than once per hour


def check_ytdlp_version():
    """Log a warning if yt-dlp is more than 30 days old. Auto-update if stale."""
    import importlib.metadata
    from datetime import datetime

    try:
        version = importlib.metadata.version("yt-dlp")
        age_days = _get_ytdlp_age_days()

        if age_days > 30:
            logger.warning(
                "yt-dlp version %s is %d days old. Attempting auto-update...",
                version, age_days,
            )
            _try_update_ytdlp()
        else:
            logger.info("yt-dlp version %s (%d days old)", version, age_days)
    except Exception as e:
        logger.warning("Could not determine yt-dlp version: %s", e)


def _try_update_ytdlp() -> bool:
    """Attempt to update yt-dlp via pip. Returns True if updated successfully."""
    global _last_update_attempt
    import subprocess
    import sys

    # In a frozen PyInstaller bundle, sys.executable points at our own
    # bundled binary, not Python — running `sys.executable -m pip install -U`
    # would re-invoke desktop_app.main() and fork-bomb the launcher. There's
    # no pip in the bundle anyway. Skip the update; users should re-download
    # the installer when yt-dlp gets stale.
    if getattr(sys, "frozen", False):
        logger.info("Skipping yt-dlp auto-update in frozen build — re-install ViralMint to refresh")
        return False

    now = time.time()
    if now - _last_update_attempt < _UPDATE_COOLDOWN:
        logger.debug("Skipping yt-dlp update — last attempt was %ds ago", int(now - _last_update_attempt))
        return False
    _last_update_attempt = now

    try:
        logger.info("Auto-updating yt-dlp...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            # Reload the yt_dlp module to pick up the new version
            import importlib
            import yt_dlp
            importlib.reload(yt_dlp)
            new_age = _get_ytdlp_age_days()
            logger.info("yt-dlp updated successfully (now %d days old)", new_age)
            return True
        else:
            logger.warning("yt-dlp update failed: %s", result.stderr[:200])
            return False
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp update timed out")
        return False
    except Exception as e:
        logger.warning("yt-dlp update error: %s", e)
        return False


def _record_download_failure():
    """Track consecutive failures; trigger yt-dlp update after threshold."""
    global _consecutive_download_failures
    _consecutive_download_failures += 1
    if _consecutive_download_failures >= _FAILURE_THRESHOLD_FOR_UPDATE:
        age = _get_ytdlp_age_days()
        if age > 7:  # Only auto-update if at least a week old
            logger.warning(
                "%d consecutive download failures with yt-dlp %d days old — triggering auto-update",
                _consecutive_download_failures, age,
            )
            if _try_update_ytdlp():
                _consecutive_download_failures = 0  # Reset on successful update

# ── Rate-limit tracking ──────────────────────────────────────────────────────
# YouTube rate-limits at the IP level after heavy download volume.
# Track consecutive rate-limit hits and apply exponential backoff.
_rate_limit_state = {
    "consecutive_429s": 0,       # how many 429s in a row
    "last_429_time": 0.0,        # timestamp of last 429
    "cooldown_until": 0.0,       # don't attempt downloads until this time
}

RATE_LIMIT_BACKOFF_BASE = 60     # seconds — first backoff
RATE_LIMIT_BACKOFF_MAX = 300     # seconds — max backoff (5 min)
RATE_LIMIT_RESET_AFTER = 600    # seconds — reset counter after 10 min of no 429s


def _record_rate_limit():
    """Record a rate-limit hit and compute next cooldown."""
    now = time.time()
    # Reset counter if it's been a while since last 429
    if now - _rate_limit_state["last_429_time"] > RATE_LIMIT_RESET_AFTER:
        _rate_limit_state["consecutive_429s"] = 0

    _rate_limit_state["consecutive_429s"] += 1
    _rate_limit_state["last_429_time"] = now

    backoff = min(
        RATE_LIMIT_BACKOFF_BASE * (2 ** (_rate_limit_state["consecutive_429s"] - 1)),
        RATE_LIMIT_BACKOFF_MAX,
    )
    _rate_limit_state["cooldown_until"] = now + backoff
    logger.warning(
        "Rate limit hit #%d — backing off %ds (until %s)",
        _rate_limit_state["consecutive_429s"],
        backoff,
        time.strftime("%H:%M:%S", time.localtime(now + backoff)),
    )
    return backoff


def _record_success():
    """Record a successful download — reset consecutive 429 counter."""
    _rate_limit_state["consecutive_429s"] = 0


def _check_cooldown() -> float:
    """Return seconds to wait before next download, or 0 if ready."""
    remaining = _rate_limit_state["cooldown_until"] - time.time()
    return max(remaining, 0.0)

# ── Cookie caching ────────────────────────────────────────────────────────────
# Extract browser cookies ONCE into a Netscape cookies.txt file, then reuse it.
# This avoids repeated macOS Keychain prompts (one per yt-dlp invocation).

_BROWSER_COOKIE_SOURCE = None
_COOKIE_FILE: Path | None = None
_COOKIE_FILE_AGE: float = 0
_COOKIE_MAX_AGE = 3600  # Re-extract cookies every hour


def _detect_cookie_browser() -> str | None:
    """Detect which browser to use for cookies (checked once, cached)."""
    global _BROWSER_COOKIE_SOURCE
    if _BROWSER_COOKIE_SOURCE is not None:
        return _BROWSER_COOKIE_SOURCE or None

    system = platform.system()
    candidates = ["chrome", "brave", "edge", "firefox"]
    if system == "Darwin":
        browser_paths = {
            "chrome": "/Applications/Google Chrome.app",
            "brave": "/Applications/Brave Browser.app",
            "edge": "/Applications/Microsoft Edge.app",
            "firefox": "/Applications/Firefox.app",
        }
        for name in candidates:
            if Path(browser_paths.get(name, "")).exists():
                _BROWSER_COOKIE_SOURCE = name
                logger.info(f"Auto-detected browser for cookies: {name}")
                return name
    else:
        for name in candidates:
            binary = {"chrome": "google-chrome", "brave": "brave-browser", "edge": "microsoft-edge", "firefox": "firefox"}.get(name, name)
            if shutil.which(binary) or shutil.which(name):
                _BROWSER_COOKIE_SOURCE = name
                logger.info(f"Auto-detected browser for cookies: {name}")
                return name

    _BROWSER_COOKIE_SOURCE = ""
    logger.warning("No browser detected for cookie extraction — YouTube downloads may fail")
    return None


def _get_cookie_file() -> Path | None:
    """
    Return path to a cached cookies.txt file, extracting from browser if needed.
    Only triggers Keychain prompt once per hour max.
    """
    global _COOKIE_FILE, _COOKIE_FILE_AGE

    cookie_path = settings.TMP_DIR / "yt_cookies.txt"

    # Fast path: in-memory cache is fresh
    if _COOKIE_FILE and _COOKIE_FILE.exists() and (time.time() - _COOKIE_FILE_AGE) < _COOKIE_MAX_AGE:
        return _COOKIE_FILE

    # Check if file already exists on disk (survives module reloads / server restarts)
    if cookie_path.exists() and cookie_path.stat().st_size > 0:
        file_age = time.time() - cookie_path.stat().st_mtime
        if file_age < _COOKIE_MAX_AGE:
            _COOKIE_FILE = cookie_path
            _COOKIE_FILE_AGE = cookie_path.stat().st_mtime
            logger.info(f"Reusing cached cookie file ({cookie_path.stat().st_size} bytes, {int(file_age)}s old)")
            return _COOKIE_FILE

    # Need to extract fresh cookies from browser (triggers one Keychain prompt)
    browser = _detect_cookie_browser()
    if not browser:
        # No browser, but stale cookie file is better than nothing
        if cookie_path.exists() and cookie_path.stat().st_size > 0:
            _COOKIE_FILE = cookie_path
            _COOKIE_FILE_AGE = time.time()
            return _COOKIE_FILE
        return None

    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Extracting fresh cookies from {browser} (Keychain prompt expected)...")

    try:
        import yt_dlp
        opts = {
            **_yt_dlp_base_opts(),
            **_yt_dlp_js_opts(),
            "cookiesfrombrowser": (browser,),
            "cookiefile": str(cookie_path),
            "extract_flat": True,
            "skip_download": True,
            "http_headers": _yt_dlp_http_headers(),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info("https://www.youtube.com/watch?v=jNQXAC9IVRw", download=False)

        if cookie_path.exists() and cookie_path.stat().st_size > 0:
            _COOKIE_FILE = cookie_path
            _COOKIE_FILE_AGE = time.time()
            logger.info(f"Browser cookies cached to {cookie_path} ({cookie_path.stat().st_size} bytes)")
            return _COOKIE_FILE
    except Exception as e:
        logger.warning(f"Failed to extract browser cookies: {e}")
        # Use stale file if available
        if cookie_path.exists() and cookie_path.stat().st_size > 0:
            _COOKIE_FILE = cookie_path
            _COOKIE_FILE_AGE = time.time()
            return _COOKIE_FILE

    return None


def _apply_cookies(opts: dict):
    """Apply cached cookie file to yt-dlp options. Falls back to browser extraction if no cache."""
    cookie_file = _get_cookie_file()
    if cookie_file:
        opts["cookiefile"] = str(cookie_file)
    else:
        browser = _detect_cookie_browser()
        if browser:
            opts["cookiesfrombrowser"] = (browser,)


def _is_bot_detection_error(error_str: str) -> bool:
    """Check if the error is a YouTube bot detection / sign-in required error."""
    indicators = ["sign in to confirm", "not a bot", "cookies-from-browser", "login required"]
    return any(ind in error_str.lower() for ind in indicators)


def _is_transient_error(error_str: str) -> bool:
    """Check if the error is transient (worth retrying after a delay)."""
    indicators = [
        "broken pipe", "connection reset", "errno 32", "errno 104",
        "incomplete read", "connection aborted", "connection refused",
        "timed out", "timeout", "temporary failure", "name resolution",
        "ssl", "certificate", "eof occurred", "server error",
        "503", "502", "500", "urlopen error", "http error 5",
        "network is unreachable", "no route to host",
        "connection timed out", "read timed out",
    ]
    return any(ind in error_str.lower() for ind in indicators)


def _is_permanent_error(error_str: str) -> bool:
    """Check if the error is permanent (no point retrying)."""
    indicators = [
        "private", "unavailable", "removed", "deleted", "not found",
        "copyright", "terminated", "blocked", "geo", "age",
        "members only", "this video is not available",
    ]
    return any(ind in error_str.lower() for ind in indicators)


# Format fallback chain — progressively simpler format selections.
# Some videos have limited formats (e.g. live streams, premieres, shorts).
FORMAT_FALLBACK_CHAIN = [
    "bestvideo[height<=720]+bestaudio/best[height<=720]/best",  # Primary: 720p merged
    "bestvideo+bestaudio/best",                                  # Any quality merged
    "best",                                                       # Single best stream
]


def _find_actual_video_file(output_dir: Path, filename_stem: str) -> Path | None:
    """Find the actual downloaded video file — yt-dlp may change the extension after merging."""
    video_exts = [".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv"]
    for ext in video_exts:
        candidate = output_dir / f"{filename_stem}{ext}"
        if candidate.exists():
            return candidate
    # Fallback: glob for any file matching the stem
    matches = list(output_dir.glob(f"{filename_stem}.*"))
    # Exclude subtitle files
    matches = [m for m in matches if m.suffix.lower() not in (".srt", ".vtt", ".ass", ".json", ".txt")]
    if matches:
        # Return the largest file (most likely the video)
        return max(matches, key=lambda p: p.stat().st_size)
    return None


def _extract_audio_locally(video_path: Path, audio_dir: Path, filename_stem: str) -> Path | None:
    """Extract audio from a local video file using FFmpeg instead of re-downloading."""
    import subprocess
    audio_path = audio_dir / f"{filename_stem}_audio.mp3"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "libmp3lame",
             "-ab", "192k", "-ar", "44100", str(audio_path)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0 and audio_path.exists() and audio_path.stat().st_size > 0:
            return audio_path
        logger.warning(f"FFmpeg audio extraction returned {result.returncode}: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        logger.warning(f"FFmpeg audio extraction timed out for {video_path}")
    except FileNotFoundError:
        logger.warning("FFmpeg not found — cannot extract audio locally")
    return None


def _cleanup_subtitle_files(output_dir: Path, filename_stem: str):
    """Remove downloaded subtitle files after they've been parsed."""
    for ext in ("srt", "vtt"):
        for f in output_dir.glob(f"{filename_stem}.*.{ext}"):
            try:
                f.unlink()
            except Exception:
                pass


def _cleanup_partial_files(output_dir: Path, filename_stem: str):
    """Remove leftover .part files from failed/interrupted downloads."""
    cleaned = 0
    for f in output_dir.glob(f"{filename_stem}*.part"):
        try:
            f.unlink()
            cleaned += 1
        except Exception:
            pass
    if cleaned:
        logger.info(f"Cleaned up {cleaned} partial file(s) for {filename_stem}")


async def download_video(
    url: str,
    output_dir: Path = None,
    filename: str = None,
    extract_audio: bool = True,
) -> dict:
    """
    Download a video using yt-dlp.
    Returns: {"video_path": Path, "audio_path": Path|None, "duration": int, "file_size_mb": float,
              "subtitles": list|None, "chapters": list|None, "tags": list|None, "category": str|None}
    Raises: RateLimitError, VideoUnavailableError, DownloadError
    """
    if output_dir is None:
        output_dir = settings.VIDEOS_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    file_stem = filename or ""

    # Wait out any active rate-limit cooldown before starting
    cooldown = _check_cooldown()
    if cooldown > 0:
        logger.info(f"Rate-limit cooldown active — waiting {cooldown:.0f}s before downloading {url[:60]}")
        await asyncio.sleep(cooldown)

    def _download():
        import yt_dlp

        video_path = None
        audio_path = None

        # Base video download options (format set per-attempt via fallback chain)
        base_opts = {
            **_yt_dlp_base_opts(),
            **_yt_dlp_js_opts(),
            "outtmpl": str(output_dir / (filename or "%(id)s")) + ".%(ext)s",
            "extract_flat": False,
            "socket_timeout": 30,
            "retries": 5,
            "fragment_retries": 5,
            "file_access_retries": 3,
            "extractor_retries": 3,
            # Anti-detection: sleep between yt-dlp internal requests
            "sleep_interval": 3,
            "max_sleep_interval": 8,
            "sleep_interval_requests": 1,
            # Browser-like headers
            "http_headers": _yt_dlp_http_headers(),
            # Subtitle download
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "zh", "ja", "ko", "es", "fr", "de", "pt", "ru", "ar"],
            "subtitlesformat": "srt/vtt/best",
        }

        # Use cached browser cookies (avoids repeated Keychain prompts on macOS)
        _apply_cookies(base_opts)

        def _attempt_download(opts):
            """Single download attempt — raises on failure."""
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return Path(ydl.prepare_filename(info)), info

        def _classify_and_raise(error):
            """Classify a yt-dlp error and raise the appropriate exception type."""
            error_str = str(error).lower()
            _cleanup_partial_files(output_dir, file_stem)
            if _is_permanent_error(error_str):
                raise VideoUnavailableError(f"Video unavailable: {error}")
            if "429" in error_str or "rate" in error_str:
                _record_rate_limit()
                raise RateLimitError(
                    f"YouTube is rate-limiting downloads from this IP. "
                    f"Try again in a few minutes. (Error: {error})"
                )
            _record_download_failure()
            raise DownloadError(f"Download failed: {error}")

        # ── Main download with multi-layer retry ────────────────────────────
        last_error = None

        for fmt_idx, fmt in enumerate(FORMAT_FALLBACK_CHAIN):
            video_opts = {**base_opts, "format": fmt}

            # Up to 2 attempts per format (original + 1 retry for transient errors)
            for attempt in range(2):
                try:
                    video_path, info = _attempt_download(video_opts)
                    duration = info.get("duration", 0)
                    _record_success()
                    global _consecutive_download_failures
                    _consecutive_download_failures = 0
                    last_error = None
                    break  # Success — exit retry loop

                except yt_dlp.utils.DownloadError as e:
                    last_error = e
                    error_str = str(e).lower()

                    # ── Permanent errors: don't retry, raise immediately ────
                    if _is_permanent_error(error_str):
                        _cleanup_partial_files(output_dir, file_stem)
                        raise VideoUnavailableError(f"Video unavailable: {e}")

                    if "429" in error_str or "rate" in error_str:
                        # Subtitle-specific 429: retry without subtitles, don't give up
                        if ("subtitle" in error_str or "subtitles" in error_str):
                            logger.warning(f"Subtitle download rate-limited, retrying without subtitles")
                            _cleanup_partial_files(output_dir, file_stem)
                            video_opts["writesubtitles"] = False
                            video_opts["writeautomaticsub"] = False
                            continue  # Retry same format without subtitles

                        # Full 429 rate limit — no point retrying
                        _cleanup_partial_files(output_dir, file_stem)
                        _record_rate_limit()
                        raise RateLimitError(
                            f"YouTube is rate-limiting downloads from this IP. "
                            f"Try again in a few minutes. (Error: {e})"
                        )

                    # ── Bot detection: retry with fresh cookies ────
                    if _is_bot_detection_error(str(e)) and attempt == 0:
                        browser = _detect_cookie_browser()
                        if browser:
                            logger.warning(f"Bot detection triggered, retrying with {browser} cookies")
                            _cleanup_partial_files(output_dir, file_stem)
                            _apply_cookies(video_opts)
                            import time as _time
                            _time.sleep(3)
                            continue  # Retry with cookies
                        else:
                            _cleanup_partial_files(output_dir, file_stem)
                            raise DownloadError(
                                "YouTube requires sign-in to verify you're not a bot. "
                                "Install Chrome/Brave/Firefox and log into YouTube, then retry."
                            )

                    # ── Transient errors: retry once after delay ────
                    if _is_transient_error(error_str) and attempt == 0:
                        logger.warning(f"Transient error (attempt {attempt + 1}), retrying in 5s: {e}")
                        _cleanup_partial_files(output_dir, file_stem)
                        import time as _time
                        _time.sleep(5)
                        continue  # Retry

                    # ── Format-related errors: try next format ────
                    if any(s in error_str for s in ["no video formats", "requested format", "format is not available", "merge"]):
                        logger.warning(f"Format '{fmt}' failed, trying next fallback: {e}")
                        _cleanup_partial_files(output_dir, file_stem)
                        break  # Break retry loop, try next format

                    # ── Unknown error on first attempt: retry once ────
                    if attempt == 0:
                        logger.warning(f"Download error (attempt 1), retrying in 5s: {e}")
                        _cleanup_partial_files(output_dir, file_stem)
                        import time as _time
                        _time.sleep(5)
                        continue

                    # Second attempt also failed — try next format
                    logger.warning(f"Download failed on attempt 2 with format '{fmt}': {e}")
                    _cleanup_partial_files(output_dir, file_stem)
                    break  # Try next format

            if last_error is None:
                break  # Download succeeded

        # All formats exhausted — raise the last error
        if last_error is not None:
            _classify_and_raise(last_error)

        # Resolve actual video file (yt-dlp may change extension after merging)
        file_stem_resolved = file_stem or info.get("id", "")
        if video_path and not video_path.exists():
            actual = _find_actual_video_file(output_dir, file_stem_resolved)
            if actual:
                logger.info(f"Video file extension changed: expected {video_path.suffix}, found {actual.suffix}")
                video_path = actual

        # Extract subtitles from downloaded files
        subtitles = _collect_subtitles(output_dir, file_stem_resolved)

        # Clean up subtitle files after parsing (they're stored in DB now)
        _cleanup_subtitle_files(output_dir, file_stem_resolved)

        # Extract chapters from metadata
        chapters = _extract_chapters(info)

        # Extract tags and category
        tags = info.get("tags") or []
        category = info.get("categories", [None])[0] if info.get("categories") else info.get("category")

        # Audio extraction — extract locally from downloaded video (faster, no re-download)
        audio_dir = settings.AUDIO_DIR
        audio_dir.mkdir(parents=True, exist_ok=True)
        if extract_audio and video_path and video_path.exists():
            audio_path = _extract_audio_locally(video_path, audio_dir, file_stem_resolved)
            if not audio_path:
                # Fallback: re-download audio from YouTube
                logger.info("Local audio extraction failed, falling back to yt-dlp download")
                audio_opts = {
                    **_yt_dlp_base_opts(),
                    **_yt_dlp_js_opts(),
                    "format": "bestaudio/best",
                    "outtmpl": str(audio_dir / file_stem_resolved) + "_audio.%(ext)s",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }],
                    "http_headers": _yt_dlp_http_headers(),
                }
                _apply_cookies(audio_opts)
                try:
                    with yt_dlp.YoutubeDL(audio_opts) as ydl:
                        ydl.extract_info(url, download=True)
                        audio_path = audio_dir / f"{file_stem_resolved}_audio.mp3"
                except Exception as e:
                    logger.warning(f"Audio extraction failed (continuing without): {e}")

        file_size_mb = video_path.stat().st_size / (1024 * 1024) if video_path and video_path.exists() else 0

        return {
            "video_path": str(video_path) if video_path else None,
            "audio_path": str(audio_path) if audio_path and audio_path.exists() else None,
            "duration": duration,
            "file_size_mb": round(file_size_mb, 2),
            "title": info.get("title"),
            "views": info.get("view_count", 0),
            "likes": info.get("like_count", 0),
            "comments": info.get("comment_count", 0),
            "upload_date": info.get("upload_date"),  # "YYYYMMDD" string
            "uploader": info.get("uploader"),
            "uploader_url": info.get("uploader_url"),
            "thumbnail": info.get("thumbnail"),
            "description": info.get("description"),
            # New metadata fields
            "subtitles": subtitles,
            "chapters": chapters,
            "tags": tags,
            "category": category,
        }

    try:
        return await asyncio.wait_for(asyncio.to_thread(_download), timeout=1200)  # 20 min max per video
    except asyncio.TimeoutError:
        raise DownloadError("Download timed out after 20 minutes. The video may be too large or the connection too slow.")


def _collect_subtitles(output_dir: Path, video_id: str) -> list[dict] | None:
    """Find downloaded subtitle files and parse the best one into text segments."""
    # yt-dlp saves subtitles as: {video_id}.{lang}.{ext}
    patterns = [
        str(output_dir / f"{video_id}.*.srt"),
        str(output_dir / f"{video_id}.*.vtt"),
    ]
    sub_files = []
    for pattern in patterns:
        sub_files.extend(glob.glob(pattern))

    if not sub_files:
        return None

    # Prefer creator subtitles (no "auto" in filename) over auto-generated
    creator_subs = [f for f in sub_files if ".auto." not in Path(f).name]
    chosen = creator_subs[0] if creator_subs else sub_files[0]

    try:
        text = Path(chosen).read_text(encoding="utf-8", errors="replace")
        segments = _parse_subtitle_text(text, chosen)

        # Detect language from filename (e.g. "video_id.en.srt" → "en")
        stem = Path(chosen).stem  # "video_id.en"
        parts = stem.split(".")
        lang = parts[-1] if len(parts) > 1 else None

        # Determine source type
        is_auto = ".auto." in Path(chosen).name or any(".auto." in f for f in sub_files if f == chosen)
        source = "auto_subtitles" if is_auto else "creator_subtitles"

        return {
            "text": _segments_to_text(segments),
            "language": lang,
            "source": source,
            "segments": segments,
            "file": str(chosen),
        }
    except Exception as e:
        logger.warning(f"Failed to parse subtitle file {chosen}: {e}")
        return None


def _parse_subtitle_text(content: str, filepath: str) -> list[dict]:
    """Parse SRT or VTT subtitle content into segments with timestamps."""

    segments = []

    if filepath.endswith(".vtt"):
        # WebVTT format
        # Skip header lines
        lines = content.split("\n")
        i = 0
        while i < len(lines) and not re.match(r"\d{2}:\d{2}", lines[i]):
            i += 1

        while i < len(lines):
            line = lines[i].strip()
            # Match timestamp line: 00:00:01.000 --> 00:00:04.000
            ts_match = re.match(
                r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})", line
            )
            if ts_match:
                start = _ts_to_seconds(ts_match.group(1))
                end = _ts_to_seconds(ts_match.group(2))
                i += 1
                text_lines = []
                while i < len(lines) and lines[i].strip():
                    # Strip VTT tags like <c> </c> and positioning
                    cleaned = re.sub(r"<[^>]+>", "", lines[i].strip())
                    if cleaned:
                        text_lines.append(cleaned)
                    i += 1
                text = " ".join(text_lines).strip()
                if text:
                    segments.append({"start": start, "end": end, "text": text})
            else:
                i += 1
    else:
        # SRT format
        blocks = re.split(r"\n\s*\n", content.strip())
        for block in blocks:
            lines = block.strip().split("\n")
            if len(lines) < 2:
                continue
            # Find timestamp line
            for j, line in enumerate(lines):
                ts_match = re.match(
                    r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})", line
                )
                if ts_match:
                    start = _ts_to_seconds(ts_match.group(1))
                    end = _ts_to_seconds(ts_match.group(2))
                    text = " ".join(l.strip() for l in lines[j + 1:] if l.strip())
                    text = re.sub(r"<[^>]+>", "", text)  # strip HTML tags
                    if text:
                        segments.append({"start": start, "end": end, "text": text})
                    break

    return segments


def _ts_to_seconds(ts: str) -> float:
    """Convert timestamp like '00:01:23,456' or '00:01:23.456' to seconds."""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + s


def _segments_to_text(segments: list[dict]) -> str:
    """Join subtitle segments into a single text string, deduplicating consecutive identical lines."""
    seen = []
    for seg in segments:
        text = seg["text"].strip()
        if not seen or text != seen[-1]:
            seen.append(text)
    return " ".join(seen)


def _extract_chapters(info: dict) -> list[dict] | None:
    """Extract chapter markers from yt-dlp metadata."""
    chapters = info.get("chapters")
    if not chapters:
        return None

    return [
        {
            "start": ch.get("start_time", 0),
            "end": ch.get("end_time", 0),
            "title": ch.get("title", ""),
        }
        for ch in chapters
        if ch.get("title")
    ]


async def get_video_info(url: str, flat: bool = False) -> dict:
    """Get video/channel metadata without downloading.
    Use flat=True for channels/playlists to avoid resolving every video."""
    def _info():
        import yt_dlp
        opts = {**_yt_dlp_base_opts(), **_yt_dlp_js_opts(), "extract_flat": flat, "socket_timeout": 15,
                "http_headers": _yt_dlp_http_headers()}
        _apply_cookies(opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        return await asyncio.to_thread(_info)
    except Exception as e:
        logger.error(f"Failed to get video info: {e}")
        return {}


async def list_channel_videos(url: str, max_videos: int = 5) -> list[dict]:
    """
    Extract video list from a channel/playlist URL without downloading.
    Returns list of dicts with video metadata (url, title, duration, views, etc).
    """
    def _list():
        import yt_dlp
        opts = {
            **_yt_dlp_base_opts(),
            **_yt_dlp_js_opts(),
            "extract_flat": True,        # Don't download, just list
            "playlistend": max_videos,   # Limit number of videos
            "http_headers": _yt_dlp_http_headers(),
        }
        _apply_cookies(opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return []

        # Channel/playlist returns entries
        entries = info.get("entries", [])
        if not entries:
            # Single video URL — return as-is
            return [{
                "url": info.get("webpage_url") or info.get("url") or url,
                "video_id": info.get("id", ""),
                "title": info.get("title", ""),
                "duration": info.get("duration"),
                "view_count": info.get("view_count"),
                "upload_date": info.get("upload_date"),
            }]

        results = []
        for entry in entries:
            if not entry:
                continue
            video_url = entry.get("url") or entry.get("webpage_url", "")
            # Flat extraction often gives just video IDs — build full URL
            if video_url and not video_url.startswith("http"):
                video_url = f"https://www.youtube.com/watch?v={video_url}"
            results.append({
                "url": video_url,
                "video_id": entry.get("id", ""),
                "title": entry.get("title", ""),
                "duration": entry.get("duration"),
                "view_count": entry.get("view_count"),
                "upload_date": entry.get("upload_date"),
            })

        return results

    try:
        return await asyncio.to_thread(_list)
    except Exception as e:
        logger.error(f"Failed to list channel videos: {e}")
        return []


def is_channel_or_playlist_url(url: str) -> bool:
    """Check if a URL points to a channel or playlist rather than a single video."""
    indicators = [
        "/@", "/channel/", "/c/", "/user/",  # YouTube channels
        "/playlist?", "&list=",               # YouTube playlists
        "/videos",                            # Channel videos tab
    ]
    # A single video URL like youtube.com/watch?v=xxx without &list= is NOT a channel
    if "watch?v=" in url and "&list=" not in url:
        return False
    return any(ind in url for ind in indicators)
