# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Named, bounded thread pools for work that cannot be cancelled.

Why this module exists
──────────────────────
`asyncio.to_thread()` hands every call to ONE process-wide executor —
CPython's implicit default, sized `min(32, cpu_count + 4)` (12 workers on
an 8-core machine). backend/ has ~140 `to_thread` call sites, from probing
a file's duration to a per-chunk `f.read()` in the media range server. They
all share those 12 workers.

That is survivable only while every call returns. Two of ours may not:

  * **yt-dlp downloads.** `socket_timeout`, `retries` and `fragment_retries`
    with exponential backoff can burn many minutes per format rung having
    moved zero bytes, and `FORMAT_FALLBACK_CHAIN` has several rungs, each
    retried.
  * **Whisper.** No internal wall clock at all, plus a blocking
    `_transcribe_gate.acquire()` for batch-length audio.

And `asyncio.wait_for(asyncio.to_thread(f), timeout=N)` **cannot cancel the
thread**. When the timeout fires, `f` keeps running and its worker is gone
until `f` returns on its own — which, for a wedged download, may be never.

Measured on the real app with 12 workers parked:

    pure-async route        200 in 0.014s      ok
    GET /api/jobs           200 in 0.015s      ok   (aiosqlite owns its threads)
    any route using to_thread   never returned      <-- the whole-app freeze

That is how a reported freeze presented: two downloads stuck at 0 MB, then
every API call blocked until the app was restarted.

What this module changes
────────────────────────
Long, uncancellable work moves onto its own small named pool. A wedged
download can then only ever consume `download_pool`; the request path keeps
the default executor to itself. When a dedicated pool IS exhausted, callers
get a fast, diagnostic `PoolExhaustedError` instead of an unbounded wait —
"the app is frozen" becomes "2 of 4 download slots are held by stuck work".

Threads are named (`vm-download-*`, `vm-transcribe-*`). Anonymous
`ThreadPoolExecutor-0_3` threads are a large part of why this was invisible
for so long in a stack dump.

What it deliberately does NOT do
────────────────────────────────
It does not make a stuck thread killable — Python offers no such thing. It
bounds the blast radius and makes the accounting honest. Killing the work
itself is the stall watchdog's job (see `ytdlp_service._progress_hook` and
`DOWNLOAD_STALL_TIMEOUT_S`); this module is the seatbelt for when that
watchdog cannot fire because yt-dlp never called back at all.

ffmpeg is intentionally left on the default executor: every `subprocess.run`
in `ffmpeg_service` carries an explicit `timeout=`, so those threads always
return and are not leakers.

Known, accepted, pre-existing
─────────────────────────────
A still-running worker thread delays interpreter exit: `ThreadPoolExecutor`
workers are non-daemon, so both `concurrent.futures.thread._python_exit` and
`threading._shutdown()` join them, and `shutdown(wait=False)` does not opt
out. Measured: a process with one wedged worker sat past 30s and
needed SIGKILL. This predates this module (the implicit default executor
behaves identically) and is bounded in practice — `launcher._terminate_process_tree`
escalates `terminate()` → `wait(timeout=8)` → `kill()`, so Quit costs at most
~8 extra seconds rather than hanging. Making the workers daemon threads means
re-implementing `ThreadPoolExecutor._adjust_thread_count` against CPython
internals, which is a worse trade than 8 seconds on an already-degraded process.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


# Sized for the work, not for the machine.
#
# DOWNLOAD: every yt-dlp thread in the process lives here. The arithmetic:
# `task_runner._task_semaphore` (cap 3) bounds the heavy jobs that download,
# plus headroom for the UNgated helpers (`get_video_info`,
# `list_channel_videos`) that API routes call directly. 8 covers real
# concurrency without ever being so large that leaked threads go unnoticed.
#
# Sizing this pool is a trade, not a maximum: too small and legitimate
# parallel work starts raising PoolExhaustedError where it used to queue.
DOWNLOAD_POOL_WORKERS = 8

# TRANSCRIBE: Whisper on CPU is compute-bound, and `whisper_service._run`
# takes a BLOCKING `_transcribe_gate.acquire()` for any audio at or above
# `_SERIALIZE_MIN_SECONDS` (120s — i.e. most real clips). A worker parked on
# that gate is occupied while doing no work, so the pool has to be sized
# above the gate's blocking depth: at 2, two batch transcriptions would fill
# both slots (one decoding, one waiting) and a third caller — including a
# short gate-exempt interactive one, or ensure_model() — would queue out its
# whole admission budget and then fail. Under the old shared 12-worker
# default that case was a wait, never an error; 4 keeps it a wait.
TRANSCRIBE_POOL_WORKERS = 4

# The shared request-path pool. Explicit beats CPython's implicit
# `min(32, cpu_count + 4)`: the size stops depending on which machine the
# desktop app happens to be installed on, and the threads get a name.
DEFAULT_POOL_WORKERS = 32

# How long a caller queues for a slot before giving up.
#
# Deliberately GENEROUS. Downloads and transcriptions are dispatched as
# background jobs, so waiting behind other real work is correct behaviour —
# failing fast on a merely *busy* pool would turn "your download is second in
# line" into an error, which is a regression, not a fix. Fast failure is
# reserved for a pool that is *wedged* (every slot held by abandoned work),
# which `run()` detects without waiting at all.
DOWNLOAD_ADMISSION_TIMEOUT_S = 300.0     # 5 min behind the other gated downloads
TRANSCRIBE_ADMISSION_TIMEOUT_S = 900.0   # CPU Whisper on long audio is slow by nature
DEFAULT_ADMISSION_TIMEOUT_S = 300.0

# Admission is poll-based (see NamedPool._reserve). These pools see a handful
# of submissions a minute, so a 25 ms poll costs nothing and keeps the pool
# free of loop-bound asyncio primitives — NamedPool instances are module
# globals and outlive any single event loop, which asyncio.Semaphore does not.
_ADMISSION_POLL_S = 0.025


class PoolExhaustedError(RuntimeError):
    """Every worker in a named pool is occupied and none came free in time.

    Raised instead of queueing forever. In practice this means work that
    cannot be cancelled is still running with nobody awaiting it — the
    message says how many slots are in that state so the cause is readable
    from the error alone.
    """


class NamedPool:
    """A bounded, named `ThreadPoolExecutor` with honest occupancy accounting.

    `occupied` counts callables that have been submitted and have not yet
    returned — INCLUDING ones whose awaiting coroutine has since been
    cancelled or timed out. That is the whole point: the executor and
    `asyncio` both consider an abandoned call finished, while the operating
    system thread is still very much running and still unavailable. Counting
    it as free is what let the old shared pool drain to zero invisibly.
    """

    def __init__(
        self,
        name: str,
        max_workers: int,
        admission_timeout: float = DEFAULT_ADMISSION_TIMEOUT_S,
    ) -> None:
        self.name = name
        self.max_workers = max_workers
        self.admission_timeout = admission_timeout
        self.thread_name_prefix = f"vm-{name}"
        # Created lazily and re-created after a shutdown. The pools are module
        # globals that outlive any single event loop, so a lifespan teardown
        # must not leave them permanently dead: `uvicorn --reload` and the
        # test suite both start a second app in the same process, and an
        # executor shut down by the first one would fail every later submit
        # with "cannot schedule new futures after shutdown".
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._occupied = 0   # submitted, not yet returned
        self._awaiting = 0   # of those, still has a live awaiter

    # ── accounting ──────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Snapshot for diagnostics, `/health` and the exhaustion message."""
        with self._lock:
            occupied, awaiting = self._occupied, self._awaiting
        return {
            "name": self.name,
            "max_workers": self.max_workers,
            "occupied": occupied,
            "awaiting": awaiting,
            # Running, but nobody is waiting for the answer any more. These
            # are the slots that never come back on their own.
            #
            # Clamped at 0: `_release_slot()` fires on the worker thread the
            # instant the work ends, while `_release_awaiter()` fires when
            # the awaiting coroutine next resumes. Between those two the raw
            # difference dips negative, and a negative "abandoned" on
            # /health reads as a bug in the thing you are using to diagnose
            # a bug.
            "abandoned": max(0, occupied - awaiting),
            "available": max(0, self.max_workers - occupied),
        }

    def _reserve(self) -> bool:
        with self._lock:
            if self._occupied >= self.max_workers:
                return False
            self._occupied += 1
            self._awaiting += 1
            return True

    def _release_slot(self) -> None:
        with self._lock:
            self._occupied -= 1

    def _release_awaiter(self) -> None:
        with self._lock:
            self._awaiting -= 1

    def _get_executor(self) -> ThreadPoolExecutor:
        """The live executor, re-creating it if a shutdown retired the last one."""
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.max_workers,
                    thread_name_prefix=self.thread_name_prefix,
                )
            return self._executor

    # ── running work ────────────────────────────────────────────────────

    async def run(self, fn, /, *args, admission_timeout: float | None = None,
                  on_admit=None, **kwargs):
        """Run `fn(*args, **kwargs)` on this pool. Drop-in for `asyncio.to_thread`.

        Copies the caller's contextvars, exactly as `asyncio.to_thread` does,
        so logging context and request-scoped state survive the hop.

        Raises `PoolExhaustedError` when no worker comes free — immediately
        if the pool is WEDGED (every slot abandoned, so waiting cannot help),
        otherwise after `admission_timeout` seconds of queueing behind live
        work. `admission_timeout=None` means "use the pool's default", not
        "wait forever"; there is deliberately no unbounded option.

        `on_admit`, when given, is called (on the event loop) the moment a
        worker slot is reserved for this call — i.e. when the work stops
        QUEUEING. A stall watchdog uses it to start its clock: time spent
        waiting for a free worker is backpressure, not a stalled transfer.

        ⚠️ `admission_timeout` and `on_admit` are consumed here, so they
        cannot be forwarded as keyword arguments to `fn`. No current caller
        needs that; a `fn` with its own parameter of either name must be
        wrapped in a lambda.
        """
        budget = self.admission_timeout if admission_timeout is None else admission_timeout
        deadline = time.monotonic() + budget
        while not self._reserve():
            s = self.stats()
            # WEDGED: every slot is held by work whose awaiter is already gone.
            # Those threads may never return, so waiting cannot help — say so
            # immediately instead of burning the caller's budget on a queue
            # that will never move. This is the state a reported whole-app
            # freeze ended in, and it used to present as an infinite hang.
            if s["abandoned"] >= self.max_workers:
                raise PoolExhaustedError(
                    f"{self.name} pool is wedged: all {s['max_workers']} workers are held "
                    f"by work that was abandoned and cannot be cancelled. Waiting will not "
                    f"free them — restart ViralMint to clear it."
                )
            # BUSY: the slots hold live work somebody is still awaiting. That
            # is ordinary backpressure and queueing is the correct answer, so
            # we wait out the (generous) budget before giving up.
            if time.monotonic() >= deadline:
                raise PoolExhaustedError(
                    f"{self.name} pool is full: {s['occupied']}/{s['max_workers']} workers "
                    f"busy ({s['abandoned']} held by work nobody is waiting on) and none "
                    f"came free in {budget:.0f}s. Try again once current work finishes."
                )
            await asyncio.sleep(_ADMISSION_POLL_S)

        if on_admit is not None:
            try:
                on_admit()
            except Exception:  # noqa: BLE001 — a notification must never leak the slot
                logger.exception("%s pool: on_admit callback failed", self.name)

        ctx = contextvars.copy_context()
        call = functools.partial(ctx.run, fn, *args, **kwargs)

        # Set the moment the work item actually begins on a worker thread.
        # `shutdown(cancel_futures=True)` can cancel a SUBMITTED-but-not-yet-
        # STARTED item, and then `_tracked` never runs and never releases the
        # slot. Because the pools are module globals that survive shutdown and
        # get reused, that leak is permanent and accumulates across restarts
        # until the pool declares itself wedged and refuses everything.
        started = threading.Event()

        def _tracked():
            started.set()
            try:
                return call()
            finally:
                # Releases the SLOT when the thread truly finishes — which,
                # for an abandoned call, is long after the awaiter gave up.
                self._release_slot()

        def _release_if_never_started(fut):
            if fut.cancelled() and not started.is_set():
                self._release_slot()

        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(self._get_executor(), _tracked)
        except BaseException:
            # The slot was reserved above but `_tracked` will never run, so
            # neither release fires on its own. Without this the counters
            # leak permanently on every rejected submit — e.g. "cannot
            # schedule new futures after shutdown" during app teardown —
            # and the pool reports itself wedged forever afterwards.
            self._release_slot()
            self._release_awaiter()
            raise
        future.add_done_callback(_release_if_never_started)
        try:
            # `shield` is load-bearing, not defensive. Without it, cancelling
            # the awaiter cancels the future too — and a future cancelled
            # BEFORE the executor picked it up never runs `_tracked`, so
            # `_release_slot` never fires and the slot leaks for real. Shield
            # guarantees `_tracked` always runs, so the slot always returns
            # once the work itself ends.
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # The awaiter is gone; the thread is not. Say so out loud — a
            # silent leak here is the original bug.
            logger.warning(
                "%s pool: a call was abandoned (cancelled or timed out) while its "
                "thread was still running — the worker stays occupied until it "
                "returns. %s", self.name, self.stats(),
            )
            # Nobody will ever await this future now. Retrieve whatever it
            # ends up with, or asyncio logs a bare "Future exception was
            # never retrieved" with no hint of which pool it came from.
            future.add_done_callback(self._log_abandoned_result)
            raise
        finally:
            self._release_awaiter()

    def _log_abandoned_result(self, future) -> None:
        """Consume the result of a call whose awaiter had already given up."""
        try:
            exc = future.exception()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — a logger must never raise
            return
        if exc is not None:
            logger.info(
                "%s pool: abandoned call finished with %s: %s",
                self.name, type(exc).__name__, exc,
            )
        else:
            logger.info("%s pool: abandoned call finished; worker released.", self.name)

    async def wait_until_occupied(self, target: int, timeout: float = 10.0) -> None:
        """Block until `occupied == target`. Test/diagnostic helper only."""
        deadline = time.monotonic() + timeout
        while self.stats()["occupied"] != target:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"{self.name} pool did not reach occupied={target} "
                    f"within {timeout}s (stats={self.stats()})"
                )
            await asyncio.sleep(_ADMISSION_POLL_S)

    def shutdown(self) -> None:
        """Drop queued work and retire the current executor.

        Never waits: a wedged download is exactly what we refuse to block app
        shutdown on. Threads already running keep running (Python cannot stop
        them) and still release their slots when they finish.

        The pool stays USABLE afterwards — the next `run()` builds a fresh
        executor. Retiring it permanently would break the second app in a
        process, which `uvicorn --reload` and the test suite both create.
        """
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


# ── The pools ───────────────────────────────────────────────────────────
#
# Module globals so occupancy is process-wide, the way the work is. Add a
# pool here only for work that can genuinely fail to return; everything with
# an enforced internal timeout belongs on the default executor.

download_pool = NamedPool(
    "download", DOWNLOAD_POOL_WORKERS, DOWNLOAD_ADMISSION_TIMEOUT_S)
transcribe_pool = NamedPool(
    "transcribe", TRANSCRIBE_POOL_WORKERS, TRANSCRIBE_ADMISSION_TIMEOUT_S)

_ALL_POOLS = (download_pool, transcribe_pool)

_default_executor: ThreadPoolExecutor | None = None


def install_default_executor(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Give `asyncio.to_thread` a pool we sized and named.

    Call as early in the lifespan as possible — replacing the default
    executor after `to_thread` has already created CPython's implicit one
    would strand whatever is running on it.

    Safe to call again for a SECOND loop in the same process (`uvicorn
    --reload`, the test suite): the executor is built once and then simply
    installed on whichever loop asks. Returning early on the second call
    instead would leave that loop quietly using CPython's implicit pool —
    the exact thing this module exists to get rid of.
    """
    global _default_executor
    # asyncio.run() shuts the loop's default executor down on exit — ours,
    # once installed — so a later loop must get a fresh one, not a dead pool
    # that fails every to_thread with "cannot schedule new futures".
    first = _default_executor is None or getattr(_default_executor, "_shutdown", False)
    if first:
        _default_executor = ThreadPoolExecutor(
            max_workers=DEFAULT_POOL_WORKERS, thread_name_prefix="vm-default",
        )
    loop = loop or asyncio.get_event_loop()
    loop.set_default_executor(_default_executor)
    if first:
        logger.info(
            "Thread pools ready — default=%d, download=%d, transcribe=%d",
            DEFAULT_POOL_WORKERS, DOWNLOAD_POOL_WORKERS, TRANSCRIBE_POOL_WORKERS,
        )


def all_pool_stats() -> list[dict]:
    """Occupancy of every named pool — surfaced on `/health`.

    A non-zero `abandoned` here is the single clearest signal that the
    process has leaked uncancellable work and wants a restart.
    """
    return [p.stats() for p in _ALL_POOLS]


def shutdown_pools() -> None:
    """Tear every pool down without waiting. Safe to call twice."""
    global _default_executor
    for pool in _ALL_POOLS:
        try:
            pool.shutdown()
        except Exception as e:  # noqa: BLE001 — shutdown must never raise
            logger.debug("Pool %s shutdown failed: %s", pool.name, e)
    if _default_executor is not None:
        try:
            _default_executor.shutdown(wait=False, cancel_futures=True)
        except Exception as e:  # noqa: BLE001
            logger.debug("Default executor shutdown failed: %s", e)
        _default_executor = None
