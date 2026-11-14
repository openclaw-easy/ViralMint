# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Reading a link without downloading it (`backend.download.info`).

Moved from the coverage suites of the deleted `ytdlp_service`:
YouTube channel-URL normalisation, the channel
listing, single-video metadata and subtitles-only extraction. yt-dlp is
mocked; the cookie store is one that never reads a browser.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yt_dlp

from backend.core.exceptions import DownloadError, VideoUnavailableError
from backend.download import cookies as cookies_mod
from backend.download import info
from backend.download.classify import ErrorClass
from backend.download.cookies import CookieStore


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def no_browser_cookies(monkeypatch, tmp_path):
    """Every yt-dlp call in `info` asks the cookie store for the jar. This one
    is switched off, so no test can reach a real browser or Keychain."""
    def never(browser, dest):
        raise AssertionError("a test tried to read a real browser's cookies")
    store = CookieStore(tmp_path / "cookies" / "browser_cookies.txt", extract=never,
                        detect=lambda: None, diagnose=lambda b: "error", disabled=lambda: True)
    monkeypatch.setattr(cookies_mod, "_store", store)


# ── _classify_known_url (regex platform classifier) ────────────────────────



# ── normalize_youtube_channel_url ──────────────────────────────────────────


class TestNormalizeYoutubeChannelUrl:
    def test_appends_videos_to_handle(self):
        out = info.normalize_youtube_channel_url("https://www.youtube.com/@porsche")
        assert out.endswith("/videos") or "/videos" in out

    def test_appends_videos_to_channel_id(self):
        out = info.normalize_youtube_channel_url("https://www.youtube.com/channel/UCabc")
        assert "/videos" in out

    def test_already_has_videos_left_alone(self):
        url = "https://www.youtube.com/@porsche/videos"
        assert info.normalize_youtube_channel_url(url) == url

    def test_non_channel_left_alone(self):
        url = "https://www.youtube.com/watch?v=abc"
        # Single-video URLs are not normalized — they're not channels.
        assert info.normalize_youtube_channel_url(url) == url


# ── is_channel_or_playlist_url ─────────────────────────────────────────────



# ── list_channel_videos ────────────────────────────────────────────────────


class TestListChannelVideos:
    def test_returns_entries_with_full_urls(self):
        """yt-dlp's flat extraction sometimes returns bare video IDs;
        list_channel_videos must reconstruct full URLs for YouTube IDs."""
        fake_ydl = MagicMock()
        fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
        fake_ydl.__exit__ = MagicMock(return_value=False)
        fake_ydl.extract_info = MagicMock(return_value={
            "entries": [
                {"id": "abc123", "url": "abc123", "title": "T1"},
                {"id": "def456", "url": "https://www.youtube.com/watch?v=def456", "title": "T2"},
            ]
        })
        with patch("yt_dlp.YoutubeDL", return_value=fake_ydl):
            result = _run(info.list_channel_videos("https://youtube.com/@porsche", max_videos=2))
        assert len(result) == 2
        # First entry's bare ID got expanded to a watch URL.
        assert all(r["url"].startswith("http") for r in result)

    def test_single_video_returns_one_entry(self):
        fake_ydl = MagicMock()
        fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
        fake_ydl.__exit__ = MagicMock(return_value=False)
        fake_ydl.extract_info = MagicMock(return_value={
            "id": "abc",
            "webpage_url": "https://www.youtube.com/watch?v=abc",
            "title": "Single",
        })
        with patch("yt_dlp.YoutubeDL", return_value=fake_ydl):
            result = _run(info.list_channel_videos("https://www.youtube.com/watch?v=abc"))
        assert len(result) == 1
        assert result[0]["video_id"] == "abc"

    def test_error_returns_empty_list(self):
        with patch("yt_dlp.YoutubeDL", side_effect=RuntimeError("probe fail")):
            result = _run(info.list_channel_videos("https://example.com/@x"))
        assert result == []

    def test_empty_info_returns_empty(self):
        fake_ydl = MagicMock()
        fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
        fake_ydl.__exit__ = MagicMock(return_value=False)
        fake_ydl.extract_info = MagicMock(return_value=None)
        with patch("yt_dlp.YoutubeDL", return_value=fake_ydl):
            result = _run(info.list_channel_videos("https://example.com/@x"))
        assert result == []


# ── get_video_info ─────────────────────────────────────────────────────────


class TestGetVideoInfo:
    def test_returns_info_dict(self):
        fake_ydl = MagicMock()
        fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
        fake_ydl.__exit__ = MagicMock(return_value=False)
        fake_ydl.extract_info = MagicMock(return_value={
            "id": "abc", "title": "Test", "duration": 90,
        })
        with patch("yt_dlp.YoutubeDL", return_value=fake_ydl):
            result = _run(info.get_video_info("https://x.com/v/abc"))
        assert result["id"] == "abc"
        assert result["title"] == "Test"

    def test_error_returns_empty_dict(self):
        with patch("yt_dlp.YoutubeDL", side_effect=RuntimeError("crash")):
            result = _run(info.get_video_info("https://x.com/v/abc"))
        assert result == {}


# ── extract_subtitles_only retry wrapper ───────────────────────────────────

HEALTHY = {
    "id": "vid1", "title": "T", "duration": 12,
    "subtitles": {"en": [{}]}, "automatic_captions": {},
}


class _FakeSubYDL:
    """Scripted fake for the subtitle path's two yt-dlp calls per attempt
    (metadata probe, then subtitle write)."""
    script: list = []
    calls = 0

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        _FakeSubYDL.calls += 1
        outcome = _FakeSubYDL.script.pop(0) if _FakeSubYDL.script else None
        if isinstance(outcome, Exception):
            raise outcome
        if download and self.opts.get("outtmpl"):
            # Materialize the subtitle file the real yt-dlp would write.
            stem = self.opts["outtmpl"].replace(".%(ext)s", "")
            Path(f"{stem}.en.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nhello world\n"
            )
        return outcome or dict(HEALTHY)


@pytest.fixture
def fake_sub_ytdlp(monkeypatch):
    """The real `yt_dlp` module with a scripted YoutubeDL, and no real 3 s
    pause between the two tries. Returns the list of pauses asked for."""
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeSubYDL)
    _FakeSubYDL.script = []
    _FakeSubYDL.calls = 0
    pauses: list[float] = []

    async def fast_sleep(seconds):
        pauses.append(seconds)
    # Only `info`'s view of asyncio: patching asyncio.sleep itself would also
    # change the event loop the test runs on.
    monkeypatch.setattr(info, "asyncio", SimpleNamespace(
        wait_for=asyncio.wait_for, TimeoutError=asyncio.TimeoutError, sleep=fast_sleep))
    return pauses


@pytest.mark.asyncio
async def test_subtitles_transient_error_retried_once(fake_sub_ytdlp, tmp_path):
    """One transient failure then success — added 2026-07-28; the path
    was single-shot before."""
    _FakeSubYDL.script = [
        yt_dlp.utils.DownloadError("Unable to download webpage: The read operation timed out"),
        dict(HEALTHY),      # retry: metadata probe
        dict(HEALTHY),      # retry: subtitle write
    ]

    result = await info.extract_subtitles_only(
        "https://www.youtube.com/watch?v=vid1", output_dir=tmp_path,
    )

    assert result["language"] == "en"
    assert result["format"] == "srt"
    assert result["path"].endswith(".srt")
    assert len(fake_sub_ytdlp) == 1, "exactly one pause, one retry"


@pytest.mark.asyncio
async def test_subtitles_no_subs_fails_fast_without_retry(fake_sub_ytdlp, tmp_path):
    """VideoUnavailableError (no subtitles) must NOT burn the retry."""
    _FakeSubYDL.script = [
        {"id": "vid1", "title": "T", "duration": 12,
         "subtitles": {}, "automatic_captions": {}},
    ]

    with pytest.raises(VideoUnavailableError):
        await info.extract_subtitles_only(
            "https://www.youtube.com/watch?v=vid1", output_dir=tmp_path,
        )

    assert _FakeSubYDL.script == [], "exactly one probe, no retry"
    assert _FakeSubYDL.calls == 1 and fake_sub_ytdlp == []


@pytest.mark.asyncio
async def test_subtitles_persistent_failure_raises_after_one_retry(fake_sub_ytdlp, tmp_path):
    """Two 403s: one retry, then OUR exception with the class's sentence
    (before the rebuild this surfaced yt-dlp's raw DownloadError)."""
    _FakeSubYDL.script = [
        yt_dlp.utils.DownloadError("ERROR: HTTP Error 403: Forbidden"),
        yt_dlp.utils.DownloadError("ERROR: HTTP Error 403: Forbidden"),
    ]

    with pytest.raises(DownloadError) as err:
        await info.extract_subtitles_only(
            "https://www.youtube.com/watch?v=vid1", output_dir=tmp_path,
        )

    assert not isinstance(err.value, yt_dlp.utils.DownloadError)
    assert err.value.error_class is ErrorClass.HTTP_403
    assert "ERROR:" not in str(err.value)
    assert _FakeSubYDL.calls == 2, "one try and exactly one retry"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_subtitles_error_a_retry_cannot_fix_is_not_retried(fake_sub_ytdlp, tmp_path):
    """Only network blips and 403s get the second try; a removed video ends
    at once, in words."""
    _FakeSubYDL.script = [
        yt_dlp.utils.DownloadError("ERROR: [youtube] vid1: Video unavailable. This video has been removed by the uploader"),
    ]

    with pytest.raises(VideoUnavailableError) as err:
        await info.extract_subtitles_only(
            "https://www.youtube.com/watch?v=vid1", output_dir=tmp_path,
        )

    assert _FakeSubYDL.calls == 1 and fake_sub_ytdlp == []
    assert "ERROR:" not in str(err.value)
