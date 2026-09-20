# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Utility for creating job records in the database."""
import json
import logging

from backend.database import AsyncSessionLocal
from backend.models.job import Job

logger = logging.getLogger(__name__)

# ── Behavior instrumentation ────────────────────────────────────────────────
# Map a completed job's `job_type` -> a UserBehavior event so the
# personalization engine (profile synthesis, smart-suggestions, pipeline_state
# awareness) can learn what the user ACTUALLY does. Without this the whole
# intelligence layer reads `video_generated` / `video_downloaded` /
# `video_uploaded` events that nothing ever writes, so it runs on empty.
# Hooking here means every runner that reaches update_job_status(..., "success")
# is instrumented automatically, with no per-runner edits to drift out of sync.
_JOB_EVENT_MAP = {
    "generate": "video_generated",
    "download": "video_downloaded",
    "download_url": "video_downloaded",
    "batch_download": "video_downloaded",
    "clip_extraction": "clip_extracted",
    "analyze": "video_analyzed",
    "analyze_imported": "video_analyzed",
    "analyze_channel": "channel_analyzed",
}

# Keys worth carrying onto the behavior event (compact; drives the profile +
# suggestions). Pulled defensively from the job's input/output JSON.
_EVENT_DATA_KEYS = (
    "generated_video_id", "downloaded_video_id", "source_id", "source_video_id",
    "niche", "aspect_ratio", "gen_tier", "visual_style", "count", "clip_count",
    "platform",
)

_TERMINAL = {"success", "failed", "cancelled"}


async def _record_job_behavior(job_type: str, user_id: str, input_json: str, output_json: str):
    """Best-effort: record a UserBehavior event for a completed job. Never
    raises into the job-status path — telemetry must not break completion."""
    try:
        if job_type and job_type.startswith("tool:"):
            event_type = "tool_used"
        elif job_type and job_type.startswith("upload"):
            # OSS ships the uploader — uploads are first-class funnel events.
            event_type = "video_uploaded"
        else:
            event_type = _JOB_EVENT_MAP.get(job_type)
        if not event_type:
            return

        data: dict = {}
        for blob in (input_json, output_json):
            try:
                parsed = json.loads(blob) if blob else {}
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, dict):
                for k in _EVENT_DATA_KEYS:
                    if k in parsed and k not in data and parsed[k] is not None:
                        data[k] = parsed[k]
        if job_type.startswith("tool:"):
            data["tool"] = job_type[len("tool:"):]

        from backend.core.user_intelligence import UserIntelligence
        await UserIntelligence().record_event(event_type, data, user_id=user_id or "local")
    except Exception:
        logger.debug("behavior-event record failed for job_type=%s", job_type, exc_info=True)


async def create_job(job_type: str, user_id: str = "local", input_data: dict = None) -> Job:
    """Create a new Job row and return it."""
    async with AsyncSessionLocal() as db:
        job = Job(
            user_id=user_id,
            job_type=job_type,
            status="pending",
            input_json=json.dumps(input_data) if input_data else None,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job


async def update_job_status(
    job_id: str,
    status: str,
    progress_pct: float = None,
    current_step: str = None,
    error_message: str = None,
    output_data: dict = None,
):
    """Update a job's status and optional fields."""
    from datetime import datetime
    import asyncio
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if not job:
            return
        # Terminal-state guard. A finalised job (success/failed/cancelled) must
        # never be reverted to "running" by a late progress update — this
        # matters now that ws_manager.send_progress also writes the DB, so a
        # stray progress event arriving after termination would otherwise
        # resurrect the row (and hand the zombie sweep a fresh heartbeat on a
        # job nobody is running). Updates AMONG terminal states are still
        # allowed, which is what lets a mis-swept job self-heal: a draining
        # predecessor's final "success" still lands over the sweep's "failed".
        if job.status in _TERMINAL and status not in _TERMINAL:
            return
        # ...with ONE exception: a user's cancel must never become "success".
        # Cancellation is cooperative (DELETE /api/jobs/{id} only flips the
        # row), so a runner that polls `job_cancelled()` too late — or, as the
        # download family did, not at all — reaches its unconditional terminal
        # write and turns the user's "cancelled" back into "success", complete
        # with a job_complete event and a completion notification. That is what
        # made cancelling a download look like it had done nothing.
        #
        # Scoped deliberately narrow. failed→success still lands, because
        # database.sweep_stale_jobs depends on exactly that to self-heal a
        # mis-swept job whose draining predecessor later finishes; and
        # cancelled→failed still lands, so a job that cancels and then errors
        # is not silently "clean". Only the transition that contradicts the
        # user is refused. The per-runner polls remain the primary mechanism —
        # they also stop the work — and this is the backstop for runners that
        # forget.
        if job.status == "cancelled" and status == "success":
            logger.info(
                "Refusing to overwrite cancelled job %s with success — "
                "the user cancelled it (job_type=%s)", job_id[:8], job.job_type,
            )
            return
        prev_status = job.status
        job.status = status
        # Heartbeat: touch on EVERY accepted update, even when no other column
        # changes (a progress tick rejected by the regression guard just below
        # would otherwise produce no UPDATE at all, and `onupdate` would not
        # fire). The boot-time sweep uses this to tell a job that is alive in a
        # draining predecessor instance from one that died with the old
        # process — see database.sweep_stale_jobs.
        job.updated_at = datetime.utcnow()
        if progress_pct is not None:
            # Prevent progress regression (e.g. 95% → 50%) unless explicitly restarting
            is_restart = status == "running" and progress_pct == 0
            if is_restart or progress_pct >= (job.progress_pct or 0):
                job.progress_pct = progress_pct
        if current_step is not None:
            job.current_step = current_step
        if error_message is not None:
            job.error_message = error_message
        if output_data is not None:
            job.output_json = json.dumps(output_data)
        if status == "running" and not job.started_at:
            job.started_at = datetime.utcnow()
        if status in ("success", "failed", "cancelled"):
            job.completed_at = datetime.utcnow()
        await db.commit()

        # On the FIRST transition to success, emit a behavior event so the
        # personalization engine learns this action happened. Fire-and-forget;
        # never blocks or fails job completion. (Args evaluated now, while the
        # session is open, so no detached-instance access later.)
        if status == "success" and prev_status not in _TERMINAL:
            asyncio.create_task(_record_job_behavior(
                job.job_type, job.user_id, job.input_json, job.output_json,
            ))


async def job_cancelled(job_id: str | None) -> bool:
    """True when the job row has been flipped to "cancelled" by the user.

    Cancellation (DELETE /api/jobs/{id} or the WS job_cancel message) only
    updates the DB row — it never interrupts the running coroutine. Long
    pipelines poll this at phase boundaries (after Whisper, after AI
    selection, before the ffmpeg fan-out) and raise JobCancelledError so a
    cancelled job stops burning CPU and its "cancelled" status isn't
    overwritten by a late terminal write.

    `None` / unknown ids return False — a pipeline invoked without a job
    (direct service call, tests) must never think it was cancelled. DB errors
    also return False: cancellation is best-effort, and a transient read
    failure must not kill a healthy job.
    """
    if not job_id:
        return False
    try:
        async with AsyncSessionLocal() as db:
            from sqlalchemy import select
            row = (await db.execute(
                select(Job.status).where(Job.id == job_id)
            )).scalar_one_or_none()
        return row == "cancelled"
    except Exception:
        return False
