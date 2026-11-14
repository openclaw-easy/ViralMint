# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Wordings `classify()` must read correctly that the corpus does not hold.

tests/fixtures/download_error_corpus.json (test_download_classify.py) is the
main list. This file keeps what the two older classifier test files pinned
and the corpus lacks:

  1. **Merge / rename wordings.** yt-dlp has no typed error for a failed
     merge; the manual ffmpeg merge fires only when one of these texts is
     read as MERGE_RENAME. A yt-dlp release that rewords one breaks here
     first (was tests/test_ytdlp_errors.py::TestIsMergeOrRenameFailure).
  2. **Short wordings** the old per-predicate tests used, one per class.
  3. **URL pollution.** Adversarial URLs whose path holds every word the
     classifier looks for. The URL is fine; only its text overlaps
     (was tests/test_download_classifier_fixtures.py; the bug that started
     it: "webpage" matched "age" and a slow DNS lookup read as age-restricted).
  4. `classifiable_text` edge cases.
  5. `OutputInvalidError`'s type: typed, ours, read without its text.
"""
from __future__ import annotations

import pytest

from backend.core.exceptions import DownloadError as CoreDownloadError
from backend.download.classify import ErrorClass as E
from backend.download.classify import classifiable_text, classify
from backend.download.outputs import OutputInvalidError

YT = "https://www.youtube.com/watch?v=BBBBBBBB"


# ── 1. Merge / rename: the manual-merge recovery depends on these ────────────

@pytest.mark.parametrize("text", [
    # yt-dlp's standard message when the ffmpeg merge → rename step fails.
    "ERROR: unable to rename file: '/tmp/v.f398.mp4'",
    # Variant: 'unable to rename: SRC -> DST'.
    "ERROR: unable to rename: '/tmp/v.temp.mp4' -> '/tmp/v.mp4'",
    # Case-insensitive.
    "ERROR: Unable To Rename file: 'x'",
    # Some variants drop 'or directory'.
    "ERROR: no such file: '/storage/videos/abc.f251.webm'",
    # The merged output was never written: the error names the final path
    # under our storage dir, without the `.fNNN` marker.
    "ERROR: No such file or directory: '/Users/x/storage/videos/abc.mp4'",
    # OSError surfacing through yt-dlp's wrapper.
    "OSError: [Errno 2] No such file or directory",
    "[Errno 2] No such file or directory",
    # The production fixture (rename with source and target under storage).
    "ERROR: unable to rename file: '/Users/x/storage/videos/abc.f398.mp4' -> '/Users/x/storage/videos/abc.mp4'",
])
def test_merge_and_rename_wordings_reach_the_manual_merge(text):
    assert classify(text, YT) is E.MERGE_RENAME


@pytest.mark.parametrize("text,expect", [
    # 'no such file' WITHOUT a merge marker (`.f` or `storage`) is some
    # other filesystem error, not a merge failure.
    ("ERROR: no such file: /tmp/cookies.txt", E.UNKNOWN),
    ("", E.UNKNOWN),
    # A generic permission error must not pretend to be a merge failure.
    ("PermissionError: [Errno 13]", E.UNKNOWN),
])
def test_other_filesystem_errors_are_not_merge_failures(text, expect):
    assert classify(text, YT) is expect


def test_a_full_disk_is_not_a_merge_failure():
    text = "ERROR: unable to write data: [Errno 28] No space left on device"
    assert classify(text, YT) is not E.MERGE_RENAME


# ── 2. Short wordings, one line per class ────────────────────────────────────

@pytest.mark.parametrize("text,expect", [
    # Video gone: never a merge failure, never retried.
    ("ERROR: This video is private", E.UNAVAILABLE),
    ("ERROR: copyright claim", E.UNAVAILABLE),
    ("ERROR: [vimeo] 1: This video is not available from your location due to geo restriction", E.UNAVAILABLE),
    # Sign-in walls.
    ("Login required to view this video", E.SIGN_IN_REQUIRED),
    ("ERROR: [youtube] a: This video is only available for registered users. "
     "Use --cookies-from-browser or --cookies for the authentication.", E.SIGN_IN_REQUIRED),
    # Network.
    ("Connection reset by peer", E.TRANSIENT_NETWORK),
    ("Read timed out", E.TRANSIENT_NETWORK),
    ("HTTP Error 502: Bad Gateway", E.TRANSIENT_NETWORK),
    ("ERROR: [youtube] CCCCCCCC: Unable to download API page: "
     "<urlopen error [Errno 54] Connection reset by peer>", E.TRANSIENT_NETWORK),
    # Formats.
    ("No media found", E.NO_FORMATS),
    # Challenge pages.
    ("Cloudflare challenge", E.ANTI_BOT_CHALLENGE),
    # A bare 429 with no extractor prefix.
    ("HTTP Error 429: Too Many Requests", E.RATE_LIMITED),
])
def test_short_wording_gets_its_class(text, expect):
    assert classify(text, YT) is expect


@pytest.mark.parametrize("text", [
    "ERROR: [youtube] abc: This live event will begin in 3 hours.",
    "ERROR: [youtube] abc: Premieres in 5 hours",
])
def test_a_stream_that_has_not_started_is_a_certain_answer(text):
    assert classify(text, YT) is E.UNAVAILABLE


# ── 3. URL pollution ─────────────────────────────────────────────────────────
#
# Each URL holds a classifier word in its path. None is a real failure.

URL_POLLUTION_CASES = [
    "https://www.tiktok.com/@private_account/video/123",
    "https://www.tiktok.com/@user_removed_in_2024/video/456",
    "https://www.youtube.com/watch?v=age_gate_demo",
    "https://www.youtube.com/watch?v=is_blocked_test",
    "https://www.tiktok.com/@geo_restricted_demo/video/789",
    "https://www.tiktok.com/@members_only_creator/video/111",
    "https://example.com/copyright_claim_test/video/222",
    "https://example.com/terminated_account/video/333",
    "https://www.youtube.com/watch?v=video_unavailable_demo",
    "https://www.youtube.com/watch?v=this_video_is_not_available",
]


@pytest.mark.parametrize("url", URL_POLLUTION_CASES)
def test_a_transport_error_that_echoes_the_url_is_read_as_the_transport_error(url):
    """The exact shape of the original bug: yt-dlp wraps a transport error in
    a message that echoes the URL. The class is the transport error's,
    whatever the URL's path says (never unavailable / sign-in / no formats /
    merge)."""
    raw = (
        f"ERROR: [Generic] {url}: Unable to download webpage: "
        f"Failed to perform, curl: (28) timed out fetching {url}."
    )
    assert classify(raw, url) is E.TRANSIENT_NETWORK


# ── 4. classifiable_text ─────────────────────────────────────────────────────

class TestClassifiableText:
    """The structural fix for URL-pollution bugs. Pinned so a refactor can't
    silently break it."""

    def test_strips_full_url(self):
        err = "Unable to download webpage: https://x.com/age_gate/y"
        out = classifiable_text(err, "https://x.com/age_gate/y")
        assert "age_gate" not in out

    def test_strips_hostname_in_addition_to_url(self):
        # Even when yt-dlp formats the URL slightly differently, the bare
        # hostname is also stripped.
        err = "ERROR: [tiktok.com] private_user: download failed"
        out = classifiable_text(err, "https://tiktok.com/@private_user/video/123")
        assert "tiktok.com" not in out

    def test_strips_curl_doc_url(self):
        # yt-dlp's curl-impersonate backend appends this URL on every
        # transport error.
        err = (
            "Failed to perform, curl: (28) Resolving timed out. "
            "See https://curl.se/libcurl/c/libcurl-errors.html first."
        )
        out = classifiable_text(err, None)
        assert "curl.se" not in out
        assert "libcurl-errors" not in out

    def test_curl_doc_url_with_no_request_url(self):
        out = classifiable_text("Failed: see https://curl.se/libcurl/c/libcurl-errors.html", None)
        assert "curl.se" not in out

    def test_lowercases_output(self):
        out = classifiable_text("HTTP Error 429: Too Many Requests", None)
        assert out == out.lower()

    def test_returns_lower_str_when_url_is_none(self):
        out = classifiable_text("HTTP Error 403: Forbidden", None)
        assert out == "http error 403: forbidden"

    def test_handles_exception_input(self):
        # Callers pass either str or Exception.
        e = ValueError("This video is private")
        out = classifiable_text(e, "https://example.com/v/1")
        assert "private" in out

    def test_bad_url_doesnt_raise(self):
        # Pure function: must NEVER raise.
        out = classifiable_text("some error", "this is not a url")
        assert isinstance(out, str)

    def test_handles_malformed_url(self):
        out = classifiable_text("HTTP Error 429", "not a real url at all")
        assert isinstance(out, str)

    def test_url_with_no_netloc(self):
        # `https:///` has an empty netloc: the `if host:` guard keeps an
        # empty-string replace from ever running.
        out = classifiable_text("some error", "https:///")
        assert "some error" in out


# ── 5. OutputInvalidError's type ─────────────────────────────────────────────

def test_an_invalid_output_is_our_typed_error_read_without_its_text():
    """Was TestOutputValidationErrorType (which pinned a yt-dlp subclass so
    the old attempt loop's `except` caught it). Since the rebuild it is OUR
    DownloadError carrying its class: the table sends it down the format
    chain whatever its message says (the bug it guards: an undersized output
    bypassing the format fallback)."""
    assert issubclass(OutputInvalidError, CoreDownloadError)
    err = OutputInvalidError("download output is only 3.8KB: HTTP Error 403: Forbidden, video unavailable")
    assert classify(err, YT) is E.OUTPUT_INVALID
