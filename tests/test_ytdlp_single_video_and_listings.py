# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A video link means the video; lookups are bounded and say why they failed.

* A link that names a video AND a playlist (`watch?v=…&list=`, a YouTube
  Music radio mix) used to make yt-dlp take the playlist: every track of an
  endless mix downloaded onto ONE output stem, each overwriting the last.
  `noplaylist` in the base opts is yt-dlp's own answer (each extractor decides
  the shape); `playlist_items=1` + `_single_video_info` are the backstop for a
  bare playlist URL handed to a single-video path. A channel root nests twice
  (channel -> "Videos" tab -> video), so the unwrap goes as deep as it must.
* `get_video_info(flat=False)` on a channel fully extracted EVERY entry, with
  no timeout; the channel analysis listed a whole /videos tab to read 30.
* `list_channel_videos` had no outer bound and folded every error into [],
  so a channel download failed as "No videos found".
"""
import asyncio
import threading
from unittest.mock import AsyncMock, patch

import pytest
import yt_dlp

from backend.core.exceptions import DownloadError, VideoUnavailableError
from backend.core.executors import download_pool
from backend.services import ytdlp_service

MIX_URL = "https://music.youtube.com/watch?v=nio23E_jyN4&list=RDAMVMnio23E_jyN4"


class _Fake:
    seen: list = []
    result = None

    def __init__(self, opts):
        _Fake.seen.append(dict(opts))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        r = _Fake.result
        if isinstance(r, Exception):
            raise r
        return r() if callable(r) else r

    def prepare_filename(self, info):
        return str(_Fake.out_dir / f"{info['id']}.mp4")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    _Fake.seen = []
    _Fake.out_dir = tmp_path
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _Fake)
    monkeypatch.setattr(ytdlp_service, "_apply_cookies", lambda o: None)
    monkeypatch.setattr(ytdlp_service, "_check_cooldown", lambda: 0)
    return _Fake


PLAYLIST = {"_type": "playlist", "id": "PL", "title": "A channel",
            "entries": [{"id": "v1", "title": "First", "duration": 61}, {"id": "v2"}]}


# ── the video, not its playlist ─────────────────────────────────────────────

def test_base_opts_prefer_the_video_over_its_playlist():
    assert ytdlp_service._yt_dlp_base_opts()["noplaylist"] is True


def test_real_youtube_extractor_honours_it_for_a_mix_link():
    """Pins the semantics against the real yt-dlp (offline): with our opts the
    youtube:tab extractor declines the playlist and defers to the video."""
    with yt_dlp.YoutubeDL(ytdlp_service._yt_dlp_base_opts()) as ydl:
        tab = ydl.get_info_extractor("YoutubeTab")
        assert tab._yes_playlist("RDAMVMnio23E_jyN4", "nio23E_jyN4") is False


class TestSingleVideoInfo:
    def _nested(self, inner, depth):
        for i in range(depth):
            inner = {"_type": "playlist", "id": f"L{i}", "entries": [inner]}
        return inner

    def test_video_passes_through(self):
        info = {"id": "v", "title": "T"}
        assert ytdlp_service._single_video_info(info) is info

    def test_none_passes_through(self):
        assert ytdlp_service._single_video_info(None) is None

    def test_playlist_unwraps_to_first_entry(self):
        info = {"_type": "playlist", "id": "RDx", "entries": [None, {"id": "v"}, {"id": "w"}]}
        assert ytdlp_service._single_video_info(info) == {"id": "v"}

    def test_empty_playlist_is_unavailable_not_a_crash(self):
        with pytest.raises(VideoUnavailableError):
            ytdlp_service._single_video_info({"_type": "playlist", "entries": []})

    def test_a_channel_tab_unwraps_to_the_video(self):
        video = {"id": "v1", "duration": 61}
        assert ytdlp_service._single_video_info(self._nested(video, 2)) == video

    def test_an_empty_inner_tab_is_unavailable(self):
        info = {"_type": "playlist", "entries": [{"_type": "playlist", "entries": []}]}
        with pytest.raises(VideoUnavailableError):
            ytdlp_service._single_video_info(info)

    def test_nesting_is_bounded(self):
        info = self._nested({"id": "v1"}, ytdlp_service._MAX_PLAYLIST_DEPTH + 1)
        with pytest.raises(VideoUnavailableError):
            ytdlp_service._single_video_info(info)

    def test_deepest_allowed_nesting_still_unwraps(self):
        video = {"id": "v1"}
        nested = self._nested(video, ytdlp_service._MAX_PLAYLIST_DEPTH)
        assert ytdlp_service._single_video_info(nested) == video


def test_video_download_is_capped_to_one_item_and_unwrapped(fake, tmp_path):
    """A bare playlist URL (no video id, so `noplaylist` can't help) fetches
    ONE item, and the result describes that item, not the wrapper."""
    def _result():
        (tmp_path / "vid.mp4").write_bytes(b"\x00" * 4096)
        return {"_type": "playlist", "id": "PLx", "title": "Some playlist",
                "entries": [{"id": "vid", "title": "First song", "duration": 200}]}
    fake.result = _result
    out = asyncio.run(ytdlp_service.download_video(
        "https://www.youtube.com/playlist?list=PLx", output_dir=tmp_path,
        filename="vid", extract_audio=False))
    assert fake.seen[0]["playlist_items"] == "1"
    assert fake.seen[0]["noplaylist"] is True
    assert out["title"] == "First song" and out["duration"] == 200


# ── URL routing ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    MIX_URL,
    "https://www.youtube.com/watch?v=abc&list=PLxyz",
    "https://youtu.be/abc?si=x&list=PLxyz",
])
def test_a_video_link_that_names_a_playlist_takes_the_single_video_path(url):
    assert ytdlp_service.is_channel_or_playlist_url(url) is False


@pytest.mark.parametrize("url,want", [
    ("https://www.youtube.com/@CNN", "https://www.youtube.com/@CNN/videos"),
    ("https://www.youtube.com/@CNN/", "https://www.youtube.com/@CNN/videos"),
    ("https://www.youtube.com/channel/UCabc", "https://www.youtube.com/channel/UCabc/videos"),
    ("https://www.youtube.com/@CNN/shorts", "https://www.youtube.com/@CNN/shorts"),
    ("https://www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"),
    ("https://www.youtube.com/playlist?list=PL", "https://www.youtube.com/playlist?list=PL"),
    ("https://www.tiktok.com/@x", "https://www.tiktok.com/@x"),
])
def test_channel_roots_list_their_videos_tab(url, want):
    assert ytdlp_service.normalize_youtube_channel_url(url) == want


# ── get_video_info ──────────────────────────────────────────────────────────

class TestGetVideoInfo:
    def test_full_info_on_a_playlist_is_one_video(self, fake):
        fake.result = PLAYLIST
        info = asyncio.run(ytdlp_service.get_video_info("https://www.youtube.com/@x", flat=False))
        assert info["id"] == "v1" and info["duration"] == 61
        assert fake.seen[0]["playlist_items"] == "1"

    def test_flat_listing_is_not_capped(self, fake):
        fake.result = PLAYLIST
        info = asyncio.run(ytdlp_service.get_video_info("https://www.youtube.com/@x", flat=True))
        assert info["_type"] == "playlist"
        assert "playlist_items" not in fake.seen[0] and "playlistend" not in fake.seen[0]

    def test_a_flat_listing_asks_for_only_what_it_reads(self, fake):
        fake.result = PLAYLIST
        asyncio.run(ytdlp_service.get_video_info(
            "https://www.youtube.com/@x/videos", flat=True, max_entries=30))
        assert fake.seen[0]["playlistend"] == 30

    def test_a_one_video_lookup_ignores_max_entries(self, fake):
        fake.result = PLAYLIST
        asyncio.run(ytdlp_service.get_video_info("https://www.youtube.com/@x", flat=False, max_entries=30))
        assert "playlistend" not in fake.seen[0]

    def test_a_wedged_extraction_times_out(self, fake, monkeypatch):
        monkeypatch.setattr(ytdlp_service, "INFO_TIMEOUT_S", 0.2)
        release = threading.Event()
        fake.result = lambda: (release.wait(5), {"id": "late"})[1]

        async def go():
            try:
                return await ytdlp_service.get_video_info("https://x.example/v", flat=False)
            finally:
                release.set()
                await download_pool.wait_until_occupied(0, timeout=5)
        assert asyncio.run(go()) == {}


# ── list_channel_videos ─────────────────────────────────────────────────────

class TestListChannelVideos:
    def test_default_contract_still_returns_empty_on_error(self, fake):
        fake.result = yt_dlp.utils.DownloadError("ERROR: This channel does not exist")
        assert asyncio.run(ytdlp_service.list_channel_videos("https://www.youtube.com/@gone")) == []

    def test_raise_errors_says_why(self, fake):
        fake.result = yt_dlp.utils.DownloadError(
            "ERROR: Private video. Sign in if you've been granted access")
        with pytest.raises(VideoUnavailableError):
            asyncio.run(ytdlp_service.list_channel_videos(
                "https://www.youtube.com/@x", raise_errors=True))

    def test_raise_errors_on_timeout(self, fake, monkeypatch):
        monkeypatch.setattr(ytdlp_service, "LISTING_TIMEOUT_S", 0.2)
        release = threading.Event()
        fake.result = lambda: (release.wait(5), PLAYLIST)[1]

        async def go():
            try:
                return await ytdlp_service.list_channel_videos(
                    "https://www.youtube.com/@x", raise_errors=True)
            finally:
                release.set()
                await download_pool.wait_until_occupied(0, timeout=5)
        with pytest.raises(DownloadError, match="timed out"):
            asyncio.run(go())

    def test_listing_carries_a_socket_timeout_and_no_item_cap(self, fake):
        fake.result = PLAYLIST
        asyncio.run(ytdlp_service.list_channel_videos("https://www.youtube.com/@x/videos"))
        assert fake.seen[0]["socket_timeout"] == 30
        assert fake.seen[0]["playlistend"] == 5
        assert "playlist_items" not in fake.seen[0]

    def test_a_channel_root_is_listed_from_its_videos_tab(self, fake, monkeypatch):
        seen_urls = []
        orig = _Fake.extract_info

        def _capture(self, url, download=False):
            seen_urls.append(url)
            return orig(self, url, download)
        monkeypatch.setattr(_Fake, "extract_info", _capture)
        fake.result = PLAYLIST
        asyncio.run(ytdlp_service.list_channel_videos("https://www.youtube.com/@CNN"))
        assert seen_urls == ["https://www.youtube.com/@CNN/videos"]


# ── the runners that read them ──────────────────────────────────────────────

def test_channel_download_reports_the_listing_error():
    from backend.core import task_runner
    with patch("backend.services.ytdlp_service.list_channel_videos",
               new=AsyncMock(side_effect=VideoUnavailableError("Video unavailable: private"))), \
         patch("backend.core.ws_manager.ws_manager.send_progress", new=AsyncMock()):
        with pytest.raises(VideoUnavailableError, match="private"):
            asyncio.run(task_runner._download_channel("job", "https://www.youtube.com/@x", "local"))


def test_an_empty_listing_is_a_download_error():
    from backend.core import task_runner
    with patch("backend.services.ytdlp_service.list_channel_videos", new=AsyncMock(return_value=[])), \
         patch("backend.core.ws_manager.ws_manager.send_progress", new=AsyncMock()):
        with pytest.raises(DownloadError, match="No videos found"):
            asyncio.run(task_runner._download_channel("job", "https://www.youtube.com/@x", "local"))


def test_the_channel_analysis_lists_only_what_it_reads(monkeypatch):
    from backend.core import task_runner
    seen = {}

    async def _info(url, flat=False, max_entries=None):
        seen.update(url=url, flat=flat, max_entries=max_entries)
        return {"channel": "CNN", "entries": [{"id": f"v{i}", "title": "t"} for i in range(40)]}
    monkeypatch.setattr(ytdlp_service, "get_video_info", _info)
    summary = asyncio.run(task_runner._analyze_youtube_channel("https://www.youtube.com/@CNN"))
    assert seen == {"url": "https://www.youtube.com/@CNN/videos", "flat": True,
                    "max_entries": task_runner.ANALYSIS_VIDEOS}
    assert task_runner.ANALYSIS_VIDEOS == 30
    assert len(summary["videos"]) == 30 and summary["total_videos_listed"] == 30


def test_a_rate_limited_channel_batch_counts_every_skipped_video(monkeypatch):
    """One rate limit skips every video after it; the summary used to count
    only the one that hit it ("Failed 1/3" with two never tried)."""
    from backend.core import task_runner
    from backend.core.exceptions import RateLimitError
    import backend.agents.analyzer as analyzer
    import backend.agents.job_helper as jh
    import backend.core.http_utils as hu

    writes = []
    outcomes = iter([{"id": "a"}, RateLimitError("429"), {"id": "never"}])

    async def _status(job_id, status, **kw):
        writes.append((status, kw))

    async def _not_cancelled(job_id):
        return False

    async def _single(*a, **kw):
        o = next(outcomes)
        if isinstance(o, Exception):
            raise o
        return o

    class _NoAnalysis:
        async def run(self, **kw):
            return None

    monkeypatch.setattr(jh, "update_job_status", _status)
    monkeypatch.setattr(jh, "job_cancelled", _not_cancelled)
    monkeypatch.setattr(hu, "jittered_delay", lambda: 0)
    monkeypatch.setattr(analyzer, "AnalyzerAgent", _NoAnalysis)
    monkeypatch.setattr(task_runner, "_download_single_video_to_db", _single)
    monkeypatch.setattr("backend.services.ytdlp_service.list_channel_videos",
                        AsyncMock(return_value=[{"url": f"https://y/{i}", "title": f"v{i}"} for i in range(3)]))
    with patch("backend.core.ws_manager.ws_manager.send_progress", new=AsyncMock()), \
         patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()), \
         patch("backend.core.ws_manager.ws_manager.send_constraint_warning", new=AsyncMock()):
        asyncio.run(task_runner._download_channel("job", "https://www.youtube.com/@x", "local"))
    status, kw = writes[-1]
    assert status == "success"
    assert kw["error_message"].startswith("Failed 2/3")
    assert "Skipped (rate-limited)" in kw["error_message"]
