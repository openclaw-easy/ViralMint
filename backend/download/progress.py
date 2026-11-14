# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Is this download still moving, and has the person cancelled it?

yt-dlp runs on a worker thread Python cannot stop. Two things follow, and
both live here:

  * A bound on PROGRESS, not only on elapsed time. yt-dlp's own retry budget
    can burn 90 minutes having moved zero bytes (customer report 2026-09-20),
    so `await_with_stall_guard` fails a download that stops moving: no change
    in received bytes AND no change in the attempt's files on disk for
    `stall_timeout`, suspended while a postprocessor (an ffmpeg merge) runs.
  * A timed-out download is ABANDONED, not stopped. Its thread still owns the
    output stem, so a stall or wall-clock timeout is terminal: nothing may
    retry into the same stem.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import threading
import time
from pathlib import Path

from backend.core.exceptions import DownloadError, DownloadStalledError, DownloadTimeoutError
from backend.core.executors import PoolExhaustedError
from backend.download.util import host_of

logger = logging.getLogger(__name__)


# ── Stall detection ─────────────────────────────────────────────────────────
#
# The 90-minute `wait_for` around a download is a wall clock, not a progress
# check. Between them, `socket_timeout=60`, `retries=10` with `min(60, 2**n)`
# backoff, `fragment_retries=10`, and the three rungs of the format chain
# can consume that entire budget without a single byte arriving — which is
# exactly what a customer measured on 2026-09-20: two jobs at
# `"Downloading 0MB…"`, `progress_pct: 0`, for 90 minutes, `error_message`
# null, while plain yt-dlp pulled 1.15 MB/s from the same URL on the same
# machine in the same minutes.
#
# `throttledratelimit` (100 KB/s) does not help here: it re-extracts a
# download that is *trickling*, and never fires on one delivering nothing.
#
# So we measure progress ourselves. `DownloadProgress` is written by yt-dlp's
# hooks on the worker thread and read by `await_with_stall_guard` on the
# event loop, hence the lock.

# How often the guard wakes to compare notes with the tracker. Small enough
# to react promptly, large enough to be free next to a network transfer.
_STALL_POLL_S = 1.0

# How often the guard stats the attempt's files on disk. A glob of the videos
# directory is cheap but not free on a large Library, and the stall budget is
# measured in minutes, so a coarser cadence loses nothing.
_DISK_POLL_S = 10.0


def stem_footprint(*locations: tuple[Path, str | None]) -> int:
    """Total bytes of every file named `<stem>.*` or `<stem>_audio.*` in each (dir, stem) pair.

    Covers what a download attempt writes under its stem: `.part` files,
    HLS `.part-FragN` fragments, `.f<id>.<ext>` DASH streams, `.temp.*`
    merge output, the final file, and the `_audio.mp3` extracted after it.
    A stem that is still a yt-dlp template (`%(id)s`) or absent cannot be
    globbed and is skipped. Never raises — a stat racing a rename or a
    cleanup is simply not counted.
    """
    total = 0
    for directory, stem in locations:
        if not stem or "%" in stem:
            continue
        try:
            # "<stem>." and "<stem>_audio." only: another download whose stem
            # merely starts with ours must not count as our progress.
            files = [*Path(directory).glob(f"{glob.escape(stem)}.*"),
                     *Path(directory).glob(f"{glob.escape(stem)}_audio.*")]
            for f in files:
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        except OSError:
            pass
    return total

# Wall-clock cap, named so it is visible and patchable in a test rather than
# buried as a literal at the call site. The stall watchdog is what catches a
# download that stops moving; this only bounds one that keeps trickling.
VIDEO_HARD_TIMEOUT_S = 1200   # 20 min


def _now() -> float:
    """Monotonic clock for the stall guard's WALL-CLOCK budget.

    Indirected purely so a test can fast-forward the 90/30-minute caps
    without monkeypatching `time.monotonic` itself — that module is shared
    with asyncio's own scheduler, so patching it corrupts the event loop's
    timers and the test stops testing anything.
    """
    return time.monotonic()


class DownloadProgress:
    """Byte-level progress of one download attempt, shared thread↔loop.

    `idle_seconds()` returns None while a postprocessor is running. That is
    the single most important behaviour here: an ffmpeg merge of a long 4K
    file reports no download progress for many minutes, and killing a
    *finished* download during its merge would throw away exactly the work
    the user waited for. During postprocessing only the hard wall clock
    applies.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self, *, queued: bool = False) -> None:
        """Start a fresh attempt — the AI-retry pass gets its own budget.

        `queued=True` means the work has been handed to a pool but no worker
        has picked it up yet: the stall clock stays OFF until
        `mark_started()`. A download waiting for a free worker cannot move a
        byte, and charging that wait to the stall budget failed queued
        downloads as "stalled" before yt-dlp ever ran (the pool's own
        admission timeout bounds the wait instead).
        """
        with self._lock:
            self._bytes = 0
            self._peak = 0
            self._last_activity = time.monotonic()
            self._postprocessing = False
            self._started = not queued
            self._disk = None

    def mark_started(self) -> None:
        """A worker thread picked the attempt up — start the stall clock."""
        with self._lock:
            self._started = True
            self._last_activity = time.monotonic()

    @property
    def bytes_downloaded(self) -> int:
        """The most bytes seen in this attempt — for reporting, not detection."""
        with self._lock:
            return self._peak

    def note_bytes(self, downloaded: int) -> None:
        """Record a byte count from a progress event.

        Any CHANGE is progress; only a repeat of the exact same number is not.

        The distinction matters both ways, and getting it wrong is dangerous
        in both directions:

        * A repeated identical count is what a retrying fragment reports. If
          that counted as movement, a wedged download would look permanently
          healthy and the watchdog would never fire.
        * A count that goes DOWN is a NEW FILE starting, not a stall.
          `downloaded_bytes` is per-file, and two routine cases restart it at
          zero inside a single attempt: a DASH pull downloads the video
          stream and then begins the audio stream from 0, and each rung of
          the format chain restarts from 0 in the same `_download` call.
          Requiring the count to GROW therefore froze `_last_activity` for
          the whole of the second file — a healthy download transferring at
          full speed would be killed as "stalled" after the budget elapsed.

        `_peak` is tracked separately purely so the error message can report
        how much actually arrived.
        """
        if not isinstance(downloaded, int) or downloaded <= 0:
            return
        with self._lock:
            if downloaded != self._bytes:
                self._bytes = downloaded
                self._peak = max(self._peak, downloaded)
                self._last_activity = time.monotonic()

    def note_disk(self, footprint: int) -> None:
        """Record the attempt's on-disk footprint; any CHANGE is progress.

        The second, hook-independent progress signal. Not every stage of a
        download reports through progress hooks:

        * yt-dlp's `FFmpegFD` (non-native HLS, protocol `m3u8`) emits exactly
          one hook — `finished`, at the very end — so a healthy 20-minute
          ffmpeg-driven transfer looked idle for all 20 minutes.
        * the local audio extract and the bestaudio re-download that run
          after the video lands have no hooks at all.

        All of them write files under the attempt's stem, so bytes landing
        on disk are the ground truth. The first sample only sets the
        baseline — a file that exists but is not growing is not activity.
        """
        with self._lock:
            if self._disk is not None and footprint != self._disk:
                self._last_activity = time.monotonic()
            self._disk = footprint

    def note_event(self) -> None:
        """Record non-byte activity (a status transition, a finished file)."""
        with self._lock:
            self._last_activity = time.monotonic()

    def enter_postprocessing(self) -> None:
        with self._lock:
            self._postprocessing = True
            self._last_activity = time.monotonic()

    def exit_postprocessing(self) -> None:
        """A postprocessor finished — that is real activity, so bump the clock."""
        with self._lock:
            self._postprocessing = False
            self._last_activity = time.monotonic()

    def clear_postprocessing(self) -> None:
        """Un-stick the suspension WITHOUT counting as activity.

        Called on every `downloading` event: bytes flowing means we are
        demonstrably past postprocessing, and a postprocessor that started
        and then errored (a common reason to fall through to the next rung
        of the format chain) would otherwise leave the flag pinned and
        every later rung unwatched.

        Deliberately does NOT touch `_last_activity`. yt-dlp fires
        `downloading` hooks during retries without necessarily moving a byte,
        so refreshing the clock here would make the watchdog unable to ever
        fire — exactly the bug it exists to catch. Only `note_bytes` decides
        whether a downloading event was progress.
        """
        with self._lock:
            self._postprocessing = False

    def idle_seconds(self) -> float | None:
        """Seconds since the last sign of life, or None while not measurable
        (still queued for a worker, or a postprocessor is running)."""
        with self._lock:
            if self._postprocessing or not self._started:
                return None
            return time.monotonic() - self._last_activity


def make_postprocessor_hook(tracker: "DownloadProgress"):
    """yt-dlp postprocessor hook that suspends the stall watchdog.

    Fires around each postprocessor (Merger, FFmpegExtractAudio, embedders).
    Verified against yt-dlp 2026.09.16: `postprocessor/common.py` emits
    exactly `started` then `finished`, and postprocessors run sequentially
    rather than nested — so a plain boolean is enough and a counter would be
    false precision.

    If a postprocessor starts and never reports finishing (yt-dlp crashing
    mid-merge), the watchdog stays suspended and only the hard wall clock
    applies. That is the same bound as before this watchdog existed, so the
    failure mode is no worse than the status quo.

    Never raises: a bookkeeping error must not break a download that has
    already delivered its bytes.
    """
    def _hook(d):
        try:
            status = (d or {}).get("status")
            if status in ("started", "processing"):
                tracker.enter_postprocessing()
            elif status == "finished":
                tracker.exit_postprocessing()
            else:
                # Unknown status from a future yt-dlp: count it as a sign of
                # life, but never let it pin the watchdog off — an unknown
                # value must not be able to disable stall detection.
                tracker.note_event()
        except Exception:  # noqa: BLE001
            pass
    return _hook


STOP_POLL_S = 2.0      # how often a moving transfer asks whether the job was cancelled


def make_progress_hook(*, tracker: "DownloadProgress", job_id: str | None,
                       user_id: str, loop, what: str = "",
                       stop_now=None, stop_poll=None):
    """yt-dlp progress hook: feeds the stall tracker, then reports to the UI.

    ORDER IS LOAD-BEARING. Tracking runs first and unconditionally; the UI
    reporting below it is throttled to one update every 2s and only happens
    when a `job_id` was supplied. The previous version returned immediately
    on `if not job_id`, which is correct for *reporting* and wrong for
    *detection* — a download with no job attached stalls just as easily, and
    a throttle designed to spare the WebSocket must never decide whether a
    stall is noticed.

    `what` names the thing in the step text ("audio" → "Downloading audio 3MB…");
    video and audio share this one hook.

    `loop` is captured by the caller before the thread hop: a loop reference
    grabbed inside the hook (which runs on the download worker) is stale.

    STOPPING A TRANSFER. Python cannot stop a thread, but yt-dlp stops a
    transfer when a progress hook raises `DownloadCancelled` (verified on real
    HLS and DASH downloads: it returns within the same chunk). So while bytes
    flow, a cancel is real, not "best effort": `stop_now()` (cheap; read on
    every call) and `stop_poll()` (may read the database; read every
    `STOP_POLL_S`) end the transfer when either says so. A transfer that moves
    no bytes never reaches a hook, and ffmpeg-driven transfers only report at
    the end: those still finish or stall on their own.

    Apart from that one deliberate raise, the hook never raises: a failed
    progress update must not break a live download.
    """
    state = {"last_send": 0.0, "last_stop_poll": time.monotonic()}
    noun = f"Downloading {what} " if what else "Downloading "

    def _stop_asked() -> bool:
        try:
            if stop_now is not None and stop_now():
                return True
            if stop_poll is not None:
                now = time.monotonic()
                if now - state["last_stop_poll"] >= STOP_POLL_S:
                    state["last_stop_poll"] = now
                    return bool(stop_poll())
        except Exception:  # noqa: BLE001 — a failed check must not stop a healthy download
            return False
        return False

    def _hook(d):
        if (d or {}).get("status") == "downloading" and _stop_asked():
            from yt_dlp.utils import DownloadCancelled
            raise DownloadCancelled("stop asked")
        try:
            d = d or {}
            status = d.get("status")
            if status == "downloading":
                # Bytes are flowing, so we are demonstrably not postprocessing
                # any more. Without this, a postprocessor that STARTS and then
                # errors (a common reason to fall through to the next rung of
                # the format chain) leaves the flag pinned True and every
                # later rung runs completely unwatched.
                tracker.clear_postprocessing()
                tracker.note_bytes(d.get("downloaded_bytes") or 0)
            else:
                # 'finished' / 'error' are still signs of life — the transfer
                # is moving through its states, just not delivering bytes.
                tracker.note_event()
        except Exception:  # noqa: BLE001
            pass

        if not job_id or (d or {}).get("status") != "downloading":
            return
        try:
            now = time.monotonic()
            if now - state["last_send"] < 2.0:
                return
            state["last_send"] = now
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if downloaded <= 0:
                # Say what is actually happening. "Downloading 0MB…" reads as
                # a working download that happens to be at zero; it was the
                # text on screen for 90 minutes in the 2026-09-20 report, and
                # it told the user nothing. A connection that has not yet
                # delivered a byte deserves to say so.
                pct = 0.0
                step = "Connecting — no data received yet…"
            elif total <= 0:
                pct = 0.0
                step = f"{noun}{downloaded // 1024 // 1024}MB…"
            else:
                pct = min(99.0, (downloaded / total) * 100)
                step = f"{noun}{downloaded // 1024 // 1024}MB / {total // 1024 // 1024}MB"
            # Live WS broadcast for the in-memory activeJobs store
            from backend.core.ws_manager import ws_manager
            asyncio.run_coroutine_threadsafe(
                ws_manager.send_progress(job_id, pct, step, user_id), loop,
            )
            # Persist to DB so the polled /api/jobs endpoint sees real
            # progress. Status stays "running" — only progress fields move.
            from backend.agents.job_helper import update_job_status
            asyncio.run_coroutine_threadsafe(
                update_job_status(job_id, "running", progress_pct=pct,
                                  current_step=step),
                loop,
            )
        except Exception:  # noqa: BLE001
            pass  # never break the download for a progress UI issue

    return _hook


# How often a backoff looks up from its sleep, and how long one look may take.
_CANCEL_POLL_S = 1
_CANCEL_CHECK_TIMEOUT_S = 5


async def job_is_cancelled(job_id: str | None) -> bool:
    """`job_cancelled`, reading nothing for a download with no job."""
    if not job_id:
        return False
    from backend.agents.job_helper import job_cancelled
    return await job_cancelled(job_id)


def job_cancelled_from_thread(job_id: str | None, loop) -> bool:
    """`job_cancelled`, for the download worker thread: the read runs on the loop.

    Bounded and never raises, because this thread may have been abandoned by a
    stall (see backend/core/executors.py) and can outlive a stopped or closed loop: a read that
    can't finish counts as "not cancelled", as a DB error does in `job_cancelled`.
    No job means no read, so job-less downloads are unchanged.
    """
    if not job_id:
        return False
    coro = job_is_cancelled(job_id)
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception:  # noqa: BLE001 — the loop is gone
        coro.close()
        return False
    try:
        return future.result(timeout=_CANCEL_CHECK_TIMEOUT_S) is True
    except Exception:  # noqa: BLE001 — a timeout or a failed read: keep going
        future.cancel()
        return False


def cancellable_sleep(seconds: float, job_id: str | None, loop) -> None:
    """Sleep out a retry backoff, ending at once if the job is cancelled. The
    caller's next attempt then finds the cancel and stops the ladder."""
    if not job_id:
        time.sleep(seconds)
        return
    remaining = seconds
    while remaining > 0:
        if job_cancelled_from_thread(job_id, loop):
            return
        step = min(_CANCEL_POLL_S, remaining)
        time.sleep(step)
        remaining -= step


async def raise_if_cancelled_at_admission(job_id: str | None, url: str) -> None:
    """Stop a download whose job was cancelled while it QUEUED for a slot.

    Batch and channel downloads dispatch on the light pool, so their job reads
    "running" at once — but the transfer itself waits here for one of
    `_task_semaphore`'s two slots, AFTER the runner's own between-videos
    cancel check. A cancel landing during that wait used to be ignored and
    the whole transfer ran once a slot came free (measured live 2026-09-23:
    four downloads cancelled while queued all landed minutes later).
    Checked the moment the slot is ours, before a byte moves.
    """
    if not job_id:
        return
    from backend.agents.job_helper import job_cancelled
    if await job_cancelled(job_id):
        from backend.core.exceptions import JobCancelledError
        logger.info("Download of %s skipped — job %s was cancelled while it queued",
                    url[:80], job_id[:8])
        raise JobCancelledError("Cancelled before the transfer started")


async def await_with_stall_guard(
    awaitable,
    tracker: "DownloadProgress",
    *,
    hard_timeout: float,
    stall_timeout: float,
    url: str,
    label: str = "Download",
    footprint=None,
):
    """Await a download, failing early if it stops moving bytes.

    `footprint`, when given, is a zero-arg callable returning the attempt's
    bytes on disk (see `stem_footprint`). It is sampled every
    `_DISK_POLL_S` and any change counts as progress — the signal that keeps
    hook-less stages (FFmpegFD's HLS transfer, the local audio extract) from
    being mistaken for a stall.

    Two independent bounds:
      * `stall_timeout` — no progress for this long (suspended while a
        postprocessor runs). Raises DownloadStalledError.
      * `hard_timeout` — total wall clock, regardless of progress. Raises
        DownloadError. This is the pre-existing 90-minute cap, unchanged.

    Cancelling the task does NOT kill the worker thread — Python cannot.
    The download pool logs the abandonment and keeps the slot accounted for
    until yt-dlp really returns (see backend/core/executors.py). What this
    buys is that the JOB fails honestly and promptly instead of reporting
    nothing for an hour and a half.
    """
    task = asyncio.ensure_future(awaitable)
    deadline = _now() + hard_timeout
    next_disk_poll = 0.0
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_STALL_POLL_S)
            if done:
                try:
                    return task.result()
                except PoolExhaustedError as e:
                    # Not a DownloadError, so the engine's mapping to our
                    # exception types would not see it and the raw RuntimeError text
                    # would land verbatim in Job.error_message.
                    raise DownloadError(str(e)) from e

            if _now() >= deadline:
                raise DownloadTimeoutError(
                    f"{label} timed out after {hard_timeout / 60:.0f} minutes. "
                    f"The source may be too large or the connection too slow."
                )

            if footprint is not None and time.monotonic() >= next_disk_poll:
                next_disk_poll = time.monotonic() + _DISK_POLL_S
                try:
                    # Off the loop: a glob over a large videos directory is
                    # a real directory scan, and it always returns, so the
                    # default executor is the right place for it.
                    tracker.note_disk(int(await asyncio.to_thread(footprint)))
                except Exception:  # noqa: BLE001 — a probe must never fail the download
                    pass

            idle = tracker.idle_seconds()
            if idle is not None and idle >= stall_timeout:
                got_mb = tracker.bytes_downloaded / 1024 / 1024
                idle_txt = (
                    f"{idle / 60:.0f} minutes" if idle >= 120 else f"{idle:.0f} seconds"
                )
                logger.warning(
                    "[DOWNLOAD_STALLED] host=%s idle=%.0fs received=%.1fMB url=%s",
                    host_of(url), idle, got_mb, url[:80],
                )
                raise DownloadStalledError(
                    f"Download stalled: no data for {idle_txt} "
                    f"({got_mb:.1f} MB received). The source accepted the "
                    f"connection but stopped sending — yt-dlp kept retrying "
                    f"without receiving bytes. This is usually the host "
                    f"throttling or refusing the transfer; trying again, or a "
                    f"different source URL, normally clears it."
                )
    finally:
        if not task.done():
            task.cancel()
