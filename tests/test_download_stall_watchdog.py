# ruff: noqa: F811  (pytest fixtures are imported, then named as parameters)
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A download that moves zero bytes must fail with an error, not sit forever.

Customer report, 2026-09-20: two YouTube download jobs sat at
`current_step: "Downloading 0MB..."`, `progress_pct: 0` for over an hour
each, and `error_message` was null throughout. Meanwhile plain yt-dlp on
the same machine, same URL, same minutes pulled 36.5 MB in 31s.

Why it could happen: nothing measured *progress*. The only bound was a
90-minute wall clock (`asyncio.wait_for(..., timeout=5400)`), and the
configured retry budget can legitimately fill it having moved no bytes —
`socket_timeout=60, retries=10` with `min(60, 2**n)` backoff is ~16 min
per format rung, `FORMAT_FALLBACK_CHAIN` has three rungs, and the AI-retry
pass gets a fresh 5400s of its own. `throttledratelimit` only catches a
download that IS trickling; it never fires on zero.

These tests pin the watchdog that turns "0 MB forever" into a fast,
honest failure — and, just as importantly, pin the cases where it must
STAY QUIET, because killing a legitimate 90-minute download at the merge
step would be a worse bug than the one being fixed.
"""
import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.exceptions import DownloadError, DownloadStalledError, DownloadTimeoutError
from backend.download import engine, fetch, progress
from backend.download.progress import DownloadProgress as _DownloadProgress, await_with_stall_guard as _await_with_stall_guard
from tests.download_fakes import VIDEO_BYTES, ytdlp  # noqa: F401  (fixture)

URL = "https://youtube.com/watch?v=kN3C6b2zCTU"      # the customer's link


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
    """THE fix for the customer report.

    The hard cap here is 90 minutes, as in production. The watchdog must
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
    # The message IS the error_message the customer reads on the job row.
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


@pytest.mark.asyncio
async def test_pool_exhaustion_is_reported_as_a_download_error():
    """`PoolExhaustedError` is a RuntimeError, outside the classification cascade.

    Unmapped, it lands verbatim in `Job.error_message` — raw internals
    instead of a download error the UI knows how to phrase.
    """
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


# ── the hooks that feed the tracker ─────────────────────────────────────

@pytest.mark.asyncio
async def test_progress_hook_feeds_the_tracker_without_a_job_id():
    """Stall detection must not depend on there being a job to report to.

    The hook used to bail out immediately when `job_id` was None — that is
    the UI-reporting concern. A download with no job attached can stall
    just as easily, so tracking has to happen first, unconditionally.
    """
    tracker = _DownloadProgress()
    hook = progress.make_progress_hook(
        tracker=tracker, job_id=None, user_id="local", loop=asyncio.get_running_loop(),
    )
    tracker._last_activity = time.monotonic() - 60
    hook({"status": "downloading", "downloaded_bytes": 4096, "total_bytes": 8192})
    assert tracker.idle_seconds() < 1
    assert tracker.bytes_downloaded == 4096


@pytest.mark.asyncio
async def test_postprocessor_hook_toggles_the_suspension():
    tracker = _DownloadProgress()
    pp = progress.make_postprocessor_hook(tracker)
    pp({"status": "started", "postprocessor": "Merger"})
    assert tracker.idle_seconds() is None
    pp({"status": "finished", "postprocessor": "Merger"})
    assert tracker.idle_seconds() is not None


@pytest.mark.asyncio
async def test_progress_hook_never_raises_on_a_malformed_event():
    """A UI/bookkeeping problem must never break a download in flight."""
    tracker = _DownloadProgress()
    hook = progress.make_progress_hook(
        tracker=tracker, job_id=None, user_id="local", loop=asyncio.get_running_loop(),
    )
    hook({})
    hook({"status": "downloading", "downloaded_bytes": None, "total_bytes": None})
    hook({"status": "finished"})


# ── the watchdog, through the real engine ───────────────────────────────
#
# The scripted yt-dlp runs on the real download pool; `ytdlp.on_ok` is what
# the "transfer" does. The stall budget is 0.3 s and each scenario below
# lasts ~0.9 s, three budgets, so a watchdog that misreads it fires.

STALL_S = 0.3
SCENARIO_S = 0.9


def video(tmp_path, **kw):
    base = dict(url=URL, output_dir=tmp_path, stem="vid1", extract_audio=False)
    base.update(kw)
    return engine.Request(**base)



def finish(opts):
    """Write the file a finished transfer leaves (video, or the audio format asked for)."""
    pps = opts.get("postprocessors") or []
    extract = next((pp for pp in pps if pp.get("key") == "FFmpegExtractAudio"), None)
    ext = extract["preferredcodec"] if extract else "mp4"
    Path(opts["outtmpl"].replace(".%(ext)s", f".{ext}")).write_bytes(b"x" * VIDEO_BYTES)


@pytest.fixture
def fast_watchdog(monkeypatch):
    monkeypatch.setattr(engine.settings, "DOWNLOAD_STALL_TIMEOUT_S", STALL_S)
    monkeypatch.setattr(progress, "_STALL_POLL_S", 0.05)
    monkeypatch.setattr(progress, "_DISK_POLL_S", 0.05)


@pytest.fixture
def wedge(ytdlp):  # noqa: F811
    """A transfer that accepts the connection and never moves a byte, like a
    yt-dlp thread stuck in its retry budget with the socket open. Released
    (and given time to finish) when the test ends."""
    release = threading.Event()

    def hang(opts):
        release.wait(10)
        finish(opts)
    ytdlp.on_ok = hang
    yield release
    release.set()
    time.sleep(0.3)                    # let the abandoned thread end before the next test


@pytest.mark.asyncio
async def test_2026_09_20_a_transfer_that_moves_nothing_fails_as_stalled_in_seconds(ytdlp, tmp_path, fast_watchdog, wedge):
    """The customer's job: the link was fine, then no byte arrived. It sat at
    "Downloading 0MB…" for 90 minutes with no error. The wall clock is
    untouched here; the stall budget is what must end it."""
    assert progress.VIDEO_HARD_TIMEOUT_S == 1200
    t0 = time.monotonic()
    with pytest.raises(DownloadStalledError) as err:
        await engine.run(video(tmp_path))
    assert time.monotonic() - t0 < 5
    assert "0.0 MB" in str(err.value) and "stall" in str(err.value).lower()
    assert len(ytdlp.calls) == 1                       # terminal: nothing retried into the stem



@pytest.mark.asyncio
async def test_both_hooks_are_attached_whether_or_not_there_is_a_job(ytdlp, tmp_path):
    make = video
    """The audio hook used to be registered only `if job_id`, which left every
    job-less audio pull with no stall protection; and without the
    postprocessor hook the watchdog would kill a long podcast mid-transcode."""
    await engine.run(make(tmp_path))
    assert len(ytdlp.calls[0]["progress_hooks"]) == 1
    assert len(ytdlp.calls[0]["postprocessor_hooks"]) == 1


@pytest.mark.asyncio
async def test_a_silent_merge_or_transcode_longer_than_the_stall_budget_is_not_a_stall(ytdlp, tmp_path, fast_watchdog):
    make = video
    """ffmpeg reports no download progress for minutes. Killing a finished
    download during its merge destroys work the person already waited for."""
    def downloads_then_postprocesses(opts):
        opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 50_000_000})
        opts["postprocessor_hooks"][0]({"status": "started", "postprocessor": "Merger"})
        time.sleep(SCENARIO_S)                         # silent ffmpeg
        finish(opts)
        opts["postprocessor_hooks"][0]({"status": "finished", "postprocessor": "Merger"})
    ytdlp.on_ok = downloads_then_postprocesses
    result = await engine.run(make(tmp_path))
    assert Path(result.get("video_path") or result["audio_path"]).exists()


@pytest.mark.asyncio
async def test_a_second_file_restarting_at_zero_is_progress_through_the_real_hook(ytdlp, tmp_path, fast_watchdog):
    """`downloaded_bytes` is per file: the video stream ends at 1.5 GB and the
    audio stream starts over near 0. A watchdog that wanted the count to GROW
    froze for the whole second file and killed a healthy download."""
    def video_then_audio_stream(opts):
        hook = opts["progress_hooks"][0]
        hook({"status": "downloading", "downloaded_bytes": 1_500_000_000})
        for n in range(1, 10):                         # every count is BELOW the first file's
            time.sleep(SCENARIO_S / 9)
            hook({"status": "downloading", "downloaded_bytes": n * 64_000})
        finish(opts)
    ytdlp.on_ok = video_then_audio_stream
    result = await engine.run(video(tmp_path))
    assert Path(result["video_path"]).exists()


@pytest.mark.asyncio
async def test_a_download_that_keeps_reporting_the_same_byte_count_is_stalled(ytdlp, tmp_path, fast_watchdog):
    """yt-dlp fires `downloading` events while retrying a fragment without
    moving a byte. If those counted, a wedged download would look healthy
    forever."""
    release = threading.Event()

    def retries_without_moving(opts):
        hook = opts["progress_hooks"][0]
        while not release.is_set():
            hook({"status": "downloading", "downloaded_bytes": 2048})
            time.sleep(0.02)
        finish(opts)
    ytdlp.on_ok = retries_without_moving
    try:
        with pytest.raises(DownloadStalledError):
            await asyncio.wait_for(engine.run(video(tmp_path)), timeout=8)
    finally:
        release.set()
        await asyncio.sleep(0.3)


@pytest.mark.asyncio
async def test_files_growing_on_disk_count_as_progress_when_no_hook_fires(ytdlp, tmp_path, fast_watchdog):
    """yt-dlp's ffmpeg downloader (HLS, protocol m3u8) emits one hook, at the
    very end. A healthy 20-minute transfer looked idle for all 20 minutes;
    the bytes landing under the attempt's stem are the ground truth."""
    def ffmpeg_writes_a_part_file(opts):
        part = Path(opts["outtmpl"].replace(".%(ext)s", ".mp4.part"))
        with part.open("wb") as f:
            for _ in range(18):
                f.write(b"x" * 20_000)
                f.flush()
                time.sleep(SCENARIO_S / 18)
        part.unlink()
        finish(opts)
    ytdlp.on_ok = ffmpeg_writes_a_part_file
    result = await engine.run(video(tmp_path))
    assert Path(result["video_path"]).exists()


@pytest.mark.asyncio
async def test_a_file_that_is_there_but_not_growing_is_still_a_stall(ytdlp, tmp_path, fast_watchdog, wedge):
    (tmp_path / "vid1.mp4.part").write_bytes(b"x" * 4096)
    with pytest.raises(DownloadStalledError):
        await engine.run(video(tmp_path))


@pytest.mark.asyncio
async def test_time_spent_queued_for_a_worker_is_not_charged_to_the_stall_budget(ytdlp, tmp_path, fast_watchdog, monkeypatch):
    """A download waiting for a free worker cannot move a byte. Charging that
    wait to the stall budget failed queued downloads as "stalled" before
    yt-dlp ever ran. The clock starts when the pool admits the work."""
    real_run = engine.download_pool.run
    admitted = []

    async def busy_pool(fn, /, *args, on_admit=None, **kwargs):
        await asyncio.sleep(SCENARIO_S)                # every worker is busy for three stall budgets
        admitted.append(time.monotonic())
        return await real_run(fn, *args, on_admit=on_admit, **kwargs)
    monkeypatch.setattr(engine.download_pool, "run", busy_pool)
    result = await engine.run(video(tmp_path))
    assert Path(result["video_path"]).exists() and len(admitted) == 1


@pytest.mark.asyncio
async def test_once_admitted_a_queued_download_is_watched_like_any_other(ytdlp, tmp_path, fast_watchdog, wedge, monkeypatch):
    real_run = engine.download_pool.run

    async def busy_pool(fn, /, *args, on_admit=None, **kwargs):
        await asyncio.sleep(SCENARIO_S)
        return await real_run(fn, *args, on_admit=on_admit, **kwargs)
    monkeypatch.setattr(engine.download_pool, "run", busy_pool)
    t0 = time.monotonic()
    with pytest.raises(DownloadStalledError):
        await engine.run(video(tmp_path))
    assert time.monotonic() - t0 >= SCENARIO_S + STALL_S * 0.9     # the budget ran from admission


# ── an abandoned attempt is terminal, through the whole fetch path ──────

class _World:
    def __init__(self):
        self.fix_calls: list[str] = []


@pytest.fixture
def world(monkeypatch, ytdlp, tmp_path):  # noqa: F811
    """Around the real engine: a link fixer that would gladly have another go."""
    w = _World()

    async def fake_fix(url, error, user_settings=None):
        w.fix_calls.append(url)
        return "https://other.example/the-same-video"

    async def quiet(*a, **kw):
        return None

    async def not_cancelled(job_id):
        return False

    class _NoDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **kw):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    monkeypatch.setattr("backend.core.ai_retry.ai_fix_url", fake_fix)
    monkeypatch.setattr("backend.core.ws_manager.ws_manager.send_constraint_warning", quiet)
    monkeypatch.setattr("backend.core.ws_manager.ws_manager.send", quiet)
    monkeypatch.setattr("backend.download.progress.job_is_cancelled", not_cancelled)
    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _NoDB())
    return w


@pytest.mark.asyncio
async def test_an_abandoned_attempt_is_terminal_and_never_retried(world, ytdlp, tmp_path, fast_watchdog, wedge):
    """A stall must NOT fall through to the link fixer.

    Not because a retry could never help, but because we no longer own the
    workspace: the abandoned thread is still inside yt-dlp, and on its way
    out it sweeps `{stem}*.part` / `{stem}.f*.*` / `{stem}.temp.*`. A retry
    writes those exact paths, so it would have its fragments deleted out
    from under it, after a period of both attempts writing the same files.
    """
    with pytest.raises(DownloadStalledError):
        await fetch.fetch_video(URL, output_dir=tmp_path, filename="probe", extract_audio=False)
    assert len(ytdlp.probe_calls) == 1 and len(ytdlp.calls) == 1
    assert world.fix_calls == []


@pytest.mark.asyncio
async def test_the_abandoned_thread_sweeps_its_own_stem_when_it_finally_ends(ytdlp, tmp_path, fast_watchdog):
    """Why nothing may retry into the stem: this sweep runs whenever the
    abandoned thread returns, minutes after the job already failed."""
    release = threading.Event()

    def wedged_then_fails(opts):
        release.wait(10)
        import yt_dlp
        raise yt_dlp.utils.DownloadError("ERROR: [youtube] x: Private video. Sign in if you've been granted access")
    ytdlp.on_ok = wedged_then_fails
    with pytest.raises(DownloadStalledError):
        await engine.run(video(tmp_path))
    # What a retry into the same stem would have been writing by now:
    for name in ("vid1.mp4.part", "vid1.f251.webm", "vid1.temp.mp4"):
        (tmp_path / name).write_bytes(b"retry")
    release.set()
    def files():
        return sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if not files():
            break
    assert files() == []
    assert len(ytdlp.calls) == 1


@pytest.mark.asyncio
async def test_a_wall_clock_timeout_is_also_terminal(world, ytdlp, tmp_path, wedge, monkeypatch):
    """Same reasoning as the stall: the thread outlived our await."""
    monkeypatch.setattr(engine.settings, "DOWNLOAD_STALL_TIMEOUT_S", 99999)
    monkeypatch.setattr(progress, "_STALL_POLL_S", 0.05)
    ticks = iter([0.0] + [1e9] * 1000)
    monkeypatch.setattr(progress, "_now", lambda: next(ticks))

    with pytest.raises(DownloadTimeoutError, match="20 minutes"):
        await fetch.fetch_video("https://example.com/v", output_dir=tmp_path, filename="probe", extract_audio=False)
    assert len(ytdlp.calls) == 1, "a wall-clock timeout must not start a second attempt"
    assert world.fix_calls == []
