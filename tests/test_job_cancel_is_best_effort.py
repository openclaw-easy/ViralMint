# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Cancelling a running job is best-effort, and the response must say so.

Reported against the hosted variant: cancelling returned "Job cancelled"
while the underlying download kept running, and the transfer completed 16
minutes later. Reading a flat success, the client started a duplicate.

The behaviour is correct and cannot change — yt-dlp and ffmpeg run in
threads Python cannot interrupt, so cancellation is cooperative: the row
flips at once, the runner stops at its next phase boundary, and whatever is
already in flight finishes. What was wrong is that the RESPONSE
was indistinguishable from a pre-emptive stop, so a client had no way to
know it should wait rather than retry.

These tests pin the structured answer: `cancelled` (the row flipped),
`best_effort` (work may still be in flight), and a `detail` a human can act
on — while keeping `message`, which the frontend reads.
"""
import asyncio
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.database import AsyncSessionLocal
from backend.models.job import Job


@pytest.fixture(scope="module", autouse=True)
def _tables():
    """The isolated test DB (conftest points DATA_DIR at a tmpdir) starts empty."""
    from backend.database import init_db
    asyncio.run(init_db())


@pytest_asyncio.fixture
async def client():
    from backend.main import create_app
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:16888",
        headers={"Origin": "http://127.0.0.1:16888"},
    ) as c:
        yield c


async def _make_job(job_id: str, status: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add(Job(id=job_id, job_type="download", status=status, user_id="local"))
        await db.commit()


@pytest.mark.asyncio
async def test_cancelling_a_running_job_reports_best_effort(client):
    """The case that cost a user a duplicate download."""
    await _make_job("cancel-running", "running")

    r = await client.delete("/api/jobs/cancel-running")
    assert r.status_code == 200
    body = r.json()

    assert body["cancelled"] is True
    assert body["best_effort"] is True, (
        "a running job may have uninterruptible work in flight — saying so "
        "is the whole point of this field"
    )
    # A client reading only prose still needs the warning in it.
    assert "message" in body, "the frontend reads `message` — keep it"
    detail = (body.get("detail") or "") + body["message"]
    assert "finish" in detail.lower() or "in flight" in detail.lower(), detail


@pytest.mark.asyncio
async def test_cancelling_a_pending_job_is_not_best_effort(client):
    """Nothing has started, so the cancel really is complete.

    Reporting best_effort unconditionally would be its own lie, and would
    train clients to ignore the field.
    """
    await _make_job("cancel-pending", "pending")

    body = (await client.delete("/api/jobs/cancel-pending")).json()
    assert body["cancelled"] is True
    assert body["best_effort"] is False


@pytest.mark.asyncio
async def test_the_row_really_does_flip_immediately(client):
    """Best-effort applies to the WORK, not to the row."""
    await _make_job("cancel-flip", "running")
    await client.delete("/api/jobs/cancel-flip")

    async with AsyncSessionLocal() as db:
        job = await db.get(Job, "cancel-flip")
        assert job.status == "cancelled"
