# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A scripted yt-dlp for the download engine's tests (no network).

`script` is a list consumed one item per `extract_info(download=True)` call:
a string raises yt-dlp's DownloadError with that text; an Exception instance
is raised as is; "ok" writes the output file and returns an info dict. The
last item repeats. Metadata-only calls (download=False) use `probe_script`
the same way, with "ok" returning info and writing nothing.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yt_dlp

from backend.download import cookies as cookies_mod
from backend.download import health, outputs, progress
from backend.download.cookies import CookieStore

VIDEO_BYTES = 200_000
INFO = {"id": "vid1", "title": "A title", "duration": 42, "uploader": "Someone", "height": 1080,
        "view_count": 10, "like_count": 2, "comment_count": 1, "tags": ["a"], "categories": ["Music"]}


def jar_text(n: int = 25) -> str:
    return "# Netscape HTTP Cookie File\n\n" + "".join(
        f".example{i}.com\tTRUE\t/\tTRUE\t1999999999\tname{i}\tvalue{i}\n" for i in range(n))


class Scripted:
    def __init__(self):
        self.script: list = ["ok"]
        self.probe_script: list = ["ok"]
        self.calls: list[dict] = []          # the options of every download attempt
        self.probe_calls: list[dict] = []
        self.waits: list[float] = []         # every wait the ladder asked for
        self.cancel_after_attempt: int | None = None
        self.info = dict(INFO)
        self.browser_reads = 0
        self.browser_error: Exception | None = None
        self.streak: list = []               # failures that counted toward the yt-dlp update streak
        self.on_ok = None                    # optional hook(opts) run instead of writing the default file

    def _next(self, script: list, n: int):
        return script[min(n, len(script) - 1)]

    @property
    def cancelled(self) -> bool:
        return self.cancel_after_attempt is not None and len(self.calls) >= self.cancel_after_attempt


@pytest.fixture
def ytdlp(monkeypatch, tmp_path):
    """Installs the scripted yt-dlp, a CookieStore with a fake browser, no real
    sleeping, and a clean health memory. Returns the `Scripted` controller."""
    s = Scripted()

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if not download:
                item = s._next(s.probe_script, len(s.probe_calls))
                s.probe_calls.append(dict(self.opts))
            else:
                item = s._next(s.script, len(s.calls))
                s.calls.append(dict(self.opts))
            if isinstance(item, BaseException):
                raise item
            if item != "ok":
                raise yt_dlp.utils.DownloadError(item)
            if download:
                if s.on_ok:
                    s.on_ok(self.opts)
                else:
                    stem = self.opts["outtmpl"].replace(".%(ext)s", "")
                    pps = self.opts.get("postprocessors") or []
                    audio = next((pp for pp in pps if pp.get("key") == "FFmpegExtractAudio"), None)
                    ext = audio["preferredcodec"] if audio else "mp4"
                    Path(f"{stem}.{ext}").write_bytes(b"x" * VIDEO_BYTES)
            return dict(s.info)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)

    def fake_browser(browser, dest):
        s.browser_reads += 1
        if s.browser_error:
            raise s.browser_error
        dest.write_text(jar_text())

    store = CookieStore(tmp_path / "cookies" / "browser_cookies.txt", extract=fake_browser,
                        detect=lambda: "chrome", diagnose=lambda b: "no_access",
                        disabled=lambda: False)
    monkeypatch.setattr(cookies_mod, "_store", store)
    s.store = store

    monkeypatch.setattr(progress, "cancellable_sleep", lambda seconds, job_id, loop: s.waits.append(seconds))
    monkeypatch.setattr(progress, "job_cancelled_from_thread", lambda job_id, loop: s.cancelled)
    monkeypatch.setattr("backend.services.video_utils.probe_duration", lambda path, default=0.0: 42.0)
    monkeypatch.setattr(outputs, "extract_audio_locally", lambda video, audio_dir, stem: None)
    monkeypatch.setattr(health, "_rate_limit_by_host", {})
    monkeypatch.setattr(health, "_consecutive_download_failures", 0)
    monkeypatch.setattr(health, "_count_failure", lambda: s.streak.append(1))
    return s
