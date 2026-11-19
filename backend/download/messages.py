# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Words for people. One sentence per error class.

The download path raises our exceptions with these sentences as their
message, so a job's error, a chat bubble and a toast all say the same thing.
Diagnostic detail (yt-dlp's raw text) goes to the log, never to the person:
raw errors quote file paths, cookies and tokens.
"""
from __future__ import annotations

from backend.core.exceptions import ViralMintError
from backend.download.classify import ErrorClass as E
from backend.download.classify import classify, headline, is_raw_ytdlp_text, unavailable_kind
from backend.download.util import host_of

_UNAVAILABLE = {
    "upcoming": "This stream hasn't started yet. Try again after it has aired.",
    "private": "This video is private.",
    "members": "This video is members-only.",
    "age": "This video is age-restricted and needs a signed-in account.",
    "geo": "This video is blocked in your country.",
    "copyright": "This video was taken down for copyright.",
    "removed": "This video has been removed.",
    "": "Video unavailable. It may be removed, private or region-locked.",
}


def site_name(url: str | None) -> str:
    """The site as a person would say it: "YouTube", the bare host, or "the
    site" when the link isn't known (never a guess at which site)."""
    host = host_of(url)
    if host == "unknown":
        return "the site"
    if "youtube" in host or host.endswith("youtu.be"):
        return "YouTube"
    return host[4:] if host.startswith("www.") else host


def _cap(sentence: str) -> str:
    """Capitalise "the site …" at the start of a sentence; a host name stays as it is written."""
    return "The site" + sentence[8:] if sentence.startswith("the site") else sentence

def site_label(url: str | None) -> str:
    """The site's name to open a sentence with: "YouTube", "vimeo.com", or "The site"."""
    return _cap(site_name(url))



# The browsers CookieStore can read (cookies.BROWSERS), as a person would name them, for advice when none is found.
_READABLE_BROWSERS = "Chrome, Brave, Edge or Firefox (ViralMint can't read Safari's sign-in)"


def sign_in_message(url: str | None, cookie_hint: str | None = None, browser: str | None = None) -> str:
    """The sign-in wall, worded for the site that asked. `cookie_hint` is
    CookieStore.hint(): when we can't read the browser, "sign in in your
    browser" can't help, so the hint replaces that advice. `browser` is the
    browser CookieStore reads ("Firefox"); unknown, the advice names the ones it
    can read, never one browser for everyone (it said "open Chrome" to Firefox
    users until 2026-11-18)."""
    site = site_name(url)
    if cookie_hint:
        return _cap(f"{site} wants you signed in for this video. {cookie_hint}")
    where = browser or _READABLE_BROWSERS
    if site == "YouTube":
        return (
            "YouTube requires browser sign-in to verify you're not a bot. "
            f"To fix: open {where} → go to youtube.com → sign in → watch any video → then retry the download."
        )
    return _cap(
        f"{site} wants you signed in before it serves this video. "
        f"To fix: sign in to {site} in {browser or 'your browser'}, open the video once, then retry the download."
    )


def for_class(error_class: E, error: object = "", url: str | None = None,
              cookie_hint: str | None = None, browser: str | None = None) -> str:
    """The sentence for one failed download."""
    site = site_name(url)
    if error_class is E.COOKIE_SOURCE:
        return "Download failed: couldn't read your browser's cookies. " + (cookie_hint or "Please retry the download.")
    if error_class is E.DISK_FULL:
        return "The download stopped because the disk is full. Free some space and try again."
    if error_class in (E.SIGN_IN_REQUIRED,):
        return sign_in_message(url, cookie_hint, browser)
    if error_class in (E.AGE_GATE, E.UNAVAILABLE):
        kind = unavailable_kind(error, url) or ("age" if error_class is E.AGE_GATE else "")
        return _UNAVAILABLE[kind]
    if error_class in (E.RATE_LIMITED, E.RATE_LIMITED_SUBTITLES):
        return _cap(f"{site} is rate-limiting downloads from this network. Try again in 10–15 minutes.")
    if error_class is E.HTTP_403:
        return _cap(f"{site} refused the download (HTTP 403 Forbidden).")
    if error_class is E.HTTP_404:
        return _cap(f"{site} answered HTTP 404: the standard downloader couldn't find a video at this link.")
    if error_class is E.ANTI_BOT_CHALLENGE:
        return _cap(f"{site} put up a bot check the standard downloader can't pass.")
    if error_class is E.UNSUPPORTED_URL:
        return f"No standard extractor for {site}."
    if error_class is E.NO_FORMATS:
        return f"The standard downloader couldn't find a downloadable video on {site}."
    if error_class is E.TRANSIENT_NETWORK:
        return f"Couldn't reach {site}: a network problem. Check your connection and try again."
    if error_class is E.OUTPUT_INVALID:
        return "The download finished but the file isn't usable (too small, or not the stream that was asked for)."
    return f"Download failed: {headline(error) or 'unknown error'}"


def humanize_download_error(raw: object, url: str | None = None, max_len: int = 200) -> str:
    """A short sentence for any stored or caught download error.

    Our own exceptions already carry their sentence and pass through
    (shortened). Raw yt-dlp text (old job rows, other code paths) is
    classified and gets its class's sentence.
    """
    text = str(raw).strip()
    if not text:
        return "Download failed."
    if is_raw_ytdlp_text(text):
        return for_class(classify(text, url), text, url)[: max_len + 60]
    if isinstance(raw, BaseException) and not isinstance(raw, ViralMintError):
        # A KeyError's key, an OSError's path, a crash message: not for people.
        return "Download failed unexpectedly. Please try again."
    return headline(text, max_len)


def download_failure_message(raw_errors: list[str], total: int, abort_reason: str | None = None,
                             url: str | None = None) -> str:
    """One sentence for a batch in which every download failed. `url` is the link the batch stopped on (or the
    channel's): it names the site. Unknown, it is "The site", never a guess (it said "YouTube" for every site
    until 2026-11-18)."""
    site = site_name(url)
    if abort_reason == "rate-limited":
        return _cap(f"{site} is rate-limiting downloads from this network. Try again in 10–15 minutes.")
    if abort_reason == "bot-detection":
        return _cap(
            f"{site} blocked the download with a bot-detection check. "
            f"Open {'it' if site == 'the site' else site} in your browser, sign in, open any video, then retry."
        )
    if total == 1 and raw_errors:
        return humanize_download_error(raw_errors[0])
    if raw_errors:
        humanized = [humanize_download_error(r) for r in raw_errors]
        informative = next((h for h in humanized if not h.startswith("Download failed")), humanized[0])
        if informative.startswith("Download failed: "):
            informative = informative[len("Download failed: "):]
        return f"All {total} downloads failed: {informative}"
    return "All downloads failed. They may be unavailable or region-blocked."
