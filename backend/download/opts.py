# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""yt-dlp options: every call gets the same four layers.

Base options (impersonation, retries, timeouts), JS runtime options, browser-like
headers for the URL host, and the cookie jar. A caller that forgets a layer
silently degrades extraction, so `build()` is the only way options are made.
Cookies come in as an argument (from `CookieStore`); this module never looks
for them.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from backend.core.exceptions import VideoUnavailableError
from backend.core.http_utils import get_user_agent

logger = logging.getLogger(__name__)


def http_headers(url: str | None = None) -> dict:
    """Browser-like headers for yt-dlp requests — rotated UA + per-host Referer.

    Referer is site-specific because CDNs validate it. `video.twimg.com` returns
    HTTP 403 when the Referer is `google.com` — the tweet page URL works instead.
    For unknown hosts, we fall back to the Google referer (helps with YouTube).
    """
    from urllib.parse import urlparse
    host = (urlparse(url).netloc or "").lower().split(":", 1)[0] if url else ""

    referer = "https://www.google.com/"
    # The domain itself or a subdomain of it: a bare `endswith("x.com")` also
    # matched box.com and dropbox.com and sent them an x.com Referer.
    if any(host == d or host.endswith("." + d) for d in ("twitter.com", "x.com", "twimg.com")):
        referer = "https://x.com/"

    return {
        "User-Agent": get_user_agent(),
        "Referer": referer,
    }


class _YtDlpLogger:
    """Redirect yt-dlp output to Python logging instead of stdout/stderr.
    Prevents Broken pipe (EPIPE) when stdout is closed (e.g. piped through head)."""
    def debug(self, msg):
        logger.debug(msg)

    def info(self, msg):
        logger.info(msg)

    def warning(self, msg):
        logger.warning(msg)

    def error(self, msg):
        logger.error(msg)


# curl-cffi browser impersonation — replays Chrome's TLS handshake (JA3
# fingerprint) so sites with Cloudflare/Akamai/PerimeterX TLS-aware bot
# defenses can't fingerprint us by handshake alone.
#
# Requires curl-cffi >= 0.15 (see the pin note in requirements.txt): 0.14's
# streaming path resets a libcurl handle from a done-callback while the
# calling thread is still reading it, which SIGABRTs the whole backend
# process — reproducible on TikTok URLs, which force impersonate=True. The
# older `0.10.x–0.14.x` range this comment used to state was written when
# 0.15 broke yt-dlp's request-handler API on yt-dlp ≤ 2026.04; verified fine
# on 2026.7.4.
#
# The import is wrapped in try/except so a missing/incompatible curl-cffi
# never breaks the success path — downloads fall back to urllib's default
# fingerprint (no impersonation, but no crash either).
_IMPERSONATE_TARGET = None
try:
    import curl_cffi  # noqa: F401  (presence test; yt-dlp imports its own handler)
    from yt_dlp.networking.impersonate import ImpersonateTarget as _ImpersonateTarget
    # Verify yt-dlp's curl_cffi handler actually loaded — yt-dlp emits a
    # warning if curl_cffi is too new/old. We probe by trying to instantiate
    # an ImpersonateTarget; the YoutubeDL constructor is what actually
    # rejects it later, but presence of the class at import time is enough
    # to know the wiring is in place.
    _IMPERSONATE_TARGET = _ImpersonateTarget("chrome")
except Exception as _e:  # noqa: BLE001  (bare-except OK — diagnostic-only)
    logger.info("curl-cffi impersonation unavailable: %s — downloads use urllib fingerprint", _e)


def base_opts() -> dict:
    """Base options for all yt-dlp calls — quiet mode + logger redirect.

    `retry_sleep_functions` aligns with yt-dlp's documented best practice
    for resilience under rate-limits (`--retry-sleep linear=1::2` etc.):
    exponential backoff per retry-pool. Replaces the previous static
    `sleep_interval=3-8` which wasted retry budget on rate-limited downloads.

    `format_sort` declares preference order for the picked format:
    prefer 1080p, then high fps, then h264+aac (max compatibility), then
    smaller file, then higher bitrate. Falls through gracefully when the
    preferred quality isn't available.

    `impersonate` enables curl-cffi browser fingerprinting when the lib
    is importable; otherwise omitted (no-op fallback to urllib).
    """
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtDlpLogger(),
        # Format preference (gracefully degrades when 1080p isn't there).
        # `lang` is FIRST so that on YouTube multi-language videos we pick the
        # ORIGINAL audio track — yt-dlp's `lang` sort key prefers formats whose
        # language matches the video page's default language (the original).
        # Without `lang`, plain `bestaudio` sorted by bitrate alone, which on
        # multi-language videos commonly picked a dub (Portuguese, German,
        # Spanish, etc.). Bug observed 2026-04-30 with a Tesla Semi YouTube
        # video that downloaded as Portuguese audio instead of English.
        # No-op for non-YouTube sources (formats lack `lang` → sort key has
        # no effect, the rest of the sort chain wins).
        "format_sort": ["lang", "res:1080", "fps", "codec:avc:m4a", "size", "br"],
        # Retry sleep — exponential per-pool. `n` is the retry attempt number.
        "retry_sleep_functions": {
            "http":        lambda n: min(60, 2 ** n),         # 1, 2, 4, 8, 16, 32, 60, 60...
            "fragment":    lambda n: min(120, 2 * (n + 1)),   # 2, 4, 6, 8, 10... (linear, capped)
            "file_access": lambda n: 0.5 * n,                 # quick disk retries
        },
        # Catch stale signed URLs that "succeed" but trickle bytes. yt-dlp
        # re-extracts the URL from scratch when speed drops below this.
        "throttledratelimit": 100_000,  # 100 KB/s
        # A link that names a video AND a playlist (YouTube `&list=`, a Music
        # radio mix, a Bilibili anthology) means the video. Each extractor
        # decides what that shape is — no URL string matching on our side.
        # Pure playlist / channel URLs carry no video and are unaffected.
        # Without it a pasted song + its endless mix downloaded every track
        # onto one output stem (job d9246b61, 2026-10-25).
        "noplaylist": True,
    }
    if _IMPERSONATE_TARGET is not None:
        opts["impersonate"] = _IMPERSONATE_TARGET
    return opts


# JS-runtime discovery cache. Positive results are process-lifetime (a
# runtime doesn't move once found); a *negative* result is NOT cached so a
# user who installs Node/deno mid-session starts benefiting on the very
# next download. The discovery cost is two shutil.which calls — noise next
# to a yt-dlp invocation.
_JS_RUNTIMES_RESOLVED: dict | None = None
_warned_no_js_runtime = False


def resolve_js_runtimes() -> dict:
    """Build yt-dlp's `js_runtimes` dict from what this machine actually has.

    Why discovery instead of a static pin: this used to be hardcoded
    `{"node": {}}`, which carried two silent failure modes on a machine
    without Node (the common case for packaged desktop users — the app bundles
    no JS runtime):

      1. Explicitly passing js_runtimes REPLACES yt-dlp's default, so the
         hardcoded pin also disabled the `deno` default — a user with deno but
         not node got NO runtime instead of one.
      2. With no runtime the EJS challenge solver can't run, n-signature
         challenges go unsolved, and YouTube formats go missing or 403 —
         indistinguishable in a bug report from a cookie problem.

    Nothing found → pass the default-shaped {'deno': {}} through and log ONE
    clear warning naming the consequence, instead of letting yt-dlp's buried
    per-download warning be the only signal.
    """
    global _JS_RUNTIMES_RESOLVED, _warned_no_js_runtime
    if _JS_RUNTIMES_RESOLVED is not None:
        return dict(_JS_RUNTIMES_RESOLVED)

    runtimes: dict = {}
    if shutil.which("deno"):
        runtimes["deno"] = {}
    if shutil.which("node"):
        runtimes["node"] = {}

    if runtimes:
        _JS_RUNTIMES_RESOLVED = dict(runtimes)
        return dict(runtimes)

    # Nothing found. Don't cache — the user may install a runtime later.
    if not _warned_no_js_runtime:
        _warned_no_js_runtime = True
        logger.warning(
            "No JavaScript runtime (deno/node) found — YouTube n-signature "
            "challenges can't be solved, so some formats will be missing or "
            "fail with HTTP 403. Installing Node.js or deno fixes this."
        )
    return {"deno": {}}


def js_opts() -> dict:
    """Enable a JS runtime + EJS challenge solver for YouTube extraction.
    Without these, yt-dlp ≥ 2026.03 may fail to extract video formats.
    Runtime selection is machine-aware — see `resolve_js_runtimes`.

    Per-extractor tweaks (`extractor_args`) — these are the high-leverage
    knobs that compound with curl-cffi impersonation in `base_opts`:

    - **youtube**: cycle through player clients. Order matches yt-dlp's
      own 2026.02.04 default (`tv` first). When one client gets blocked
      by bot detection, the next in the list takes over without us
      having to give up on the download.
      `webpage_client` is distinct from `player_client` (added 2026.03.17):
      controls which client identity is used for the initial webpage
      scrape vs. the streaming-URL request. We don't override
      `player_skip` — yt-dlp made `webpage` and `configs` the default
      skip list on 2026.03.03, so an explicit override would lock us
      into yesterday's behavior.

    - **tiktok**: documented args (verified against
      yt-dlp/yt_dlp/extractor/tiktok.py). `app_info=[""]` triggers the
      genuine-device-id flow which is the most stable. `app_version`
      and `manifest_app_version` are the values that match TikTok's
      current mobile app — bump these when the extractor ages out.

    - **twitter**: force syndication API. The default graphql path now
      403s guest-token requests on the m3u8 CDN for most tweets. The
      syndication endpoint (used by embedded tweets) returns the mp4
      URL directly and works for any publicly viewable tweet without
      auth. Note: yt-dlp only reads the first list element here — no
      fallback chain.
    """
    return {
        "js_runtimes": resolve_js_runtimes(),
        "remote_components": {"ejs:github": {}},
        "extractor_args": {
            "youtube": {
                # Order leads with the three player_clients that yt-dlp's
                # PO Token Guide currently lists as NOT REQUIRED. That guide
                # (https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide,
                # verified 2026-05-04) classifies clients as:
                #   No token : tv, web_embedded, android_vr
                #   Token GVS: mweb, ios, tv_simply, web_safari, web, web_music, web_creator
                # An earlier comment here claimed "mweb and ios historically
                # don't require PO Tokens" — that's now wrong. Leading with
                # mweb meant every YouTube download tried a token-gated
                # client first, which triggers a getpot-wpc Chrome launch
                # for the token. Reordered so we exhaust the no-token
                # clients before any token-gated fallback runs.
                #
                # Trade-off: token-free clients return slightly fewer
                # formats than `web`/`web_safari` for some videos. When all
                # three fail (rare), the fallbacks still kick in. Once
                # bgutil-ytdlp-pot-provider lands (separate plugin, no
                # Chrome dep), this list becomes much less load-bearing
                # because the token-gated clients won't need a browser
                # launch either.
                "player_client": [
                    "tv", "web_embedded", "android_vr",   # no-token (lead)
                    "tv_simply", "ios", "mweb",            # token-gated fallbacks
                    "web_safari", "web",                    # last resort
                ],
                # Identity used for the initial webpage scrape, distinct
                # from the streaming-URL request above.
                "webpage_client": ["web"],
                # Allow formats that need a PO Token even if missing —
                # degraded quality vs. nothing. When `bgutil-ytdlp-pot-provider`
                # ships in Phase 2 this becomes a transient fallback.
                "formats": ["missing_pot"],
            },
            # Channel/playlist extraction with cookies present triggers
            # an authcheck inside the youtube:tab extractor that fails with
            # "Playlists that require authentication may not extract
            # correctly without a successful webpage download." for many
            # public channels. yt-dlp's own remediation in that error
            # message is `youtubetab:skip=authcheck`. Channel analysis
            # in chat (`@leonard2834`-style URLs) was returning empty
            # without it. Public-only path — never affects login-walled
            # private playlists since we don't extract those anyway.
            "youtubetab": {"skip": ["authcheck"]},
            "tiktok": {
                "app_info": [""],                       # genuine-device-id flow
                "app_version": ["35.1.3"],              # current as of 2026-04
                "manifest_app_version": ["2023501030"],
            },
            "twitter": {"api": ["syndication"]},
            # Sites known to flake in production — bump retries from the
            # default 3 to 5. yt-dlp accepts these as strings in the
            # extractor_args dict (gets parsed to int internally).
            "instagram": {"extractor_retries": ["5"]},
            "reddit": {"extractor_retries": ["5"]},
        },
    }


# Single-video download paths pass this alongside `noplaylist`: a bare
# playlist URL (no video id, so `noplaylist` can't apply) then fetches ONE
# item instead of writing every entry onto the same output stem.
ONE_ITEM = {"playlist_items": "1"}

# A channel root is two levels (channel -> "Videos" tab -> video). Real results
# stop there; the bound is so a strange extractor can't send us round forever.
_MAX_PLAYLIST_DEPTH = 5


def single_video_info(info: dict | None) -> dict | None:
    """Unwrap a playlist-shaped yt-dlp result to its first video, however nested.

    The single-video paths read `title` / `id` / `duration` off the info
    dict; a playlist wrapper would hand them the playlist's instead, and the
    probe's duration and live checks would then have nothing to check.
    """
    for _ in range(_MAX_PLAYLIST_DEPTH):
        if not info or info.get("_type") != "playlist":
            return info
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise VideoUnavailableError("This link is a playlist with no playable video.")
        logger.warning(
            "Single-video download resolved to playlist %r — using its first item %r",
            info.get("id"), entries[0].get("id"),
        )
        info = entries[0]
    if not info or info.get("_type") != "playlist":
        return info
    raise VideoUnavailableError("This link is a playlist with no playable video.")


# Format fallback chain — progressively simpler format selections.
# Some videos have limited formats (e.g. live streams, premieres, shorts).
# Original-audio preference is handled by `format_sort: ["lang", ...]` in
# `base_opts()` rather than baked into the format selector — that
# way `lang` priority applies uniformly across every fallback step.
#
# Primary cap raised to 1080p on 2026-05-04 to match the `res:1080` sort
# preference declared in `base_opts`. The previous 720p cap forced
# a downgrade even when the format_sort preferred 1080p, because format
# SELECTORS (this list) gate eligibility before format SORT picks the best
# of the eligible set. Users were getting 720p output despite our sort
# preference saying otherwise. 1080p is the contemporary baseline for
# YouTube/TikTok creator content.
FORMAT_CHAIN = [
    "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",  # Primary: 1080p merged
    "bestvideo+bestaudio/best",                                    # Any quality merged
    "best",                                                         # Single best stream
]


def build(url: str | None, *, jar: Path | None, **overrides) -> dict:
    """Options for one yt-dlp call: the standard layers, then `overrides`,
    then the cookie jar (or no cookie key at all when `jar` is None)."""
    opts = {
        **base_opts(),
        **js_opts(),
        "http_headers": http_headers(url),
        **overrides,
    }
    drop_cookies(opts)
    if jar is not None:
        opts["cookiefile"] = str(jar)
    return opts


def drop_cookies(opts: dict) -> bool:
    """Remove every cookie source from `opts`. True if one was set."""
    dropped = False
    for key in ("cookiefile", "cookiesfrombrowser"):
        if opts.pop(key, None) is not None:
            dropped = True
    return dropped
