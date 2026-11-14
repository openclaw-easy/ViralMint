# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The one place that reads download error text.

`classify(error, url)` turns a yt-dlp (or our own) failure into an
`ErrorClass`. Nothing else in the download path may match on error text:
`policy.decide` maps the class to a step, `messages` maps it to words.

The checks run in ONE fixed order, top to bottom of `classify`: local
causes first (they are never the site's answer), then the most specific
site answers, then the broad ones. The old code had three orders (the
attempt loop, the end of the ladder, the probe) that disagreed; see
tests/test_download_classify.py for every fixture where this order differs
from one of them.

yt-dlp has no typed errors for these cases, so this is substring matching
on its wording. Each wording that matters is a row in
tests/fixtures/download_error_corpus.json; a yt-dlp release that rewords
one breaks that test first.
"""
from __future__ import annotations

from enum import Enum
from urllib.parse import urlparse


class ErrorClass(str, Enum):
    COOKIE_SOURCE = "cookie_source"                    # we couldn't read the browser's cookies (local)
    DISK_FULL = "disk_full"                            # no space left on the disk (local)
    MERGE_RENAME = "merge_rename"                      # yt-dlp's merge or rename failed (local)
    POSTPROCESSING = "postprocessing"                  # a postprocessor failed after the file arrived
    OUTPUT_INVALID = "output_invalid"                  # too small / wrong stream (typed, never from text)
    AGE_GATE = "age_gate"                              # "sign in to confirm your age"
    SIGN_IN_REQUIRED = "sign_in_required"              # bot check or login wall
    RATE_LIMITED_SUBTITLES = "rate_limited_subtitles"  # 429 on a subtitle request only
    RATE_LIMITED = "rate_limited"                      # 429
    ANTI_BOT_CHALLENGE = "anti_bot_challenge"          # Cloudflare, captcha, HTTP 412
    UNSUPPORTED_URL = "unsupported_url"                # no extractor
    UNAVAILABLE = "unavailable"                        # private, removed, geo-blocked, members-only
    HTTP_403 = "http_403"
    HTTP_404 = "http_404"
    NO_FORMATS = "no_formats"
    TRANSIENT_NETWORK = "transient_network"
    UNKNOWN = "unknown"


_CURL_ERR_DOC_URL = "https://curl.se/libcurl/c/libcurl-errors.html"


def classifiable_text(err: str | BaseException, url: str | None = None) -> str:
    """Lowercased error text with the request URL, its host and curl's
    documentation URL removed.

    yt-dlp echoes the URL into its errors, and URL paths contain words like
    "private", "removed" and "403". Never raises.
    """
    s = str(err).replace(_CURL_ERR_DOC_URL, "")
    if url:
        try:
            s = s.replace(url, "")
            host = urlparse(url).netloc
            if host:
                s = s.replace(host, "")
        except Exception:
            pass
    return s.lower()


def _has(s: str, needles: tuple[str, ...]) -> bool:
    return any(n in s for n in needles)


_SIGN_IN = (
    "sign in to confirm", "not a bot", "cookies-from-browser", "login required",
    # Twitter/X's API refusing an anonymous client. 40 lines in one desktop's
    # logs, every one retried as "unknown" (2026-07 to 2026-11).
    "could not authenticate you",
)
_AGE = (
    "age-restricted", "age restricted", "age_gate", "age-gate", "age gate",
    "confirm your age", "inappropriate for some users",
)
# Multi-word phrases on purpose: bare "private" / "removed" / "blocked" match
# yt-dlp boilerplate. NOT here: "is not available" (matches "Requested format
# is not available") and "unsupported url" (no extractor is not "video gone").
_UNAVAILABLE = (
    "private video", "video is private", "this is a private",
    "video has been removed", "video was removed", "has been deleted",
    "video has been deleted", "video deleted",
    "this video is not available", "video is unavailable",
    "video unavailable", "no longer available",
    "copyright claim", "copyright strike", "removed for copyright",
    "account has been terminated", "channel has been terminated",
    "account terminated", "channel terminated",
    "geo-restricted", "geo restricted", "geo-blocked", "geo blocked",
    "not available in your country", "blocked in your country",
    "members only", "members-only", "paid promotion", "join this channel",
    "video not found", "no longer exists",
    # A scheduled stream or premiere that has not started: nothing to download yet.
    "live event will begin", "premieres in",
)
_CHALLENGE = (
    "cloudflare", "turnstile", "checking your browser", "verify you are human",
    "verify you're human", "ddos protection", "challenge-platform", "captcha",
    "perimeterx", "akamai bot", "http error 412", "412: precondition failed",
)
_NO_FORMATS = (
    "no video formats", "requested format", "format is not available",
    "no formats found", "no media found", "no playable video sources",
)
_TRANSIENT = (
    "broken pipe", "connection reset", "errno 32", "errno 104",
    "incomplete read", "connection aborted", "connection refused",
    "timed out", "timeout", "temporary failure", "name resolution",
    "ssl", "certificate", "eof occurred", "server error",
    "503", "502", "500", "urlopen error", "http error 5",
    "network is unreachable", "no route to host",
)


def _is_cookie_source(s: str) -> bool:
    if "cookies database" in s or "cookie database" in s:
        return True
    if "netscape format cookies" in s:
        return True   # OUR jar was rejected; yt-dlp quotes the offending cookie line, so this must never be shown raw
    if "unsupported browser specified for cookies" in s or "unsupported keyring" in s:
        return True
    if "could not find firefox container" in s or "could not find local state file" in s:
        return True
    return ("operation not permitted" in s or "permission denied" in s) and "cookies" in s


def _is_merge_rename(s: str) -> bool:
    # "[errno 2]" with its brackets: a bare "errno 2" also matches Errno 20-29,
    # and Errno 28 is a full disk, which no merge can fix.
    if "unable to rename" in s or "[errno 2]" in s:
        return True
    return "no such file" in s and (".f" in s or "storage" in s)


def classify(error: str | BaseException, url: str | None = None) -> ErrorClass:
    """The class of one download failure. Total: every input gets a class."""
    typed = getattr(error, "error_class", None)
    if isinstance(typed, ErrorClass):
        return typed

    s = classifiable_text(error, url)

    # Local causes: ours, not the site's.
    if _is_cookie_source(s):
        return ErrorClass.COOKIE_SOURCE
    if "no space left" in s or "[errno 28]" in s or "disk is full" in s or "disk quota exceeded" in s:
        return ErrorClass.DISK_FULL
    if _is_merge_rename(s):
        return ErrorClass.MERGE_RENAME
    if "postprocessing" in s:
        return ErrorClass.POSTPROCESSING

    # Sign-in walls. yt-dlp appends "Use --cookies-from-browser" to private
    # and members-only videos too: those stay UNAVAILABLE. Only the age gate
    # is worth one try with fresh cookies.
    if _has(s, _SIGN_IN):
        if _has(s, _AGE):
            return ErrorClass.AGE_GATE
        if _has(s, _UNAVAILABLE):
            return ErrorClass.UNAVAILABLE
        return ErrorClass.SIGN_IN_REQUIRED

    if "http error 429" in s or "too many requests" in s:
        return ErrorClass.RATE_LIMITED_SUBTITLES if "subtitle" in s else ErrorClass.RATE_LIMITED

    # Challenge pages BEFORE unavailable: they contain "blocked", "unavailable".
    if _has(s, _CHALLENGE):
        return ErrorClass.ANTI_BOT_CHALLENGE
    # No extractor BEFORE unavailable: it is not "video gone" (reading it as
    # "unavailable" once kept the generic extractor from ever being tried).
    if "unsupported url" in s:
        return ErrorClass.UNSUPPORTED_URL
    if _has(s, _UNAVAILABLE) or _has(s, _AGE):
        return ErrorClass.UNAVAILABLE

    if "http error 403" in s or "403: forbidden" in s:
        return ErrorClass.HTTP_403
    if "http error 404" in s or "404: not found" in s:
        return ErrorClass.HTTP_404
    if _has(s, _NO_FORMATS):
        return ErrorClass.NO_FORMATS
    if _has(s, _TRANSIENT):
        return ErrorClass.TRANSIENT_NETWORK
    return ErrorClass.UNKNOWN


# Why a video is unavailable, for the words shown to the person. Reads the
# same text as `classify`, here, so no other module has to.
_UNAVAILABLE_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("upcoming", ("live event will begin", "premieres in")),
    ("private", ("private video", "video is private", "this is a private")),
    ("members", ("members only", "members-only", "join this channel")),
    ("age", _AGE),
    ("geo", ("geo-restricted", "geo restricted", "geo-blocked", "geo blocked",
             "not available in your country", "blocked in your country",
             "available in your country")),
    ("copyright", ("copyright claim", "copyright strike", "removed for copyright", "copyright grounds")),
    ("removed", ("video has been removed", "video was removed", "has been deleted", "video deleted",
                 "removed by the uploader", "no longer available", "no longer exists",
                 "account has been terminated", "channel has been terminated",
                 "account terminated", "channel terminated")),
)


def unavailable_kind(error: str | BaseException, url: str | None = None) -> str:
    """"upcoming" | "private" | "members" | "age" | "geo" | "copyright" | "removed" | "" (not said)."""
    s = classifiable_text(error, url)
    return next((kind for kind, needles in _UNAVAILABLE_KINDS if _has(s, needles)), "")


def headline(error: str | BaseException, max_len: int = 200) -> str:
    """The first line of an error, without yt-dlp's "ERROR:" prefix, short
    enough to show. yt-dlp's later lines are paths, cookie data and tracebacks."""
    text = str(error).strip()
    if not text:
        return ""
    # A tab never belongs in a sentence: yt-dlp quotes cookie-file lines
    # (tab-separated, with the cookie's value) into some errors.
    line = text.split("\n", 1)[0].split("\t", 1)[0].strip()
    if line.lower().startswith("error:"):
        line = line[6:].strip()
    if len(line) > max_len and ":" in line:
        head, _, tail = line.partition(":")
        if len(tail) > len(head) and ("/" in tail or "=" in tail or "{" in tail):
            return (head.strip() + ".")[:max_len]
    return line if len(line) <= max_len else line[:max_len].rstrip() + "…"


def is_raw_ytdlp_text(error: str | BaseException) -> bool:
    """Is this yt-dlp's own output (possibly wrapped by old code) and not a
    sentence we wrote for a person?"""
    return "ERROR:" in str(error)
