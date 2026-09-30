# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""One wedged job must not be able to freeze the whole app.

Regression test for a reported freeze: two YouTube downloads sat at
0 MB, and from then on *every* API call blocked until
the app was restarted.

Root cause, measured against the real app before this module existed:

  * Every one of the ~140 `asyncio.to_thread(...)` call sites in backend/
    shares ONE process-wide default ThreadPoolExecutor, sized by CPython
    at `min(32, cpu_count + 4)` — 12 workers on an 8-core machine.
  * `asyncio.wait_for(asyncio.to_thread(f), timeout=N)` cannot cancel the
    thread. When the timeout fires, `f` keeps running and its worker is
    parked FOREVER (proved below in `test_abandoned_call_still_occupies_a_slot`).
  * yt-dlp and Whisper have no internal wall clock, so they are the two
    call sites that can park a worker indefinitely. Enough wedged
    downloads and the pool hits zero free workers; every later request
    that touches `to_thread` then waits forever. Only a restart clears it.

The fix these tests pin: long, uncancellable work runs on its own small
named pool, so its blast radius is that pool and the request path keeps
its workers.
"""
import asyncio
import threading
import time

import pytest

from backend.core.executors import (
    NamedPool,
    PoolExhaustedError,
    download_pool,
    transcribe_pool,
)


def _blocker(ev: threading.Event, timeout: float = 30.0):
    """Stand-in for a stalled yt-dlp download: blocks until released."""
    ev.wait(timeout)


@pytest.mark.asyncio
async def test_pool_bounds_its_own_concurrency():
    """A pool runs at most `max_workers` callables at once."""
    pool = NamedPool("test-bound", max_workers=2, admission_timeout=0.2)
    release = threading.Event()
    try:
        running = [asyncio.create_task(pool.run(_blocker, release)) for _ in range(2)]
        await pool.wait_until_occupied(2, timeout=5)

        assert pool.stats()["occupied"] == 2
        assert pool.stats()["available"] == 0
    finally:
        release.set()
        await asyncio.gather(*running, return_exceptions=True)
        pool.shutdown()


@pytest.mark.asyncio
async def test_exhausted_pool_fails_fast_instead_of_queueing_forever():
    """The reported symptom was an API call that never returned.

    A full pool must raise a diagnostic error within `admission_timeout`,
    not queue the caller indefinitely.
    """
    pool = NamedPool("test-exhaust", max_workers=1, admission_timeout=0.3)
    release = threading.Event()
    try:
        held = asyncio.create_task(pool.run(_blocker, release))
        await pool.wait_until_occupied(1, timeout=5)

        t0 = time.monotonic()
        with pytest.raises(PoolExhaustedError) as exc:
            await pool.run(lambda: None)
        waited = time.monotonic() - t0

        assert waited < 3.0, f"fail-fast took {waited:.1f}s — that is a hang, not an error"
        # The message has to be actionable: it is what turns "the app is
        # frozen" into "a stuck download is holding the slot".
        assert "test-exhaust" in str(exc.value)
    finally:
        release.set()
        await asyncio.gather(held, return_exceptions=True)
        pool.shutdown()


@pytest.mark.asyncio
async def test_a_wedged_pool_fails_immediately_without_burning_the_budget():
    """Wedged is not the same as busy, and must not wait at all.

    When every slot is held by ABANDONED work, the threads may never
    return, so queueing cannot help. The caller has to be told at once —
    with an error that names the restart — rather than waiting out a
    timeout on a queue that will never move.
    """
    pool = NamedPool("test-wedged", max_workers=1, admission_timeout=600.0)
    release = threading.Event()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(pool.run(_blocker, release), timeout=0.3)
        assert pool.stats()["abandoned"] == 1

        t0 = time.monotonic()
        with pytest.raises(PoolExhaustedError, match="wedged"):
            await pool.run(lambda: None)
        waited = time.monotonic() - t0

        assert waited < 1.0, (
            f"waited {waited:.1f}s on a wedged pool — it must short-circuit, "
            f"not sit out the {pool.admission_timeout}s admission budget"
        )
    finally:
        release.set()
        pool.shutdown()


@pytest.mark.asyncio
async def test_a_merely_busy_pool_queues_rather_than_erroring():
    """Backpressure is not failure.

    Slots held by LIVE, awaited work will come free. Failing fast there
    would turn "your download is second in line" into an error — a
    regression, not a fix.
    """
    pool = NamedPool("test-busy", max_workers=1, admission_timeout=30.0)
    release = threading.Event()
    try:
        held = asyncio.create_task(pool.run(_blocker, release))
        await pool.wait_until_occupied(1, timeout=5)
        assert pool.stats()["abandoned"] == 0

        queued = asyncio.create_task(pool.run(lambda: "ran after waiting"))
        await asyncio.sleep(0.3)
        assert not queued.done(), "should still be queued behind live work"

        release.set()
        assert await asyncio.wait_for(queued, timeout=5) == "ran after waiting"
        await asyncio.gather(held, return_exceptions=True)
    finally:
        release.set()
        pool.shutdown()


@pytest.mark.asyncio
async def test_abandoned_call_still_occupies_a_slot():
    """`wait_for` giving up does NOT free the worker — accounting must say so.

    This is the half of the bug that made it permanent. Python cannot
    cancel a running thread, so a timed-out download keeps its slot. The
    pool has to count that slot as occupied (and report it as abandoned)
    rather than pretend it came back.
    """
    pool = NamedPool("test-abandon", max_workers=2, admission_timeout=0.2)
    release = threading.Event()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(pool.run(_blocker, release), timeout=0.3)

        stats = pool.stats()
        assert stats["occupied"] == 1, "a timed-out thread still holds its worker"
        assert stats["abandoned"] == 1, "nobody is awaiting it any more"
        assert stats["available"] == 1

        # …and once the thread really finishes, the slot comes back.
        release.set()
        await pool.wait_until_occupied(0, timeout=5)
        assert pool.stats()["abandoned"] == 0
    finally:
        release.set()
        pool.shutdown()


@pytest.mark.asyncio
async def test_saturated_download_pool_does_not_starve_the_request_path():
    """THE regression test for the 2026-09-20 report.

    Wedge every download slot, then confirm a plain `asyncio.to_thread`
    — which is what request handlers all over backend/api use — still
    completes promptly. Before the split, this is precisely what hung.
    """
    release = threading.Event()
    wedged = []
    try:
        for _ in range(download_pool.max_workers):
            wedged.append(asyncio.create_task(download_pool.run(_blocker, release)))
        await download_pool.wait_until_occupied(download_pool.max_workers, timeout=10)
        assert download_pool.stats()["available"] == 0

        t0 = time.monotonic()
        await asyncio.wait_for(asyncio.to_thread(lambda: "request path"), timeout=5)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, (
            f"a request-path to_thread waited {elapsed:.1f}s behind a saturated "
            f"download pool — the pools are not isolated"
        )
    finally:
        release.set()
        await asyncio.gather(*wedged, return_exceptions=True)


@pytest.mark.asyncio
async def test_download_and_transcribe_pools_are_separate():
    """Whisper must not be able to starve downloads, or vice versa."""
    release = threading.Event()
    wedged = []
    try:
        for _ in range(transcribe_pool.max_workers):
            wedged.append(asyncio.create_task(transcribe_pool.run(_blocker, release)))
        await transcribe_pool.wait_until_occupied(transcribe_pool.max_workers, timeout=10)

        assert await asyncio.wait_for(
            download_pool.run(lambda: "ok"), timeout=5) == "ok"
    finally:
        release.set()
        await asyncio.gather(*wedged, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_propagates_result_and_exception():
    """The pool is a drop-in for `asyncio.to_thread` on the happy paths."""
    assert await download_pool.run(lambda a, b: a + b, 2, 3) == 5
    assert await download_pool.run(lambda *, k: k, k="kw") == "kw"

    class Boom(RuntimeError):
        pass

    def raises():
        raise Boom("from the worker thread")

    with pytest.raises(Boom, match="from the worker thread"):
        await download_pool.run(raises)


@pytest.mark.asyncio
async def test_run_copies_contextvars_like_to_thread():
    """`asyncio.to_thread` propagates contextvars; so must we."""
    import contextvars

    var = contextvars.ContextVar("vm_test_var", default="unset")
    var.set("propagated")
    assert await download_pool.run(var.get) == "propagated"


@pytest.mark.asyncio
async def test_a_rejected_submit_does_not_leak_the_slot():
    """If the executor refuses the work, the reservation must be undone.

    The slot is reserved before submitting; `_tracked` is what gives it
    back. When the submit itself raises — "cannot schedule new futures
    after shutdown" during teardown is the realistic case — `_tracked`
    never runs, so an unguarded path leaks the slot permanently and the
    pool then reports itself wedged forever.
    """
    pool = NamedPool("test-rejected", max_workers=2, admission_timeout=0.2)

    # Force the submit itself to fail, the way a retired executor would.
    class _Dead:
        def submit(self, *a, **kw):
            raise RuntimeError("cannot schedule new futures after shutdown")
    pool._get_executor = lambda: _Dead()

    with pytest.raises(RuntimeError):
        await pool.run(lambda: None)

    stats = pool.stats()
    assert stats["occupied"] == 0, f"slot leaked on a rejected submit: {stats}"
    assert stats["awaiting"] == 0, f"awaiter leaked on a rejected submit: {stats}"
    assert stats["available"] == pool.max_workers


@pytest.mark.asyncio
async def test_a_pool_survives_shutdown_and_works_again():
    """A lifespan teardown must not kill the pool for the whole process.

    The pools are module globals; `uvicorn --reload` and the test suite both
    build a SECOND app in the same process. If `shutdown()` retired the
    executor permanently, every submit after the first app's teardown would
    fail with "cannot schedule new futures after shutdown" — turning an
    ordinary reload into a dead downloader.
    """
    pool = NamedPool("test-restart", max_workers=2, admission_timeout=1.0)
    assert await pool.run(lambda: "before") == "before"

    pool.shutdown()
    assert await pool.run(lambda: "after") == "after"
    assert pool.stats()["available"] == pool.max_workers

    pool.shutdown()
    pool.shutdown()  # idempotent
    pool.shutdown()


def test_pools_are_named_for_diagnosis():
    """Anonymous threads are why this bug was invisible in a stack dump."""
    assert download_pool.thread_name_prefix.startswith("vm-")
    assert transcribe_pool.thread_name_prefix.startswith("vm-")
