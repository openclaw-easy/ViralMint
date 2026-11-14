# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The health of the downloader itself, per host and per yt-dlp version.

Two memories that outlive one download:

  * The per-host 429 cooldown. A rate limit needs waiting, not retrying, and
    the NEXT download to that host honours it. A 429 on TikTok does not
    pause YouTube.
  * The failure streak that updates yt-dlp. Only failures that say "yt-dlp
    is broken for a site it claims to support" count (`record_failure`).
    Our own cookie errors, cancels, rate limits, sign-in walls, network
    errors and stalls never do: they used to, so a local cookie problem
    could trigger a pointless update.
"""
from __future__ import annotations

import logging
import time

from backend.download.classify import ErrorClass
from backend.download.util import host_of

logger = logging.getLogger(__name__)


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
        # Version-bounded spec: on an environment whose Python is too old for
        # current yt-dlp, an unbounded "-U yt-dlp" silently resolves to an
        # ANCIENT release (macOS system python3 = 3.9 → a 2025.10 build that
        # fails on modern YouTube). Bounded, pip errors loudly instead of
        # installing a silently-broken downloader.
        result = subprocess.run(
            # Same floor as requirements.txt (2026.10.26: the first release
            # that supports curl-cffi 0.17) — raise them together.
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp>=2026.10.26"],
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


def _count_failure():
    """Track consecutive failures; update yt-dlp after the threshold, but only
    when the installed release is at least a week old (a fresh release
    failing points somewhere else)."""
    global _consecutive_download_failures
    _consecutive_download_failures += 1
    if _consecutive_download_failures < _FAILURE_THRESHOLD_FOR_UPDATE:
        return
    age = _get_ytdlp_age_days()
    if age > 7:
        logger.warning(
            "%d consecutive download failures with yt-dlp %d days old — triggering auto-update",
            _consecutive_download_failures, age,
        )
        if _try_update_ytdlp():
            _consecutive_download_failures = 0

# ── Rate-limit tracking ──────────────────────────────────────────────────────
# Per-host cooldown. Previously global, but a 429 from TikTok shouldn't
# block YouTube downloads (different IPs, different rate-limit policies).
# Each host gets its own counter + cooldown clock.
RATE_LIMIT_BACKOFF_BASE = 60     # seconds — first backoff
RATE_LIMIT_BACKOFF_MAX = 300     # seconds — max backoff (5 min)
RATE_LIMIT_RESET_AFTER = 600     # seconds — reset counter after 10 min of no 429s
_RATE_LIMIT_STATE_TTL = 1800     # seconds — drop host buckets after 30 min idle


def _empty_host_state() -> dict:
    return {"consecutive_429s": 0, "last_429_time": 0.0, "cooldown_until": 0.0}


# host (e.g. "youtube.com") → state dict
_rate_limit_by_host: dict[str, dict] = {}


def _purge_stale_rate_limit_buckets():
    """Drop host buckets that haven't been touched in `_RATE_LIMIT_STATE_TTL`s.
    Cheap maintenance call — keeps the dict from growing unboundedly across
    long-running processes that download from many hosts."""
    now = time.time()
    stale = [
        host for host, st in _rate_limit_by_host.items()
        if st["last_429_time"] > 0 and now - st["last_429_time"] > _RATE_LIMIT_STATE_TTL
    ]
    for host in stale:
        del _rate_limit_by_host[host]


def record_rate_limit(url: str | None = None) -> float:
    """Record a rate-limit hit on `url`'s host and compute next cooldown."""
    host = host_of(url)
    now = time.time()
    state = _rate_limit_by_host.setdefault(host, _empty_host_state())

    # Reset counter if it's been a while since last 429 on this host
    if now - state["last_429_time"] > RATE_LIMIT_RESET_AFTER:
        state["consecutive_429s"] = 0

    state["consecutive_429s"] += 1
    state["last_429_time"] = now

    backoff = min(
        RATE_LIMIT_BACKOFF_BASE * (2 ** (state["consecutive_429s"] - 1)),
        RATE_LIMIT_BACKOFF_MAX,
    )
    state["cooldown_until"] = now + backoff
    logger.warning(
        "Rate limit hit on %s (#%d) — backing off %ds (until %s)",
        host,
        state["consecutive_429s"],
        backoff,
        time.strftime("%H:%M:%S", time.localtime(now + backoff)),
    )
    return backoff


def record_success(url: str | None = None) -> None:
    """A download worked: clear this host's 429 count and the failure streak."""
    global _consecutive_download_failures
    _consecutive_download_failures = 0
    host = host_of(url)
    state = _rate_limit_by_host.get(host)
    if state:
        state["consecutive_429s"] = 0


# The classes that can mean "yt-dlp no longer understands this site".
_STREAK_CLASSES = frozenset({ErrorClass.UNKNOWN, ErrorClass.NO_FORMATS})


def _has_site_extractor(url: str) -> bool:
    """Does yt-dlp have an extractor of its own for this URL (not the generic one)?"""
    try:
        from yt_dlp.extractor import gen_extractor_classes
        return any(ie.suitable(url) for ie in gen_extractor_classes() if ie.IE_NAME != "generic")
    except Exception:  # noqa: BLE001 — a lookup must never fail a download's error path
        return False


def record_failure(error_class: ErrorClass, url: str) -> bool:
    """A download ended in failure. Returns True when it counted toward the
    update streak: only an unexplained failure or missing formats
    on a site yt-dlp has its own extractor for. Call it for errors yt-dlp
    raised, never for our own bugs, cancels or abandonments."""
    if error_class not in _STREAK_CLASSES or not _has_site_extractor(url):
        return False
    _count_failure()
    return True


def cooldown_for(url: str | None = None) -> float:
    """Return seconds to wait before next download on this host, or 0 if ready.

    Bucketed by host so a 429 on TikTok doesn't pause YouTube downloads.
    """
    _purge_stale_rate_limit_buckets()
    host = host_of(url)
    state = _rate_limit_by_host.get(host)
    if not state:
        return 0.0
    return max(state["cooldown_until"] - time.time(), 0.0)
