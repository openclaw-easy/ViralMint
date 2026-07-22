# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Round-2 coverage for `backend.services.ytdlp_service`.

Ported from the hosted variant and adapted for the OSS build. The hosted
manual-merge cascade, cookie-refresh cooldown, and cloud billing wiring
don't exist here, so those suites were dropped; what remains targets the
metadata-probe + local-audio-extract surface that both variants share:

  - `_extract_audio_locally` (ffmpeg subprocess paths)
  - `is_channel_or_playlist_url` (string classifier)
  - `list_channel_videos` / `get_video_info` (yt-dlp probe mocks)
  - `_consecutive_download_failures` sanity

Each test mocks the external side-effects (ffmpeg, yt-dlp, cookies) so
the orchestration logic runs without booting them for real.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

import backend.services.ytdlp_service as ys


def _run(coro):
    return asyncio.run(coro)


# ── _extract_audio_locally ─────────────────────────────────────────────────


class TestExtractAudioLocally:
    def test_successful_extraction(self, tmp_path):
        v = tmp_path / "v.mp4"
        v.write_bytes(b"v" * 1000)

        def fake_run(cmd, *args, **kwargs):
            # Simulate ffmpeg creating the audio file.
            out = Path(cmd[-1])
            out.write_bytes(b"audio" * 100)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("subprocess.run", side_effect=fake_run):
            out = ys._extract_audio_locally(v, tmp_path, "v")
        assert out is not None
        assert out.exists()

    def test_ffmpeg_failure_returns_none(self, tmp_path):
        v = tmp_path / "v.mp4"
        v.write_bytes(b"v" * 1000)

        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stderr = "ffmpeg failed"
            return result

        with patch("subprocess.run", side_effect=fake_run):
            out = ys._extract_audio_locally(v, tmp_path, "v")
        assert out is None

    def test_ffmpeg_missing_returns_none(self, tmp_path):
        v = tmp_path / "v.mp4"
        v.write_bytes(b"v" * 1000)
        with patch("subprocess.run", side_effect=FileNotFoundError("ffmpeg")):
            out = ys._extract_audio_locally(v, tmp_path, "v")
        assert out is None

    def test_ffmpeg_timeout_returns_none(self, tmp_path):
        v = tmp_path / "v.mp4"
        v.write_bytes(b"v" * 1000)
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired("ffmpeg", 300)):
            out = ys._extract_audio_locally(v, tmp_path, "v")
        assert out is None


# ── is_channel_or_playlist_url (string classifier) ─────────────────────────


class TestIsChannelOrPlaylistUrl:
    """OSS uses a pure string classifier (no yt-dlp probe). Single-video
    watch URLs are False; channel/playlist shapes are True."""

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
    ])
    def test_single_video_false(self, url):
        assert ys.is_channel_or_playlist_url(url) is False

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/@porsche",
        "https://www.youtube.com/channel/UCabc",
        "https://www.youtube.com/c/abc",
        "https://www.youtube.com/user/abc",
        "https://www.youtube.com/playlist?list=PLabc",
        "https://www.youtube.com/@porsche/videos",
    ])
    def test_channel_or_playlist_true(self, url):
        assert ys.is_channel_or_playlist_url(url) is True

    def test_watch_url_with_list_is_true(self):
        # A watch URL that also carries &list= is part of a playlist.
        url = "https://www.youtube.com/watch?v=abc&list=PLxyz"
        assert ys.is_channel_or_playlist_url(url) is True


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
        with patch("yt_dlp.YoutubeDL", return_value=fake_ydl), \
             patch.object(ys, "_apply_cookies", return_value=None):
            result = _run(ys.list_channel_videos("https://youtube.com/@porsche", max_videos=2))
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
        with patch("yt_dlp.YoutubeDL", return_value=fake_ydl), \
             patch.object(ys, "_apply_cookies", return_value=None):
            result = _run(ys.list_channel_videos("https://www.youtube.com/watch?v=abc"))
        assert len(result) == 1
        assert result[0]["video_id"] == "abc"

    def test_error_returns_empty_list(self):
        with patch("yt_dlp.YoutubeDL", side_effect=RuntimeError("probe fail")), \
             patch.object(ys, "_apply_cookies", return_value=None):
            result = _run(ys.list_channel_videos("https://example.com/@x"))
        assert result == []

    def test_empty_info_returns_empty(self):
        fake_ydl = MagicMock()
        fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
        fake_ydl.__exit__ = MagicMock(return_value=False)
        fake_ydl.extract_info = MagicMock(return_value=None)
        with patch("yt_dlp.YoutubeDL", return_value=fake_ydl), \
             patch.object(ys, "_apply_cookies", return_value=None):
            result = _run(ys.list_channel_videos("https://example.com/@x"))
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
        with patch("yt_dlp.YoutubeDL", return_value=fake_ydl), \
             patch.object(ys, "_apply_cookies", return_value=None):
            result = _run(ys.get_video_info("https://x.com/v/abc"))
        assert result["id"] == "abc"
        assert result["title"] == "Test"

    def test_error_returns_empty_dict(self):
        with patch("yt_dlp.YoutubeDL", side_effect=RuntimeError("crash")), \
             patch.object(ys, "_apply_cookies", return_value=None):
            result = _run(ys.get_video_info("https://x.com/v/abc"))
        assert result == {}


# ── _consecutive_download_failures ─────────────────────────────────────────


class TestConsecutiveFailureTracker:
    def test_module_has_failure_counter(self):
        # Behavioral coverage for _record_download_failure lives in
        # test_ytdlp_update.py; here just pin the global's existence.
        assert hasattr(ys, "_consecutive_download_failures")
