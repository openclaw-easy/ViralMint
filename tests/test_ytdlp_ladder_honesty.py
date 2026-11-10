# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The download cascade ends honestly.

* A "success" must name a file that exists and holds bytes — yt-dlp returning
  cleanly is not that, and every caller saves a Library row on it.
* A 403 that survives the cookie drop ended only the attempt loop, so every
  remaining format rung repeated the refusal, with a sleep each time.
* A bot wall that the cookie retry didn't pass was retried on every remaining
  attempt and format, and the message told Instagram and TikTok users that
  "YouTube requires sign-in".
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.exceptions import DownloadError
from backend.services import ytdlp_service as YT
from tests.test_ytdlp_download import _DownloadError, _install_yt_dlp


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(YT, "_check_cooldown", lambda: 0)
    monkeypatch.setattr(YT, "_get_cookie_file", lambda: None)
    monkeypatch.setattr(YT, "_detect_cookie_browser", lambda: None)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda *_: None)
    return tmp_path


def _download(out_dir, url="https://youtu.be/testid"):
    return asyncio.run(YT.download_video(url, output_dir=out_dir, extract_audio=False))


class TestASuccessHasAFile:
    def _no_file(self, monkeypatch, out_dir, size=None):
        calls = _install_yt_dlp(monkeypatch, [None], out_dir)
        import sys
        ydl = sys.modules["yt_dlp"].YoutubeDL
        real = ydl.extract_info

        def extract_info(self, url, download=True):
            info = real(self, url, download)
            p = out_dir / "testid.mp4"
            if size is None:
                p.unlink()
            else:
                p.write_bytes(b"\x00" * size)
            return info
        monkeypatch.setattr(ydl, "extract_info", extract_info)
        return calls

    def test_a_clean_return_with_no_file_is_a_failure(self, env, monkeypatch):
        self._no_file(monkeypatch, env)
        with pytest.raises(DownloadError, match="no usable video file"):
            _download(env)

    def test_an_empty_file_is_a_failure(self, env, monkeypatch):
        self._no_file(monkeypatch, env, size=0)
        with pytest.raises(DownloadError, match="empty"):
            _download(env)

    def test_a_real_file_is_still_a_success(self, env, monkeypatch):
        _install_yt_dlp(monkeypatch, [None], env)
        assert Path(_download(env)["video_path"]).stat().st_size > 0


class TestA403EndsTheChain:
    def test_a_403_without_cookies_is_retried_once_then_ends(self, env, monkeypatch):
        calls = _install_yt_dlp(
            monkeypatch, [_DownloadError("ERROR: HTTP Error 403: Forbidden")] * 40, env)
        with pytest.raises(DownloadError) as e:
            _download(env)
        assert len(calls) == 2, "one retry, then no format rung repeats the refusal"
        assert "HTTP 403 Forbidden" in str(e.value) and "youtu.be" in str(e.value)

    def test_a_403_that_survives_the_cookie_drop_ends(self, env, monkeypatch, tmp_path):
        ck = tmp_path / "ck.txt"
        ck.write_text("# cookies\n")
        monkeypatch.setattr(YT, "_get_cookie_file", lambda: ck)
        calls = _install_yt_dlp(
            monkeypatch, [_DownloadError("ERROR: HTTP Error 403: Forbidden")] * 40, env)
        with pytest.raises(DownloadError):
            _download(env)
        assert len(calls) == 2
        assert "cookiefile" in calls[0]["opts"] and "cookiefile" not in calls[1]["opts"]

    def test_a_403_then_success_still_downloads(self, env, monkeypatch):
        calls = _install_yt_dlp(
            monkeypatch, [_DownloadError("ERROR: HTTP Error 403: Forbidden"), None], env)
        assert _download(env)["video_path"]
        assert len(calls) == 2


BOT = "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser"


class TestABotWallEndsAtOnce:
    def test_after_the_cookie_retry_it_ends(self, env, monkeypatch):
        monkeypatch.setattr(YT, "_detect_cookie_browser", lambda: "chrome")
        calls = _install_yt_dlp(monkeypatch, [_DownloadError(BOT)] * 40, env)
        with pytest.raises(DownloadError, match="YouTube requires sign-in"):
            _download(env)
        assert len(calls) == 2, "one cookie retry, then no format rung repeats the wall"

    def test_with_no_browser_it_ends_on_the_first(self, env, monkeypatch):
        calls = _install_yt_dlp(monkeypatch, [_DownloadError(BOT)] * 40, env)
        with pytest.raises(DownloadError):
            _download(env)
        assert len(calls) == 1

    def test_the_message_names_the_site_that_asked(self, env, monkeypatch):
        _install_yt_dlp(monkeypatch, [_DownloadError("ERROR: login required")] * 40, env)
        with pytest.raises(DownloadError) as e:
            _download(env, url="https://www.instagram.com/reel/abc/")
        assert "instagram.com" in str(e.value) and "YouTube" not in str(e.value)
