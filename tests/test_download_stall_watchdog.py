# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A download that moves zero bytes must fail with an error, not sit forever.

Reported against the hosted variant: two YouTube download jobs sat at
"Downloading…" with no progress for over an hour each, and the job's error
stayed empty throughout, while plain yt-dlp on the same machine pulled the
same URL in seconds.

Why it could happen: nothing measured *progress*. The only bound was a wall
clock (`asyncio.wait_for(..., timeout=1200)` here), and yt-dlp's retry budget
— socket timeouts and retries with exponential backoff, times the format
fallback rungs — can fill it having moved no bytes. Worse, the timeout could
not stop the thread, which kept holding a worker of the executor every
`asyncio.to_thread` call shares.

These tests pin the watchdog that turns "0 MB forever" into a fast, honest
failure — and, just as importantly, pin the cases where it must STAY QUIET,
because killing a legitimate long download at the merge step would be a
worse bug than the one being fixed.
"""
import asyncio
import time

import pytest

from backend.core.exceptions import DownloadError, DownloadStalledError
from backend.services.ytdlp_service import (
    _DownloadProgress,
    _await_with_stall_guard,
)


# ── the progress tracker ────────────────────────────────────────────────

def test_idle_grows_while_no_bytes_arrive():
    p = _DownloadProgress()
    p._last_activity = time.monotonic() - 42
    assert p.idle_seconds() == pytest.approx(42, abs=2)


def test_bytes_arriving_resets_idle():
    p = _DownloadProgress()
    p._last_activity = time.monotonic() - 42
    p.note_bytes(1024)
    assert p.idle_seconds() < 1


def test_repeated_identical_byte_count_is_not_progress():
    """yt-dlp re-reports the same total on retries — that is not movement."""
    p = _DownloadProgress()
    p.note_bytes(2048)
    p._last_activity = time.monotonic() - 42
    p.note_bytes(2048)  # same count again
    assert p.idle_seconds() == pytest.approx(42, abs=2), (
        "a repeated byte count must not count as progress, or a stuck "
        "download that keeps re-reporting its position looks healthy"
    )


def test_a_new_file_restarting_at_zero_is_progress_not_a_stall():
    """`downloaded_bytes` is PER FILE, and routinely restarts inside one attempt.

    Two ordinary cases do it: a DASH pull finishes the video stream and
    begins the audio stream from 0, and every rung of FORMAT_FALLBACK_CHAIN
    restarts from 0 in the same `_download` call. Requiring the count to
    GROW froze the activity clock for the whole of that second file, so a
    download transferring at full speed was killed as "stalled".
    """
    p = _DownloadProgress()
    p.note_bytes(1_500_000_000)          # video stream done
    p._last_activity = time.monotonic() - 42
    p.note_bytes(64_000)                 # audio stream starts over at ~0
    assert p.idle_seconds() < 1, (
        "a byte count going DOWN means a new file started, not a stall"
    )


def test_peak_bytes_are_still_what_the_error_reports():
    """Detection uses change; the message should quote the high-water mark."""
    p = _DownloadProgress()
    p.note_bytes(5_000_000)
    p.note_bytes(1_000)                  # new file
    assert p.bytes_downloaded == 5_000_000


def test_a_downloading_event_unsticks_postprocessing_without_faking_activity():
    """Two failure modes guarded at once.

    A postprocessor that STARTS and then errors (a common reason to fall to
    the next format rung) would otherwise pin the suspension on and leave
    every later rung unwatched. But un-sticking it must NOT bump the clock:
    yt-dlp fires `downloading` events during retries without moving a byte,
    so refreshing activity there would make the watchdog unable to ever fire.
    """
    p = _DownloadProgress()
    p.enter_postprocessing()
    p._last_activity = time.monotonic() - 42

    p.clear_postprocessing()
    idle = p.idle_seconds()
    assert idle is not None, "suspension must be cleared"
    assert idle == pytest.approx(42, abs=2), (
        "clearing the flag must not count as activity, or a retrying "
        "download looks healthy forever"
    )


def test_postprocessing_suspends_the_watchdog():
    """An ffmpeg merge reports no download progress for minutes.

    Killing a finished 4K download during its merge would destroy work the
    user already paid for and waited for. While a postprocessor is running
    the stall watchdog stands down; only the hard wall clock applies.
    """
    p = _DownloadProgress()
    p._last_activity = time.monotonic() - 3600
    p.enter_postprocessing()
    assert p.idle_seconds() is None, "watchdog must be suspended during merge"
    p.exit_postprocessing()
    assert p.idle_seconds() is not None


def test_reset_clears_state_between_attempts():
    """The AI-retry pass is a fresh attempt and gets a fresh budget."""
    p = _DownloadProgress()
    p.note_bytes(5000)
    p.enter_postprocessing()
    p._last_activity = time.monotonic() - 999
    p.reset()
    assert p.idle_seconds() < 1
    assert p.bytes_downloaded == 0


# ── the guard ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guard_returns_the_result_when_work_completes():
    async def work():
        await asyncio.sleep(0.05)
        return {"video_path": "/tmp/x.mp4"}

    p = _DownloadProgress()
    out = await _await_with_stall_guard(
        work(), p, hard_timeout=30, stall_timeout=30, url="https://example.com/v",
    )
    assert out == {"video_path": "/tmp/x.mp4"}


@pytest.mark.asyncio
async def test_guard_raises_stalled_long_before_the_hard_timeout():
    """THE fix for the reported "0 MB forever" download.

    The hard cap here is 90 minutes (production is 20). The watchdog must
    fire on the stall threshold instead — in seconds, not an hour and a half.
    """
    async def never_finishes():
        await asyncio.sleep(3600)

    p = _DownloadProgress()
    p._last_activity = time.monotonic() - 10  # already idle 10s

    t0 = time.monotonic()
    with pytest.raises(DownloadStalledError) as exc:
        await _await_with_stall_guard(
            never_finishes(), p, hard_timeout=5400, stall_timeout=10,
            url="https://youtube.com/watch?v=kN3C6b2zCTU",
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 5, f"watchdog took {elapsed:.1f}s to notice a stall"

    msg = str(exc.value)
    # The message IS the error_message the user reads on the job row.
    # It has to say what happened and how much arrived, not just "failed".
    assert "0.0 MB" in msg or "0 MB" in msg, msg
    assert "stall" in msg.lower(), msg


@pytest.mark.asyncio
async def test_stalled_is_a_downloaderror_so_the_existing_cascade_still_routes_it():
    """`download_video`'s handlers branch on DownloadError. Stay inside it."""
    assert issubclass(DownloadStalledError, DownloadError)


@pytest.mark.asyncio
async def test_guard_stays_quiet_while_bytes_keep_arriving():
    """A slow-but-moving download must not be killed."""
    p = _DownloadProgress()
    got = 0

    async def trickles():
        nonlocal got
        for i in range(1, 9):
            await asyncio.sleep(0.05)
            got = i * 1024
            p.note_bytes(got)
        return "finished"

    out = await _await_with_stall_guard(
        trickles(), p, hard_timeout=30, stall_timeout=0.2,
        url="https://example.com/slow",
    )
    assert out == "finished"
    assert got == 8 * 1024


@pytest.mark.asyncio
async def test_guard_still_enforces_the_hard_wall_clock():
    """Activity must not let a download run forever."""
    p = _DownloadProgress()

    async def busy_forever():
        n = 0
        while True:
            await asyncio.sleep(0.02)
            n += 1024
            p.note_bytes(n)

    with pytest.raises(DownloadError) as exc:
        await _await_with_stall_guard(
            busy_forever(), p, hard_timeout=0.3, stall_timeout=30,
            url="https://example.com/endless",
        )
    assert not isinstance(exc.value, DownloadStalledError)
    assert "timed out" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_guard_does_not_fire_during_postprocessing():
    """The merge case, end to end through the guard."""
    p = _DownloadProgress()
    p.note_bytes(50_000_000)
    p.enter_postprocessing()
    p._last_activity = time.monotonic() - 600  # 10 min of silent ffmpeg

    async def merging():
        await asyncio.sleep(0.2)
        return "merged"

    assert await _await_with_stall_guard(
        merging(), p, hard_timeout=30, stall_timeout=1,
        url="https://example.com/big",
    ) == "merged"


# ── the hook that feeds the tracker ─────────────────────────────────────

@pytest.mark.asyncio
async def test_progress_hook_feeds_the_tracker():
    from backend.services import ytdlp_service

    tracker = _DownloadProgress()
    hook = ytdlp_service._make_progress_hook(tracker)
    tracker._last_activity = time.monotonic() - 60
    hook({"status": "downloading", "downloaded_bytes": 4096, "total_bytes": 8192})
    assert tracker.idle_seconds() < 1
    assert tracker.bytes_downloaded == 4096


@pytest.mark.asyncio
async def test_postprocessor_hook_toggles_the_suspension():
    from backend.services import ytdlp_service

    tracker = _DownloadProgress()
    pp = ytdlp_service._make_postprocessor_hook(tracker)
    pp({"status": "started", "postprocessor": "Merger"})
    assert tracker.idle_seconds() is None
    pp({"status": "finished", "postprocessor": "Merger"})
    assert tracker.idle_seconds() is not None


def _wedged_pool(monkeypatch, ytdlp_service, calls=None):
    """Replace the download pool with one whose transfer never returns —
    a yt-dlp thread stuck in its retry budget with the socket open."""
    async def fake_run(fn, /, *args, on_admit=None, **kwargs):
        if calls is not None:
            calls.append(fn.__name__)
        if on_admit is not None:
            on_admit()  # a worker picked it up — as the real pool reports
        await asyncio.sleep(3600)
    monkeypatch.setattr(ytdlp_service.download_pool, "run", fake_run)
    monkeypatch.setattr(ytdlp_service, "_check_cooldown", lambda *a, **kw: 0)


@pytest.mark.asyncio
async def test_download_video_gives_up_on_a_stalled_transfer(tmp_path, monkeypatch):
    """End to end: a wedged transfer fails `download_video` in bounded time."""
    from backend.services import ytdlp_service

    monkeypatch.setattr(ytdlp_service.settings, "DOWNLOAD_STALL_TIMEOUT_S", 1)
    _wedged_pool(monkeypatch, ytdlp_service)

    t0 = time.monotonic()
    with pytest.raises(DownloadStalledError):
        await ytdlp_service.download_video(
            url="https://youtube.com/watch?v=kN3C6b2zCTU",
            output_dir=tmp_path,
            filename="probe",
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 20, (
        f"download_video took {elapsed:.0f}s to report a stall with a 1s budget"
    )


@pytest.mark.asyncio
async def test_download_runs_on_the_download_pool_not_the_default_executor(tmp_path, monkeypatch):
    """The transfer must be handed to `download_pool` — on the shared default
    executor a wedged yt-dlp thread starves every `to_thread` in the app."""
    from backend.services import ytdlp_service

    monkeypatch.setattr(ytdlp_service.settings, "DOWNLOAD_STALL_TIMEOUT_S", 1)
    calls: list[str] = []
    _wedged_pool(monkeypatch, ytdlp_service, calls)

    with pytest.raises(DownloadStalledError):
        await ytdlp_service.download_video(
            url="https://example.com/v", output_dir=tmp_path, filename="probe",
        )
    assert calls == ["_download"]


@pytest.mark.asyncio
async def test_an_abandoned_attempt_is_terminal_and_never_retried(monkeypatch):
    """A stall must NOT fall through to the AI URL-fix retry.

    Not because a retry could never help, but because we no longer own the
    workspace: the abandoned thread is still inside `_download`, and its
    `finally` will later run `_cleanup_partial_files(output_dir, file_stem)`
    — deleting `{stem}*.part`. The retry writes into that same stem, so it
    would have its fragments deleted out from under it.
    """
    from backend.core import task_runner
    from backend.services import ytdlp_service
    import backend.core.ai_retry as ai_retry

    attempts = []

    async def stalled(**kw):
        attempts.append(kw["url"])
        raise DownloadStalledError("Download stalled: no data for 10 minutes")
    monkeypatch.setattr(ytdlp_service, "download_video", stalled)

    fixer_called = []

    async def _spy(*a, **kw):
        fixer_called.append(a)
        return "https://example.com/fixed"
    monkeypatch.setattr(ai_retry, "ai_fix_url", _spy)

    with pytest.raises(DownloadStalledError):
        await task_runner._download_single_video_to_db(
            "job-x", "https://example.com/v", "t", "local")

    assert not fixer_called and attempts == ["https://example.com/v"], (
        "the AI URL-fix retry ran after an abandoned attempt — it would race "
        "the still-running thread over the same part-files"
    )


@pytest.mark.asyncio
async def test_a_wall_clock_timeout_is_also_terminal(tmp_path, monkeypatch):
    """Same reasoning as the stall: the thread outlived our await."""
    from backend.core.exceptions import DownloadTimeoutError
    from backend.services import ytdlp_service

    monkeypatch.setattr(ytdlp_service.settings, "DOWNLOAD_STALL_TIMEOUT_S", 99999)
    ticks = iter([0.0] + [1e9] * 1000)
    monkeypatch.setattr(ytdlp_service, "_now", lambda: next(ticks))
    _wedged_pool(monkeypatch, ytdlp_service)

    with pytest.raises(DownloadTimeoutError, match="20 minutes"):
        await ytdlp_service.download_video(
            url="https://example.com/v", output_dir=tmp_path, filename="probe",
        )


@pytest.mark.asyncio
async def test_pool_exhaustion_is_reported_as_a_download_error():
    """`PoolExhaustedError` is a RuntimeError, outside the download error
    classes — unmapped, the raw internals would land verbatim in the job's
    error message instead of a download error the UI knows how to phrase."""
    from backend.core.executors import PoolExhaustedError

    async def boom():
        raise PoolExhaustedError("download pool is wedged: all 8 workers …")

    p = _DownloadProgress()
    with pytest.raises(DownloadError) as exc:
        await _await_with_stall_guard(
            boom(), p, hard_timeout=30, stall_timeout=30,
            url="https://example.com/v",
        )
    assert "wedged" in str(exc.value)




def test_cleanup_refuses_an_empty_stem(tmp_path):
    """An empty stem would glob `*.part` — every in-flight download's
    part-files in the directory. The final-stage cleanup now runs on EVERY
    exit, so this has to be refused outright."""
    from backend.services import ytdlp_service

    other = tmp_path / "someone-elses-download.mp4.part"
    other.write_bytes(b"x")
    ytdlp_service._cleanup_partial_files(tmp_path, "")
    assert other.exists()


@pytest.mark.asyncio
async def test_progress_hook_never_raises_on_a_malformed_event():
    """A bookkeeping problem must never break a download in flight."""
    from backend.services import ytdlp_service

    tracker = _DownloadProgress()
    hook = ytdlp_service._make_progress_hook(tracker)
    hook({})
    hook(None)
    hook({"status": "downloading", "downloaded_bytes": None, "total_bytes": None})
    hook({"status": "finished"})
