# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""REST /api/jobs — job CRUD + status."""
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import select, update

from backend.database import AsyncSessionLocal
from backend.models.job import Job
from backend.agents.job_helper import update_job_status
from backend.core.task_runner import runner_active

logger = logging.getLogger(__name__)
router = APIRouter()


class JobResponse(BaseModel):
    id: str
    job_type: str
    status: str
    title: Optional[str] = None
    progress_pct: float = 0.0
    current_step: Optional[str] = None
    error_message: Optional[str] = None
    input_json: Optional[str] = None
    output_json: Optional[str] = None
    estimated_cost_usd: float = 0.0
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


@router.get("/jobs")
async def list_jobs(
    status: Optional[str] = None,
    job_type: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
):
    """List jobs, optionally filtered by status and/or type.

    `status` accepts a COMMA-SEPARATED list ("running,pending") as well as a
    single value. The frontend's active-job restore wants exactly those two and
    was issuing one request per status on every page load; one filter answers
    both. A single value still means what it always did, so every existing
    caller is unaffected.
    """
    async with AsyncSessionLocal() as db:
        statuses = [s.strip() for s in (status or "").split(",") if s.strip()]

        # Built once and applied to BOTH the page query and the count. They
        # used to be written out separately, which is the shape where a filter
        # added to one silently makes `total` disagree with the rows.
        filters = [Job.user_id == "local"]
        if statuses:
            filters.append(Job.status.in_(statuses))
        if job_type:
            filters.append(Job.job_type == job_type)

        base = select(Job).where(*filters)

        from sqlalchemy import func
        total = (await db.execute(
            select(func.count(Job.id)).where(*filters)
        )).scalar()

        query = base.order_by(Job.created_at.desc()).offset(offset).limit(limit)
        result = await db.execute(query)
        jobs = result.scalars().all()

    return {
        "total": total,
        "jobs": [
            {
                "id": j.id,
                "job_type": j.job_type,
                "status": j.status,
                "title": j.title,
                "progress_pct": j.progress_pct,
                "current_step": j.current_step,
                "error_message": j.error_message,
                "input_json": j.input_json,
                "output_json": j.output_json,
                "estimated_cost_usd": j.estimated_cost_usd,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in jobs
        ]
    }


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Get a single job by ID."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Job).where(Job.id == job_id))
        j = result.scalar_one_or_none()

    if not j:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "id": j.id,
        "job_type": j.job_type,
        "status": j.status,
        "title": j.title,
        "progress_pct": j.progress_pct,
        "current_step": j.current_step,
        "error_message": j.error_message,
        "input_json": j.input_json,
        "output_json": j.output_json,
        "estimated_cost_usd": j.estimated_cost_usd,
        # True while the job's runner is still executing in this process —
        # including AFTER a cancel flipped `status` to "cancelled". The one
        # field that says when in-flight work has actually ended; wait on it
        # before starting a replacement for a cancelled job.
        "runner_active": runner_active(j.id),
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
        "created_at": j.created_at.isoformat() if j.created_at else None,
    }


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """Cancel a running job or delete a completed one."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Job).where(Job.id == job_id))
        j = result.scalar_one_or_none()

        if not j:
            raise HTTPException(status_code=404, detail="Job not found")

        if j.status in ("running", "pending"):
            # A runner that was admitted but has not yet written "running" is
            # already doing work — count it as running, or its cancel would
            # claim a completeness it does not have.
            was_running = j.status == "running" or runner_active(job_id)
            # Conditional flip: only a job that is STILL live gets cancelled.
            # A plain read-then-assign overwrote a job that finished in
            # between — the user got "cancelled" for work that had already
            # succeeded and been reported as complete.
            flipped = await db.execute(
                update(Job)
                .where(Job.id == job_id, Job.status.in_(("running", "pending")))
                .values(status="cancelled")
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            if flipped.rowcount == 0:
                await db.refresh(j)
                return {
                    "message": f"Job already finished ({j.status}) — nothing to cancel",
                    "cancelled": False,
                    "best_effort": False,
                    "status": j.status,
                }
            # Cancellation is COOPERATIVE, and the response has to admit it.
            #
            # Flipping the row is instant. Stopping the work is not: yt-dlp
            # transfers and ffmpeg passes run in threads Python cannot
            # interrupt, so a running job finishes whatever step is already in
            # flight (the row stays "cancelled"). A flat "Job cancelled" reads
            # as a completed stop — a user saw the download land minutes later
            # and had already started a duplicate by then.
            #
            # `best_effort` is False for a PENDING job on purpose: nothing had
            # started, so that cancel really is complete. Always reporting
            # best_effort would be its own inaccuracy and would train clients
            # to ignore the field.
            if was_running:
                return {
                    "message": "Job cancelled — a step already in flight may still finish",
                    "cancelled": True,
                    "best_effort": True,
                    "detail": (
                        "The job is marked cancelled and no further steps will "
                        "start. Work already running (a download transfer, an "
                        "ffmpeg pass) cannot be interrupted and will finish on "
                        "its own. Poll the job until `runner_active` is false "
                        "before starting a replacement, or you may end up with "
                        "a duplicate."
                    ),
                }
            return {
                "message": "Job cancelled before it started",
                "cancelled": True,
                "best_effort": False,
            }

        # A cancelled job whose runner is still finishing its in-flight step
        # keeps its row: the runner is about to write the outcome there — what
        # landed, or why it failed — and deleting it first throws that away.
        if runner_active(j.id):
            raise HTTPException(
                status_code=409,
                detail="That job is still finishing the step it was on when you "
                       "cancelled it — remove it once it stops.",
            )

        # A successful tool run is not a log entry — it IS the Library item for
        # the file it wrote, and the asset endpoint resolves that file BY THIS
        # ROW's id. Deleting it here would take the user's captioned cut off
        # every surface while the bytes sat on disk forever. Point at the door
        # that removes both.
        from backend.services.job_retention import is_library_item
        if is_library_item(j):
            raise HTTPException(
                status_code=409,
                detail=("This job produced a file in your Library. Delete it from "
                        "there (that removes the file too)."),
            )

        await db.delete(j)
        await db.commit()
        return {"message": "Job deleted"}


class BulkDeleteRequest(BaseModel):
    job_ids: list[str]


@router.post("/jobs/bulk-delete")
async def bulk_delete_jobs(body: BulkDeleteRequest):
    """Delete multiple completed/failed jobs at once.

    Rows that back a file in the Library are KEPT and counted separately.
    "Clear finished" is a request to tidy the activity log, and it must never
    be a way to delete the user's videos — which is what it would be now that a
    successful tool run is the Library item for its own output.
    """
    from backend.services.job_retention import is_library_item

    deleted = 0
    cancelled = 0
    kept_library = 0
    kept_draining = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Job).where(Job.id.in_(body.job_ids)))
        jobs = result.scalars().all()
        for j in jobs:
            if j.status in ("running", "pending"):
                # Conditional, like delete_job: a job that finished between
                # the SELECT and this write keeps its real terminal status
                # instead of being rewritten as cancelled.
                flipped = await db.execute(
                    update(Job)
                    .where(Job.id == j.id, Job.status.in_(("running", "pending")))
                    .values(status="cancelled")
                    .execution_options(synchronize_session=False)
                )
                if flipped.rowcount:
                    cancelled += 1
            elif runner_active(j.id):
                # Cancelled but still draining its in-flight step — keep the
                # row so the runner's outcome lands (see delete_job).
                kept_draining += 1
            elif is_library_item(j):
                kept_library += 1
            else:
                await db.delete(j)
                deleted += 1
        await db.commit()
    return {"deleted": deleted, "cancelled": cancelled, "kept_library": kept_library,
            "kept_draining": kept_draining}
