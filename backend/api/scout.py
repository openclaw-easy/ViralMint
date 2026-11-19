# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""REST /api/scout — start scouting + retrieve results."""
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.scout_result import ScoutResult
from backend.agents.job_helper import create_job

logger = logging.getLogger(__name__)
router = APIRouter()


class ScoutStartRequest(BaseModel):
    niche: str
    platforms: list[str] = ["youtube"]


@router.post("/scout/start")
async def start_scout(req: ScoutStartRequest):
    """Start a scout job (runs in-process as async background task)."""
    from backend.core.task_runner import run_scout, dispatch
    job = await create_job("scout", "local", {"niche": req.niche, "platforms": req.platforms})
    dispatch(run_scout(job_id=job.id, niche=req.niche, platforms=req.platforms, user_id="local"))
    return {"job_id": job.id}


@router.get("/scout/results")
async def get_scout_results(
    job_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Get scout results, optionally filtered by job_id."""
    async with AsyncSessionLocal() as db:
        query = (
            select(ScoutResult)
            .where(ScoutResult.user_id == "local")
            .order_by(ScoutResult.created_at.desc())
        )
        if job_id:
            query = query.where(ScoutResult.job_id == job_id)
        query = query.offset(offset).limit(limit)

        result = await db.execute(query)
        results = result.scalars().all()

        # Count total
        from sqlalchemy import func
        count_query = select(func.count(ScoutResult.id)).where(ScoutResult.user_id == "local")
        if job_id:
            count_query = count_query.where(ScoutResult.job_id == job_id)
        total = (await db.execute(count_query)).scalar()

    return {
        "total": total,
        "results": [
            {
                "id": r.id,
                "platform": r.platform,
                "video_id": r.video_id,
                "video_url": r.video_url,
                "embed_url": r.embed_url,
                "title": r.title,
                "description": r.description,
                "author": r.author,
                "author_url": r.author_url,
                "thumbnail_url": r.thumbnail_url,
                "views": r.views,
                "likes": r.likes,
                "comments": r.comments,
                "shares": r.shares,
                "duration_seconds": r.duration_seconds,
                "upload_date": r.upload_date.isoformat() if r.upload_date else None,
                "virality_score": r.virality_score,
                "niche": r.niche,
                "is_downloaded": r.is_downloaded,
                "is_analyzed": r.is_analyzed,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in results
        ],
    }


@router.get("/scout/results/{result_id}")
async def get_scout_result(result_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ScoutResult).where(ScoutResult.id == result_id))
        r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Scout result not found")
    return {
        "id": r.id,
        "platform": r.platform,
        "video_id": r.video_id,
        "video_url": r.video_url,
        "embed_url": r.embed_url,
        "title": r.title,
        "description": r.description,
        "author": r.author,
        "thumbnail_url": r.thumbnail_url,
        "views": r.views,
        "likes": r.likes,
        "comments": r.comments,
        "virality_score": r.virality_score,
        "niche": r.niche,
    }


class DownloadRequest(BaseModel):
    scout_result_ids: list[str]


@router.post("/scout/download")
async def start_download(req: DownloadRequest):
    """Start a download + analyze job for selected scout results."""
    if not req.scout_result_ids:
        raise HTTPException(status_code=400, detail="No results selected")
    if len(req.scout_result_ids) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 videos per batch")
    from backend.core.task_runner import run_download, dispatch
    job = await create_job("download", "local", {"scout_result_ids": req.scout_result_ids})
    dispatch(run_download(job_id=job.id, scout_result_ids=req.scout_result_ids, user_id="local"))
    return {"job_id": job.id, "count": len(req.scout_result_ids)}


@router.get("/scout/suggest")
async def suggest_keywords(q: str = "", lang: str = "en"):
    """YouTube search autocomplete suggestions — free, no API key needed."""
    if not q or len(q.strip()) < 2:
        return {"suggestions": []}
    from backend.services.youtube_suggest_service import get_suggestions
    suggestions = await get_suggestions(q.strip(), language=lang, max_results=10)
    return {"query": q, "suggestions": suggestions}


@router.get("/scout/search-demand")
async def search_demand(niche: str = "", lang: str = "en"):
    """Full search demand analysis for a niche — reveals what users are searching for."""
    if not niche or len(niche.strip()) < 2:
        raise HTTPException(status_code=400, detail="Niche must be at least 2 characters")
    from backend.services.youtube_suggest_service import get_search_demand
    return await get_search_demand(niche.strip(), language=lang)


@router.get("/scout/keyword-score")
async def keyword_score(keyword: str):
    """Score a keyword/niche for content opportunity (volume vs competition)."""
    if not keyword or len(keyword.strip()) < 2:
        raise HTTPException(status_code=400, detail="Keyword must be at least 2 characters")
    from backend.services.keyword_score_service import score_keyword
    result = await score_keyword(keyword.strip())
    return result


@router.get("/scout/rising-keywords")
async def rising_keywords(niche: str = ""):
    """Discover rising keywords related to a niche using Google Trends."""
    if not niche or len(niche.strip()) < 2:
        raise HTTPException(status_code=400, detail="Niche must be at least 2 characters")
    from backend.services.keyword_score_service import discover_rising_keywords
    results = await discover_rising_keywords(niche.strip())
    return {"niche": niche, "keywords": results, "count": len(results)}


@router.get("/scout/cross-platform")
async def cross_platform_check(keyword: str = ""):
    """Check a keyword across Google Trends, YouTube, and Reddit for cross-platform momentum."""
    if not keyword or len(keyword.strip()) < 2:
        raise HTTPException(status_code=400, detail="Keyword must be at least 2 characters")
    from backend.services.trend_velocity_service import cross_platform_correlation
    return await cross_platform_correlation(keyword.strip())


@router.get("/scout/trend-velocity")
async def check_trend_velocity(keyword: str = None):
    """Check trend velocity for a keyword or all user's tracked niches."""
    if keyword:
        from backend.services.trend_velocity_service import check_keyword_velocity
        alert = await check_keyword_velocity(keyword.strip())
        return {
            "keyword": alert.keyword,
            "current_interest": alert.current_interest,
            "baseline_interest": alert.baseline_interest,
            "velocity": alert.velocity_multiplier,
            "alert_level": alert.alert_level,
            "note": alert.note,
        }
    else:
        from backend.services.trend_velocity_service import check_user_keywords_velocity
        readings = [
            {
                "keyword": a.keyword,
                "current_interest": a.current_interest,
                "baseline_interest": a.baseline_interest,
                "velocity": a.velocity_multiplier,
                "alert_level": a.alert_level,
                "note": a.note,
            }
            for a in await check_user_keywords_velocity()
        ]
        # `readings`: every niche checked, a failed one says `unknown` + its note. `alerts`: the rising ones, as before.
        return {"readings": readings, "alerts": [r for r in readings if r["alert_level"] in ("spike", "rising")]}


@router.delete("/scout/results/{result_id}")
async def delete_scout_result(result_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ScoutResult).where(ScoutResult.id == result_id))
        r = result.scalar_one_or_none()
        if not r:
            raise HTTPException(status_code=404, detail="Scout result not found")
        await db.delete(r)
        await db.commit()
    return {"message": "Deleted"}


@router.post("/scout/viral-formula")
async def generate_formula(body: dict = None):
    """
    Generate a cross-video viral formula for a niche.
    Body: { "niche": "personal finance", "video_ids": ["id1", "id2", ...] }
    If video_ids not provided, uses all analyzed videos matching the niche.
    Requires at least 3 analyzed videos.
    """
    import json
    from backend.models.downloaded_video import DownloadedVideo
    from backend.models.viral_formula import ViralFormula
    from backend.models.user_settings import UserSettings
    from backend.core.ai_provider import get_ai_client
    from backend.services.viral_formula_service import generate_viral_formula
    from sqlalchemy import and_

    body = body or {}
    niche = body.get("niche", "").strip()
    video_ids = body.get("video_ids")

    if not niche:
        raise HTTPException(status_code=400, detail="Niche is required")

    async with AsyncSessionLocal() as db:
        if video_ids:
            result = await db.execute(
                select(DownloadedVideo).where(
                    and_(DownloadedVideo.id.in_(video_ids), DownloadedVideo.insights_json != None)
                )
            )
        else:
            # Find all analyzed videos in this niche via scout results
            from backend.models.scout_result import ScoutResult
            result = await db.execute(
                select(DownloadedVideo)
                .join(ScoutResult, ScoutResult.id == DownloadedVideo.scout_result_id)
                .where(
                    and_(
                        ScoutResult.niche.ilike(f"%{niche}%"),
                        DownloadedVideo.insights_json != None,
                    )
                )
                .limit(15)
            )
        videos = result.scalars().all()

    if len(videos) < 3:
        raise HTTPException(
            status_code=400,
            detail=f"Need at least 3 analyzed videos for a viral formula (found {len(videos)}). "
                   f"Download and analyze more videos in the '{niche}' niche first."
        )

    # Parse insights
    analyses = []
    used_ids = []
    for v in videos:
        try:
            insights = json.loads(v.insights_json)
            analyses.append(insights)
            used_ids.append(v.id)
        except (json.JSONDecodeError, TypeError):
            continue

    if len(analyses) < 3:
        raise HTTPException(status_code=400, detail="Not enough valid analyses found")

    # Get AI client
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(UserSettings).where(UserSettings.user_id == "local"))
        user_settings = result.scalar_one_or_none()

    ai = get_ai_client(user_settings)
    formula = await generate_viral_formula(niche, analyses, ai)

    if not formula:
        raise HTTPException(status_code=500, detail="Failed to generate viral formula")

    # Save to DB
    async with AsyncSessionLocal() as db:
        vf = ViralFormula(
            niche=niche,
            formula_json=json.dumps(formula),
            video_count=len(analyses),
            source_video_ids_json=json.dumps(used_ids),
        )
        db.add(vf)
        await db.commit()
        await db.refresh(vf)

    return {
        "id": vf.id,
        "niche": niche,
        "formula": formula,
        "video_count": len(analyses),
        "created_at": vf.created_at.isoformat(),
    }


@router.get("/scout/viral-formulas")
async def list_formulas(niche: str = None):
    """List saved viral formulas, optionally filtered by niche."""
    import json
    from backend.models.viral_formula import ViralFormula

    async with AsyncSessionLocal() as db:
        query = select(ViralFormula).order_by(ViralFormula.created_at.desc()).limit(20)
        if niche:
            query = query.where(ViralFormula.niche.ilike(f"%{niche}%"))
        result = await db.execute(query)
        formulas = result.scalars().all()

    return {
        "formulas": [
            {
                "id": f.id,
                "niche": f.niche,
                "formula": json.loads(f.formula_json),
                "video_count": f.video_count,
                "created_at": f.created_at.isoformat(),
            }
            for f in formulas
        ]
    }
