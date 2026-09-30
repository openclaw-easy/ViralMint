# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A cancelled download must still say WHY it failed.

Reported against the hosted variant: two download jobs came back as:

    status:        cancelled
    current_step:  "Cancelled — 0/1 downloaded before stopping"
    error_message: null

…with the user reasonably concluding that the downloader "never reported
an error". It had one. The finalizer in
`run_batch_download_urls` checks `if cancelled or await job_cancelled(...)`
BEFORE the `if not downloaded_ids:` failure branch, so a job that was both
cancelled *and* failed took the cancel path and dropped `error_summary` on
the floor.

Both halves were true at once: the jobs ended exactly at the download
wall-clock cap, not when a human pressed cancel. The user had cancelled
earlier; the runner only noticed when the transfer finally gave up.

Cancel is still the right STATUS (it is what the user asked for). Losing
the reason is the bug.
"""
import asyncio

import pytest

from backend.core import task_runner as dl_task


class _Recorder:
    """Captures every update_job_status call so assertions can read the row."""

    def __init__(self):
        self.calls = []

    async def __call__(self, job_id, status, **kw):
        self.calls.append({"status": status, **kw})

    def last(self, status=None):
        for c in reversed(self.calls):
            if status is None or c["status"] == status:
                return c
        raise AssertionError(
            f"no update_job_status(status={status!r}) call; got "
            f"{[c['status'] for c in self.calls]}"
        )

    @property
    def final_status(self):
        return self.calls[-1]["status"]


class _NullWs:
    async def send(self, *a, **kw): pass
    async def send_progress(self, *a, **kw): pass
    async def send_constraint_warning(self, *a, **kw): pass


@pytest.fixture
def wiring(monkeypatch):
    """Patch the runner's collaborators, leaving its own logic intact."""
    rec = _Recorder()
    import backend.agents.job_helper as jh
    import backend.core.ws_manager as wsm

    monkeypatch.setattr(jh, "update_job_status", rec)
    monkeypatch.setattr(wsm, "ws_manager", _NullWs())
    return rec


def _cancelled_after(n_calls: int):
    """job_cancelled(): False for the first n polls, True afterwards."""
    state = {"n": 0}

    async def _f(job_id):
        state["n"] += 1
        return state["n"] > n_calls
    return _f


@pytest.mark.asyncio
async def test_cancelled_and_failed_keeps_the_error_message(wiring, monkeypatch):
    """THE regression test for the null error_message."""
    import backend.agents.job_helper as jh

    # Cancel lands while the single transfer is already in flight, so the
    # loop gate never sees it — exactly the one-URL case they hit.
    monkeypatch.setattr(jh, "job_cancelled", _cancelled_after(1))

    async def _boom(*a, **kw):
        raise RuntimeError(
            "Download stalled: no data for 10 minutes (0.0 MB received)")
    monkeypatch.setattr(dl_task, "_download_single_video_to_db", _boom)

    await dl_task.run_batch_download_urls(
        "job-1", [{"url": "https://youtube.com/watch?v=kN3C6b2zCTU", "title": "t"}],
        analyze=False,
    )

    final = wiring.last()
    assert final["status"] == "cancelled", "cancel is what the user asked for"
    assert final.get("error_message"), (
        "a cancelled job that ALSO failed must keep the reason — this is the "
        "field that read as null"
    )
    assert "stalled" in final["error_message"].lower()


@pytest.mark.asyncio
async def test_cancelled_without_failure_invents_no_error(wiring, monkeypatch):
    """A clean cancel is not a failure. Don't fabricate one."""
    import backend.agents.job_helper as jh
    monkeypatch.setattr(jh, "job_cancelled", _cancelled_after(0))

    async def _never_called(*a, **kw):
        raise AssertionError("should not download after a cancel")
    monkeypatch.setattr(dl_task, "_download_single_video_to_db", _never_called)

    await dl_task.run_batch_download_urls(
        "job-2", [{"url": "https://example.com/v", "title": "t"}],
        analyze=False,
    )

    final = wiring.last()
    assert final["status"] == "cancelled"
    assert not final.get("error_message")


@pytest.mark.asyncio
async def test_cancelled_keeps_what_actually_downloaded(wiring, monkeypatch):
    """Partial work is still the user's — report it alongside the error."""
    import backend.agents.job_helper as jh
    monkeypatch.setattr(jh, "job_cancelled", _cancelled_after(2))
    import backend.core.http_utils as hu
    monkeypatch.setattr(hu, "jittered_delay", lambda *a, **kw: 0)

    calls = {"n": 0}

    async def _one_ok_then_boom(job_id, url, title, user_id, options=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"id": "dv-1", "title": title}
        raise RuntimeError("host refused the transfer")
    monkeypatch.setattr(dl_task, "_download_single_video_to_db", _one_ok_then_boom)

    await dl_task.run_batch_download_urls(
        "job-3",
        [{"url": "https://e.com/1", "title": "a"},
         {"url": "https://e.com/2", "title": "b"},
         {"url": "https://e.com/3", "title": "c"}],
        analyze=False,
    )

    final = wiring.last()
    assert final["status"] == "cancelled"
    assert final["output_data"]["downloaded_ids"] == ["dv-1"]
    assert "refused" in (final.get("error_message") or "")


@pytest.mark.asyncio
async def test_failures_are_numbered_by_url_position_not_error_position(
        wiring, monkeypatch):
    """"Video 1/5 failed" must mean URL 1, not "the first failure".

    The summary enumerated `errors`, so a batch of 5 where only URLs 4 and 5
    failed told the user "Video 1/5" and "Video 2/5" — naming two downloads
    that had actually succeeded. The whole point of this batch of fixes is
    that the error text can be trusted.
    """
    import backend.agents.job_helper as jh

    async def _never_cancelled(job_id):
        return False
    monkeypatch.setattr(jh, "job_cancelled", _never_cancelled)
    import backend.core.http_utils as hu
    monkeypatch.setattr(hu, "jittered_delay", lambda *a, **kw: 0)

    n = {"i": 0}

    async def _last_two_fail(job_id, url, title, user_id, options=None):
        n["i"] += 1
        if n["i"] <= 3:
            return {"id": f"dv-{n['i']}", "title": title}
        raise RuntimeError("host refused")
    monkeypatch.setattr(dl_task, "_download_single_video_to_db", _last_two_fail)

    await dl_task.run_batch_download_urls(
        "job-5",
        [{"url": f"https://e.com/{k}", "title": f"t{k}"} for k in range(1, 6)],
        analyze=False,
    )

    msg = wiring.last().get("error_message") or ""
    assert "Video 4/5" in msg and "Video 5/5" in msg, msg
    assert "Video 1/5" not in msg, f"named a download that succeeded: {msg}"
    assert "Video 2/5" not in msg, f"named a download that succeeded: {msg}"


@pytest.mark.asyncio
async def test_rate_limited_batch_reports_the_rate_limit_not_a_python_error(
        wiring, monkeypatch):
    """A skipped-because-rate-limited entry must not crash the summary.

    The skip path appended a plain STRING to `errors` while every other
    path appended a `(title, message)` tuple, and the summary builder
    unpacks `for i, (t, m) in enumerate(errors)`. With 3+ URLs that raises
    `ValueError: too many values to unpack (expected 2)`, which the outer
    handler then wrote into error_message — so a rate-limited batch told
    the user about a tuple instead of about YouTube.
    """
    import backend.agents.job_helper as jh
    from backend.core.exceptions import RateLimitError

    async def _never_cancelled(job_id):
        return False
    monkeypatch.setattr(jh, "job_cancelled", _never_cancelled)
    import backend.core.http_utils as hu
    monkeypatch.setattr(hu, "jittered_delay", lambda *a, **kw: 0)

    async def _rate_limited(*a, **kw):
        raise RateLimitError("HTTP Error 429: Too Many Requests")
    monkeypatch.setattr(dl_task, "_download_single_video_to_db", _rate_limited)

    await dl_task.run_batch_download_urls(
        "job-4",
        [{"url": "https://e.com/1", "title": "a"},
         {"url": "https://e.com/2", "title": "b"},
         {"url": "https://e.com/3", "title": "c"}],
        analyze=False,
    )

    final = wiring.last()
    assert final["status"] == "failed"
    msg = final.get("error_message") or ""
    assert "unpack" not in msg, f"internal Python error leaked to the user: {msg}"
    assert "429" in msg or "rate" in msg.lower(), msg
