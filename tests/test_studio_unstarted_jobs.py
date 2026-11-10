# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A studio job cancelled while it queued for a slot never runs its `finally`. Whatever the route took
for it (a render claim, a plan's seat, a `generating` scene) has to be given back, or the project stays
locked until a restart."""
import asyncio
import json

import pytest

from backend.core import task_runner
from backend.core.tasks import studio as tasks
from backend.services import studio_store as store
from tests.test_studio_render import client, env, make_project, media  # noqa: F401 — fixtures


def _job(job_type="generate") -> str:
    from backend.agents.job_helper import cancel_if_live, create_job

    async def go():
        job = await create_job(job_type, "local", {}, title="x")
        assert await cancel_if_live(job.id)
        return job.id
    return asyncio.run(go())


def _drop_queued(coro) -> None:
    """The dispatcher's own path: the job was cancelled while queued, so the runner never starts."""
    asyncio.run(task_runner._run_with_limit(coro, asyncio.Semaphore(1)))


def _row(job_id):
    from backend.database import AsyncSessionLocal
    from backend.models.job import Job

    async def go():
        async with AsyncSessionLocal() as db:
            return await db.get(Job, job_id)
    return asyncio.run(go())


@pytest.fixture
def project(client, env):
    return make_project(client)


def _render_is_free(project_id):
    assert not store.render_running_here(project_id)
    asyncio.run(store.claim_render(project_id))                 # the next render isn't refused
    asyncio.run(store.finish_render(project_id))
    store.release_render(project_id)


def test_a_queued_render_gives_its_claim_back(project):
    job = _job()
    asyncio.run(store.claim_render(project["id"]))
    _drop_queued(tasks.run_studio_render(job, project["id"], "local"))
    _render_is_free(project["id"])
    assert _row(job).completed_at is not None


def test_a_queued_stock_generate_gives_its_claim_back(project):
    job = _job()
    asyncio.run(store.claim_render(project["id"]))
    _drop_queued(tasks.run_studio_from_legacy(job, project["id"], {"script": "x"}, "local"))
    _render_is_free(project["id"])
    assert _row(job).completed_at is not None


def test_a_queued_plan_gives_its_seat_back(project):
    job = _job("studio_plan")
    tasks.PLANS_IN_FLIGHT[project["id"]] = job
    _drop_queued(tasks.run_studio_plan(job, project["id"], "local", False, None))
    assert project["id"] not in tasks.PLANS_IN_FLIGHT
    assert _row(job).completed_at is not None


def test_a_queued_plan_leaves_a_newer_plans_seat_alone(project):
    job = _job("studio_plan")
    tasks.PLANS_IN_FLIGHT[project["id"]] = "newer"
    _drop_queued(tasks.run_studio_plan(job, project["id"], "local", False, None))
    assert tasks.PLANS_IN_FLIGHT.pop(project["id"]) == "newer"


def test_queued_voices_settle_their_scenes(project):
    job = _job("studio_voice")
    uid = project["doc"]["scenes"][0]["uid"]
    asyncio.run(store.write_ops(project["id"], [
        {"op": "voice_result", "uid": uid, "voice": {"state": "generating"}},
    ], if_revision=None, internal=True))
    _drop_queued(tasks.run_studio_voice(job, project["id"], [uid], "local"))
    doc = json.loads(asyncio.run(store.get_project(project["id"])).doc_json)
    assert doc["scenes"][0]["voice"]["state"] != "generating"


def test_a_runner_that_started_is_not_settled_twice():
    """Its own finally already ran: the cleanup must not run on top of it."""
    seen = []

    async def run_probe(job_id):
        seen.append("ran")

    async def cleanup(**kw):
        seen.append("cleanup")
    task_runner.on_unstarted("run_probe")(cleanup)
    try:
        asyncio.run(task_runner._run_with_limit(run_probe("not-a-job"), asyncio.Semaphore(1)))
    finally:
        task_runner._UNSTARTED_CLEANUPS.pop("run_probe", None)
    assert seen == ["ran"]


def test_a_failing_cleanup_never_escapes_the_dispatcher():
    async def run_probe2(job_id):
        pass

    async def cleanup(**kw):
        raise RuntimeError("boom")

    coro = run_probe2("x")
    task_runner.on_unstarted("run_probe2")(cleanup)
    try:
        asyncio.run(task_runner._settle_unstarted(coro))      # closes it, runs the cleanup, swallows the error
    finally:
        task_runner._UNSTARTED_CLEANUPS.pop("run_probe2", None)
