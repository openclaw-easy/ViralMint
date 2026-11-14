# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for `messages.humanize_download_error`: the words a person sees.

These pin the user-facing sentence for each kind of raw yt-dlp error. Every
previous "huge red error in chat" bug shipped because the humanizer was
bypassed or didn't know the new yt-dlp wording. Since the rebuild the
humanizer classifies raw text (`classify`) and says that class's sentence
(`messages.for_class`); the sentences changed on purpose, the rules did not:
one short line, no paths, no cookies, no tokens, never past the length cap.
"""
from __future__ import annotations

import pytest

from backend.download.messages import humanize_download_error


# ── Raw yt-dlp text → its class's sentence ──────────────────────────────────


class TestHumanizeDownloadErrorPatterns:
    """One raw wording per sentence. If an edit drops a mapping, the row that
    depends on it breaks loudly."""

    @pytest.mark.parametrize("raw, expected_phrase", [
        # Auth / bot detection
        ("ERROR: Sign in to confirm you're not a bot. Use cookies.", "signed in"),
        ("ERROR: This client appears to be NOT A BOT detection.", "signed in"),
        ("ERROR: Login required to view this video", "signed in"),
        # Specific availability states
        ("ERROR: This is a Private video", "private"),
        ("ERROR: This video is members-only content", "members-only"),
        ("ERROR: Join this channel to access this content", "members-only"),
        ("ERROR: Video unavailable. This video has been blocked.", "unavailable"),
        ("ERROR: This video has been removed by the uploader.", "removed"),
        ("ERROR: This video is no longer available", "removed"),
        # Live streams: no class of their own, the headline is shown as is.
        ("ERROR: This is a live event — wait for it to end", "live event"),
        ("ERROR: Requested format is not available", "downloadable"),
        # Geo
        ("ERROR: This video is not available in your country", "blocked in your country"),
        # yt-dlp's GeoRestrictedError default wording.
        ("ERROR: This video is not available from your location due to geo restriction", "region-locked"),
        # HTTP
        ("ERROR: HTTP Error 429: Too Many Requests", "rate-limiting"),
        ("ERROR: HTTP Error 403: Forbidden", "HTTP 403"),
        ("ERROR: HTTP Error 404: Not Found", "HTTP 404"),
        ("ERROR: HTTP Error 412: Precondition Failed", "bot check"),
        # Network
        ("ERROR: Temporary failure in name resolution", "network problem"),
        ("ERROR: SSL certificate problem", "network problem"),
        ("ERROR: Connection refused by peer", "network problem"),
        ("ERROR: Connection timed out after 30s", "network problem"),
        # Extractor missing
        ("ERROR: Unsupported URL: https://x.com/v", "standard extractor"),
        # A cookie file yt-dlp couldn't parse (the original "2KB cookie
        # blob in chat" trigger): still says it is about cookies.
        ("ERROR: invalid Netscape format cookies file: '/tmp/x'", "cookies"),
    ])
    def test_pattern_maps_to_phrase(self, raw, expected_phrase):
        out = humanize_download_error(raw)
        assert expected_phrase.lower() in out.lower(), \
            f"raw={raw!r} → out={out!r} did not contain {expected_phrase!r}"
        assert "ERROR:" not in out and "\n" not in out

    def test_unsupported_url_names_the_site(self):
        # Was TestFriendlyErrorMessage (`_friendly_error_message`).
        out = humanize_download_error(
            "ERROR: Unsupported URL: https://example.com/video", "https://example.com/video")
        assert "extractor" in out
        assert "example.com" in out

    def test_raw_text_without_its_url_does_not_blame_youtube(self):
        out = humanize_download_error("ERROR: Unsupported URL: https://some-site.example/watch/9")
        assert "YouTube" not in out


# ── Nothing private, nothing long ────────────────────────────────────────────


class TestHumanizeDownloadErrorFallbacks:
    """What protects the chat bubble from cookie blobs, paths and huge JWTs
    when the text has no class of its own."""

    def test_empty_string(self):
        assert humanize_download_error("") == "Download failed."

    def test_whitespace_only(self):
        assert humanize_download_error("   \n\n\t  ") == "Download failed."

    def test_takes_first_line_only(self):
        """Multiline yt-dlp dumps — the first line is the headline; the
        rest is usually file paths / cookie blobs / tracebacks. Must NOT
        leak past the first line."""
        raw = (
            "ERROR: Something broke\n"
            "  File '/Users/x/very/long/path/that/should/never/leak'\n"
            "  cookie: a-very-long-base64-encoded-secret=="
        )
        out = humanize_download_error(raw)
        assert "Something broke" in out
        assert "secret" not in out
        assert "/Users" not in out

    def test_strips_error_prefix(self):
        out = humanize_download_error("ERROR: Bare message no pattern match")
        assert not out.startswith("ERROR:")
        assert "ERROR:" not in out

    def test_accepts_exception_input(self):
        # Callers pass either str or Exception; the humanizer handles both.
        out = humanize_download_error(ValueError("ERROR: Sign in to confirm you're not a bot"))
        assert "signed in" in out.lower()

    def test_our_own_sentence_passes_through(self):
        # Deliberate (2026-11-13): only raw yt-dlp text ("ERROR:" in it) is
        # re-read. A sentence we wrote is already what the person should see.
        ours = "instagram.com wants you signed in before it serves this video."
        from backend.core.exceptions import BotDetectionError
        assert humanize_download_error(BotDetectionError(ours)) == ours      # as our exception
        assert humanize_download_error(ours) == ours                          # as a stored job error

    def test_another_exceptions_internals_never_reach_the_person(self):
        # A KeyError's key, an OSError's path, a browser crash: not sentences.
        for exc in (KeyError("id"), OSError("[Errno 13] Permission denied: '/Users/me/x'"), RuntimeError("chrome crashed")):
            assert humanize_download_error(exc) == "Download failed unexpectedly. Please try again."

    def test_fallback_truncates_long_headline_with_colon_noise(self):
        """When the headline has a colon and the trailing part looks
        structurally noisy (paths, equals signs), trim it. Prevents
        leaking cookie/JWT-shaped strings."""
        noisy = "ERROR: Something failed: " + "/Users/x/" * 30 + "secret_token=abc123"
        out = humanize_download_error(noisy)
        assert "secret_token" not in out
        assert "/Users" not in out

    def test_a_long_cookie_blob_never_reaches_the_person(self):
        """The original trigger: a rejected cookie file quoted, 2 KB of it."""
        blob = "SID=" + "A1b2C3d4" * 250
        raw = f"ERROR: invalid Netscape format cookies file '/tmp/cookies.txt': '{blob}'"
        out = humanize_download_error(raw)
        assert "A1b2C3d4" not in out
        assert len(out) <= 260

    def test_a_short_rejected_cookie_line_never_reaches_the_person(self):
        raw = ("ERROR: invalid Netscape format cookies file '/tmp/browser_cookies.txt': "
               "'.youtube.com\\tTRUE\\t/\\tTRUE\\t0\\tSID\\tSECRETVALUE123'")
        assert "SECRETVALUE123" not in humanize_download_error(raw)

    def test_fallback_hard_truncates_long_headline_without_noise(self):
        # Headline has no colon and is just very long — hard truncate
        # with an ellipsis. Pin so we never leak past max_len.
        long_msg = "A" * 500
        out = humanize_download_error(long_msg, max_len=50)
        assert len(out) <= 53  # 50 + "…" (potentially 3-byte char)
        assert out.endswith("…")

    @pytest.mark.parametrize("raw", [
        "ERROR: " + "B" * 5000,                                              # unknown, no colon noise
        "ERROR: [youtube] a: HTTP Error 403: Forbidden " + "C" * 5000,       # a class with a sentence
        "ERROR: unable to rename file: '" + "/d" * 3000 + "'",               # merge: headline shown
        "ERROR: Postprocessing: " + "E=" * 3000,
    ])
    def test_raw_text_of_any_length_stays_one_short_line(self, raw):
        out = humanize_download_error(raw, "https://www.youtube.com/watch?v=a")
        assert len(out) <= 260 and "\n" not in out
        out = humanize_download_error(raw, "https://www.youtube.com/watch?v=a", max_len=50)
        assert len(out) <= 110

    def test_fallback_keeps_short_headline_as_is(self):
        out = humanize_download_error("Brief unique error")
        assert out == "Brief unique error"
