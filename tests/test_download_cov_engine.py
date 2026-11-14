# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Engine behaviour the old `ytdlp_service` coverage suites pinned through
seams that are gone, re-expressed through `engine.run` with the scripted
yt-dlp (tests/download_fakes.py).

Two groups:
  * what the old anti-bot recogniser and terminal-failure log line
    guaranteed;
  * the promises callers save a Library row on: a success names a real
    file, every failure sweeps its partial files, and a sweep never touches a
    file that belongs to another download or to the Library.
"""
import asyncio
import logging
import threading
from pathlib import Path

import pytest
import yt_dlp

from backend.core.exceptions import (
    DownloadError, DownloadStalledError, DownloadTimeoutError, ExtractorMissingError,
    VideoUnavailableError,
)
from backend.download import engine, outputs, progress
from tests import download_fakes
from tests.download_fakes import VIDEO_BYTES

ytdlp = download_fakes.ytdlp        # the scripted yt-dlp fixture, re-exported for this module

URL = "https://www.youtube.com/watch?v=abc"
NET = "ERROR: [youtube] abc: Unable to download webpage: [Errno 104] Connection reset by peer"
PRIVATE = "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video"

pytestmark = pytest.mark.asyncio


def video(tmp_path, **kw):
    base = dict(url=URL, output_dir=tmp_path, stem="vid1", extract_audio=False)
    base.update(kw)
    return engine.Request(**base)



def leftovers(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if p.is_file())


# ── Anti-bot walls end at "a real browser might" ─────────────────────────────

@pytest.mark.parametrize("msg", [
    "ERROR: Cloudflare protection detected",
    "ERROR: Turnstile challenge issued",
    "ERROR: PerimeterX rejected the request",
    "ERROR: HTTP Error 412: Precondition Failed",
    "ERROR: HTTP Error 403: Forbidden",
    "ERROR: HTTP Error 404: Not Found",
    "ERROR: Akamai bot manager flagged this client",
])
async def test_an_anti_bot_wall_ends_as_extractor_missing(ytdlp, tmp_path, msg):
    """Was `_is_anti_bot_challenge(msg)`: each of these ends the ladder as
    `ExtractorMissingError` (no way into this page), never "unavailable"."""
    ytdlp.script = [msg]
    with pytest.raises(ExtractorMissingError):
        await engine.run(video(tmp_path, url="https://videos.example.com/watch/1"))
    assert leftovers(tmp_path) == []


# ── The one log line per terminal failure ────────────────────────────────────

async def test_a_terminal_failure_logs_one_structured_line(ytdlp, tmp_path, caplog):
    """Was `_log_terminal_failure` (`[YTDLP_TERMINAL_FAILURE]`): the marker
    the canary and log reads correlate failures by is now `[DOWNLOAD_FAILED]`."""
    ytdlp.script = [PRIVATE]
    with caplog.at_level(logging.ERROR, logger="backend.download.engine"):
        with pytest.raises(VideoUnavailableError):
            await engine.run(video(tmp_path))
    lines = [r.getMessage() for r in caplog.records if "[DOWNLOAD_FAILED]" in r.getMessage()]
    assert len(lines) == 1
    assert "host=www.youtube.com" in lines[0] and "yt_dlp=" in lines[0]


# ── A success names a real file ──────────────────────────────────────────────

async def test_a_video_success_names_a_file_that_exists_and_passes_validation(ytdlp, tmp_path):
    result = await engine.run(video(tmp_path))
    path = Path(result["video_path"])
    assert path.exists() and path.stat().st_size == VIDEO_BYTES
    outputs.validate_output(path)                           # raises when it isn't a usable video
    assert path.exists()                                    # and validation left it in place



async def test_a_video_download_that_wrote_nothing_is_never_a_success(ytdlp, tmp_path):
    ytdlp.on_ok = lambda opts: None
    with pytest.raises(DownloadError):
        await engine.run(video(tmp_path))
    assert leftovers(tmp_path) == []


# ── Every failure sweeps its partial files, and only its own ─────────────────

PARTIALS = ("{s}.mp4.part", "{s}.f398.mp4", "{s}.f251.webm.part", "{s}.temp.mp4")
# Files a sweep of stem "vid1" / "aud1" must never touch: finished downloads in
# the Library (same folder), another download's finished file and its
# in-flight streams, and a note the person keeps next to them.
NEIGHBOURS = ("other.mp4", "other.f398.mp4", "other.mp4.part", "xvid1.mp4.part", "vid1_notes.txt")


def _seed(tmp_path, stem):
    for name in PARTIALS:
        (tmp_path / name.format(s=stem)).write_bytes(b"partial")
    for name in NEIGHBOURS:
        (tmp_path / name).write_bytes(b"keep")


@pytest.mark.parametrize("error", [NET, PRIVATE, "ERROR: HTTP Error 403: Forbidden",
                                   "ERROR: [youtube] abc: Requested format is not available"])
async def test_a_failed_video_download_sweeps_its_partials_and_nothing_else(ytdlp, tmp_path, error):
    _seed(tmp_path, "vid1")
    ytdlp.script = [error]
    with pytest.raises(DownloadError):
        await engine.run(video(tmp_path))
    assert leftovers(tmp_path) == sorted(NEIGHBOURS)



async def test_a_cancelled_download_sweeps_its_partials_and_nothing_else(ytdlp, tmp_path):
    _seed(tmp_path, "vid1")
    ytdlp.script = [NET]
    ytdlp.cancel_after_attempt = 1
    with pytest.raises(DownloadError):
        await engine.run(video(tmp_path, job_id="job-1"))
    assert leftovers(tmp_path) == sorted(NEIGHBOURS)


async def test_a_failed_redownload_never_costs_the_library_file_of_the_same_stem(ytdlp, tmp_path):
    """The same stem downloaded again (a retry of the same job) and failing:
    the file the Library row points at stays, byte for byte."""
    library_file = tmp_path / "vid1.mp4"
    library_file.write_bytes(b"L" * VIDEO_BYTES)
    (tmp_path / "vid1.mp4.part").write_bytes(b"partial")
    ytdlp.script = [NET]
    with pytest.raises(DownloadError):
        await engine.run(video(tmp_path))
    assert leftovers(tmp_path) == ["vid1.mp4"]
    assert library_file.read_bytes() == b"L" * VIDEO_BYTES


async def test_a_successful_download_leaves_only_its_file(ytdlp, tmp_path):
    _seed(tmp_path, "vid1")
    result = await engine.run(video(tmp_path))
    assert leftovers(tmp_path) == sorted([*NEIGHBOURS, "vid1.mp4"])
    assert result["video_path"] == str(tmp_path / "vid1.mp4")


async def test_an_abandoned_download_still_sweeps_its_partials_when_its_thread_ends(ytdlp, tmp_path, monkeypatch):
    """A stall abandons the thread, it does not stop it. When yt-dlp finally
    returns in that thread (here: with an error), the thread's own exit sweeps
    the stem, so nothing is left for a later download to trip over."""
    release = threading.Event()

    def hang_then_fail(opts):
        Path(opts["outtmpl"].replace(".%(ext)s", ".mp4.part")).write_bytes(b"partial")
        release.wait(10)
        raise yt_dlp.utils.DownloadError(PRIVATE)
    ytdlp.on_ok = hang_then_fail
    (tmp_path / "other.mp4").write_bytes(b"keep")
    monkeypatch.setattr(engine.settings, "DOWNLOAD_STALL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(progress, "_STALL_POLL_S", 0.05)
    try:
        with pytest.raises(DownloadStalledError):
            await engine.run(video(tmp_path))
        assert (tmp_path / "vid1.mp4.part").exists()         # the thread is still inside yt-dlp
    finally:
        release.set()
    for _ in range(40):
        if leftovers(tmp_path) == ["other.mp4"]:
            break
        await asyncio.sleep(0.05)
    assert leftovers(tmp_path) == ["other.mp4"]
    assert len(ytdlp.calls) == 1
