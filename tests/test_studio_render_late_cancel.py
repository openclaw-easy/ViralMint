# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A cancel that lands after render v2's last gate.

The video is written by then. The job must stay `cancelled` (the user asked),
but keep the link to the video, and the page must not be told "Video ready!"
— otherwise a headless poller sees a cancelled job with no output and renders
again."""
import asyncio
import json

from backend.agents import job_helper
from backend.core.tasks import studio as studio_tasks
from backend.core.ws_manager import ws_manager
from backend.database import AsyncSessionLocal, init_db
from backend.models.job import Job
from backend.services import studio_render
from backend.services import studio_store as store

U = "late-cancel-test"


def test_a_cancel_after_the_video_exists_keeps_the_video_on_the_cancelled_job(monkeypatch):
    sent, notified = [], []

    async def scenario():
        await init_db()
        job = await job_helper.create_job("generate", user_id=U)

        async def render(job_id, project_id, user_id):
            await job_helper.update_job_status(job_id, "cancelled")           # the user's cancel, during the last step
            return studio_render.RenderDone(result={"video": {"id": "gv-1"}}, output={"generated_video_id": "gv-1"},
                                            after_complete=lambda: notified.append(True))

        async def send(message, user_id=None):
            sent.append(message)

        async def finish(project_id, **kw):
            return None

        monkeypatch.setattr(studio_render, "render", render)
        monkeypatch.setattr(ws_manager, "send", send)
        monkeypatch.setattr(store, "finish_render", finish)
        await studio_tasks.run_studio_render(job.id, "p_late", U)
        async with AsyncSessionLocal() as db:
            return await db.get(Job, job.id)

    row = asyncio.run(scenario())
    assert row.status == "cancelled"
    assert json.loads(row.output_json or "{}").get("generated_video_id") == "gv-1"
    assert row.completed_at is not None
    assert not [m for m in sent if m.get("type") == "job_complete"]
    assert not notified                                        # no "Video ready" notification either
