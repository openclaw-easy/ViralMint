# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The small modules of backend/download: options, the downloader's health
memory, and the words shown to people."""
import re

import pytest

from backend.core.exceptions import VideoUnavailableError
from backend.download import health, messages, opts
from backend.download.classify import ErrorClass as E
from backend.download.classify import headline, unavailable_kind

URL = "https://www.youtube.com/watch?v=abc"


# ── opts ─────────────────────────────────────────────────────────────────────

def test_build_has_every_layer_and_no_cookie_key_without_a_jar():
    o = opts.build(URL, jar=None, socket_timeout=15)
    assert {"quiet", "logger", "noplaylist", "extractor_args", "js_runtimes", "http_headers"} <= set(o)
    assert o["socket_timeout"] == 15 and o["http_headers"]["User-Agent"]
    assert "cookiefile" not in o and "cookiesfrombrowser" not in o


def test_build_uses_the_jar_and_never_a_live_browser_read(tmp_path):
    jar = tmp_path / "jar.txt"
    o = opts.build(URL, jar=jar, cookiesfrombrowser=("chrome",))     # even if a caller asks for one
    assert o["cookiefile"] == str(jar) and "cookiesfrombrowser" not in o


def test_drop_cookies_reports_whether_anything_was_set(tmp_path):
    o = opts.build(URL, jar=tmp_path / "jar.txt")
    assert opts.drop_cookies(o) is True and "cookiefile" not in o
    assert opts.drop_cookies(o) is False


def test_x_links_get_the_referer_their_cdn_checks():
    assert opts.http_headers("https://x.com/a/status/1")["Referer"] == "https://x.com/"
    assert opts.http_headers(URL)["Referer"] == "https://www.google.com/"
    assert opts.http_headers("https://video.twimg.com/v.mp4")["Referer"] == "https://x.com/"
    # A host that merely ENDS in "x.com" is not X.
    for other in ("https://www.dropbox.com/s/a.mp4", "https://box.com/f/1"):
        assert opts.http_headers(other)["Referer"] == "https://www.google.com/"



def test_a_playlist_result_is_unwrapped_to_its_first_video():
    nested = {"_type": "playlist", "entries": [{"_type": "playlist", "entries": [None, {"id": "v1"}]}]}
    assert opts.single_video_info(nested) == {"id": "v1"}
    assert opts.single_video_info({"id": "v2"}) == {"id": "v2"}
    assert opts.single_video_info(None) is None
    with pytest.raises(VideoUnavailableError):
        opts.single_video_info({"_type": "playlist", "entries": []})


def test_format_chain_ends_on_the_single_best_stream():
    assert len(opts.FORMAT_CHAIN) == 3 and opts.FORMAT_CHAIN[-1] == "best"


# ── health ───────────────────────────────────────────────────────────────────

@pytest.fixture
def clean_health(monkeypatch):
    monkeypatch.setattr(health, "_consecutive_download_failures", 0)
    monkeypatch.setattr(health, "_rate_limit_by_host", {})
    counted = []
    monkeypatch.setattr(health, "_count_failure", lambda: counted.append(1))
    return counted


@pytest.mark.parametrize("cls", [c for c in E if c not in (E.UNKNOWN, E.NO_FORMATS)])
def test_only_unexplained_failures_feed_the_update_streak(clean_health, cls):
    assert health.record_failure(cls, URL) is False and clean_health == []


@pytest.mark.parametrize("cls", [E.UNKNOWN, E.NO_FORMATS])
def test_an_unexplained_failure_on_a_supported_site_counts(clean_health, cls):
    assert health.record_failure(cls, URL) is True and clean_health == [1]


def test_a_failure_on_a_site_yt_dlp_has_no_extractor_for_never_counts(clean_health):
    assert health.record_failure(E.UNKNOWN, "https://some-site.example/watch/9") is False
    assert clean_health == []


def test_a_success_clears_the_streak(monkeypatch):
    monkeypatch.setattr(health, "_consecutive_download_failures", 2)
    health.record_success(URL)
    assert health._consecutive_download_failures == 0


def test_a_429_arms_a_cooldown_for_that_host_only(clean_health):
    assert health.cooldown_for(URL) == 0
    health.record_rate_limit(URL)
    assert 0 < health.cooldown_for(URL) <= health.RATE_LIMIT_BACKOFF_BASE
    assert health.cooldown_for("https://www.youtube.com/watch?v=other") > 0
    assert health.cooldown_for("https://www.tiktok.com/@a/video/1") == 0


def test_repeated_429s_back_off_longer_up_to_the_cap(clean_health):
    waits = [health.record_rate_limit(URL) for _ in range(5)]
    assert waits == [60, 120, 240, 300, 300]


# ── messages ─────────────────────────────────────────────────────────────────

def test_every_class_has_a_sentence_without_raw_ytdlp_text():
    raw = "ERROR: [youtube] abc: something /Users/me/secret/cookies.txt SESSION=abcdef"
    for cls in E:
        sentence = messages.for_class(cls, raw, URL)
        assert sentence and "\n" not in sentence
        if cls is not E.UNKNOWN and cls is not E.MERGE_RENAME and cls is not E.POSTPROCESSING:
            assert "secret" not in sentence and "SESSION" not in sentence, cls


@pytest.mark.parametrize("text,kind,sentence", [
    ("ERROR: [youtube] a: Private video. Sign in if you've been granted access", "private", "This video is private."),
    ("ERROR: [youtube] a: Join this channel to get access to members-only content", "members", "This video is members-only."),
    ("ERROR: [youtube] a: Video unavailable. The uploader has not made this video available in your country", "geo", "This video is blocked in your country."),
    ("ERROR: [youtube] a: This video has been removed by the uploader.", "removed", "This video has been removed."),
    ("ERROR: [youtube] a: Video unavailable", "", "Video unavailable. It may be removed, private or region-locked."),
])
def test_unavailable_says_why_when_the_site_said_why(text, kind, sentence):
    assert unavailable_kind(text, URL) == kind
    assert messages.for_class(E.UNAVAILABLE, text, URL) == sentence
    assert messages.humanize_download_error(text) == sentence


def test_sign_in_message_names_the_site_that_asked():
    assert re.search(r"\byoutube\.com\b", messages.sign_in_message(URL))
    msg = messages.sign_in_message("https://www.instagram.com/reel/abc/")
    assert re.search(r"\binstagram\.com\b", msg) and "YouTube" not in msg


def test_sign_in_message_does_not_say_sign_in_when_the_browser_cannot_be_read():
    hint = "macOS is blocking ViralMint from reading Chrome's cookies…"
    msg = messages.sign_in_message(URL, hint)
    assert hint in msg and "open Chrome" not in msg


def test_humanize_passes_our_own_sentences_through_and_translates_raw_text():
    ours = "Download stalled: no data for 10 minutes (0.0 MB received)."
    assert messages.humanize_download_error(ours) == ours
    assert messages.humanize_download_error("") == "Download failed."
    raw = "Download failed: ERROR: [youtube] a: Unable to download webpage: HTTP Error 429: Too Many Requests"
    assert "rate-limiting" in messages.humanize_download_error(raw, URL)


def test_headline_drops_later_lines_paths_and_long_blobs():
    assert headline("ERROR: first line\n  File \"/x/y.py\"\nTraceback") == "first line"
    long = "ERROR: Invalid cookies: " + "a/b=" * 200
    assert headline(long) == "Invalid cookies."
    assert len(headline("x" * 500)) == 201


def test_batch_failure_message():
    f = messages.download_failure_message
    assert "rate-limiting" in f([], 3, abort_reason="rate-limited")
    assert f(["ERROR: [youtube] a: Private video"], 1) == "This video is private."
    assert f(["ERROR: [youtube] a: Private video", "x"], 2) == "All 2 downloads failed: This video is private."
    assert f([], 2).startswith("All downloads failed")
