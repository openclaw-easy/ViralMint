# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A job that fails must TELL the UI it failed (`job_failed` over the WS).

`update_job_status` writes the DB only; the WS event is the runner's job.
Runner paths that wrote `failed` and returned without a word (agent early
exits, catch-all handlers, the task_runner wrappers) left a chat card, an
Activity row or a busy button showing the job as running until a reload.

They now go through `job_helper.fail_job`, which announces exactly once:
  - a live job      → job_failed sent
  - already terminal → DB written as before, NOT re-announced (no second,
                       contradicting toast after the agent's own event)
  - cancelled       → stays cancelled with the reason recorded, no toast
  - row missing     → still announced, so a tracked card can clear
The drift guard at the bottom keeps new silent writes out.
"""
import asyncio
import pathlib
import re

import pytest

from backend.database import AsyncSessionLocal
from backend.models.job import Job

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", autouse=True)
def _tables():
    """The isolated test DB (conftest points DATA_DIR at a tmpdir) may start empty."""
    from backend.database import init_db
    asyncio.run(init_db())


@pytest.fixture
def sent(monkeypatch):
    """Capture every WS message instead of sending it."""
    from backend.core.ws_manager import ws_manager
    out = []

    async def fake_send(msg, user_id=None, *a, **k):
        out.append(msg)

    monkeypatch.setattr(ws_manager, "send", fake_send)
    return out


async def _make_job(job_id: str, status: str, job_type: str = "generate") -> None:
    async with AsyncSessionLocal() as db:
        db.add(Job(id=job_id, job_type=job_type, status=status, user_id="local"))
        await db.commit()


async def _status(job_id: str):
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Job.status, Job.error_message).where(Job.id == job_id))).first()


def _failed_events(sent, job_id):
    return [m for m in sent if m.get("type") == "job_failed" and m.get("job_id") == job_id]


# ── fail_job ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_live_job_is_failed_and_announced_once(sent):
    from backend.agents.job_helper import fail_job
    await _make_job("fj-live", "running")
    await fail_job("fj-live", "Source video not found", "local")
    status, err = await _status("fj-live")
    assert (status, err) == ("failed", "Source video not found")
    assert len(_failed_events(sent, "fj-live")) == 1
    assert _failed_events(sent, "fj-live")[0]["error"] == "Source video not found"


@pytest.mark.asyncio
async def test_pending_job_is_announced(sent):
    from backend.agents.job_helper import fail_job
    await _make_job("fj-pending", "pending")
    await fail_job("fj-pending", "Video not found", "local")
    assert (await _status("fj-pending"))[0] == "failed"
    assert len(_failed_events(sent, "fj-pending")) == 1


@pytest.mark.asyncio
async def test_step_and_progress_are_written(sent):
    from backend.agents.job_helper import fail_job
    from sqlalchemy import select
    await _make_job("fj-step", "running")
    await fail_job("fj-step", "Failed to analyze any of 2 videos", "local",
                   progress_pct=100, current_step="All 2 videos failed analysis")
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(Job).where(Job.id == "fj-step"))).scalar_one()
    assert row.status == "failed"
    assert row.current_step == "All 2 videos failed analysis"
    assert row.progress_pct == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["failed", "success"])
async def test_already_terminal_job_is_not_announced_again(sent, prior):
    """The agent already sent its own job_failed / job_complete; a wrapper
    catching a late exception must not add a second, contradicting event.
    The DB write itself is unchanged from before the fix."""
    from backend.agents.job_helper import fail_job
    jid = f"fj-term-{prior}"
    await _make_job(jid, prior)
    await fail_job(jid, "late error", "local")
    assert (await _status(jid))[0] == "failed"  # same as update_job_status did
    assert _failed_events(sent, jid) == []


@pytest.mark.asyncio
async def test_cancelled_job_stays_cancelled_and_silent(sent):
    """The cancel wins the status, the failure is kept as the reason."""
    from backend.agents.job_helper import fail_job
    await _make_job("fj-cancel", "cancelled")
    await fail_job("fj-cancel", "ffmpeg died after cancel", "local")
    status, err = await _status("fj-cancel")
    assert status == "cancelled"
    assert err == "ffmpeg died after cancel"
    assert _failed_events(sent, "fj-cancel") == []


@pytest.mark.asyncio
async def test_missing_row_is_still_announced(sent):
    """A card the UI is tracking must be able to clear even if the row is gone."""
    from backend.agents.job_helper import fail_job
    await fail_job("fj-missing", "boom", "local")
    assert len(_failed_events(sent, "fj-missing")) == 1


# ── an agent early exit + a wrapper ──────────────────────────────────────

@pytest.mark.asyncio
async def test_generator_missing_source_announces_failure(sent):
    """An early exit inside the generator: the source row is gone."""
    from backend.agents.generator import GeneratorAgent
    await _make_job("fj-gen", "pending")
    await GeneratorAgent().run(job_id="fj-gen", downloaded_video_id="dv-does-not-exist",
                               user_id="local")
    status, err = await _status("fj-gen")
    assert (status, err) == ("failed", "Source video not found")
    events = _failed_events(sent, "fj-gen")
    assert len(events) == 1 and events[0]["error"] == "Source video not found"


@pytest.mark.asyncio
async def test_wrapper_announces_when_the_agent_raises(sent, monkeypatch):
    import backend.agents.scout as scout_mod
    from backend.core.task_runner import run_scout

    async def explode(self, **kw):
        raise RuntimeError("platform exploded")

    monkeypatch.setattr(scout_mod.ScoutAgent, "run", explode)
    await _make_job("fj-wrap", "running", job_type="scout")
    await run_scout("fj-wrap", "cooking", ["youtube"], "local")
    assert (await _status("fj-wrap"))[0] == "failed"
    assert len(_failed_events(sent, "fj-wrap")) == 1


# ── drift guard ───────────────────────────────────────────────────────────

# Files whose runners report failure to the UI themselves. A bare
# `update_job_status(job_id, "failed"` in them, with no job_failed / _tool_fail
# within the next few lines, is the silent-failure bug coming back.
GUARDED = [
    *sorted((REPO / "backend/agents").glob("*.py")),
    *sorted((REPO / "backend/core/tasks").glob("*.py")),
    REPO / "backend/core/task_runner.py",
    REPO / "backend/core/tool_runners.py",
]


def test_no_silent_failed_writes():
    pat = re.compile(r'(update_job_status|_update|_u)\(\s*job_id\s*,\s*"failed"')
    offenders = []
    for p in GUARDED:
        lines = p.read_text().splitlines()
        for i, line in enumerate(lines):
            if not pat.search(line) or line.lstrip().startswith("#"):
                continue
            window = "\n".join(lines[max(0, i - 3): i + 10])
            if "job_failed" in window or "_tool_fail" in window:
                continue
            offenders.append(f"{p.relative_to(REPO)}:{i + 1}: {line.strip()}")
    assert not offenders, (
        "silent `failed` write — the UI never hears about it (use "
        "job_helper.fail_job, or send job_failed yourself):\n  " + "\n  ".join(offenders)
    )
