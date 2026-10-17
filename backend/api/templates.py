# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Dynamic template generation and retrieval.
AI generates trending video templates from scout data + YouTube search demand.
"""
import json
import logging
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, delete, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.dynamic_template import DynamicTemplate
from backend.models.scout_result import ScoutResult
from backend.models.user_settings import UserSettings

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Constants ──────────────────────────────────────────────────────────────────

TEMPLATE_TTL_DAYS = 7
MAX_TEMPLATES_PER_NICHE_MODE = 4

GENERATION_PROMPT = """You are a viral video content strategist. Based on the trending data below, generate {count} unique video template ideas for the "{mode}" creation mode in the "{niche}" niche.

TRENDING DATA:
{trend_data}

MODE DETAILS:
{mode_details}

For each template, output a JSON object with these exact fields:
- "name": short catchy name (3-5 words)
- "description": one sentence explaining the format (under 80 chars)
- "icon": single emoji that represents this template
- "tags": array of 2-3 lowercase tags
- "defaults": object with:
  - "scriptInstructions": detailed script writing instructions (2-4 sentences, reference the specific trend/topic)
  - "captionStyle": one of "viral", "bold", "classic", "neon", "karaoke", "glow", "minimal"
  - "musicGenre": one of "lofi", "cinematic", "upbeat", "ambient", "corporate"
  - "aspectRatio": "9:16" for short-form, "16:9" for long-form
- "trend_score": 0-100 relevance score based on how trending this topic is

RULES:
- Templates must be SPECIFIC to current trends, not generic (e.g. "AI Job Fears 2026" not "Educational Video")
- Each template should target a different angle/format
- scriptInstructions should mention specific trending topics, keywords, or angles from the data
- Make templates feel timely — reference current events, trending searches, viral formats

Output ONLY a JSON array of {count} template objects. No markdown, no explanation."""

MODE_DETAILS = {
    "stock": "Stock Video mode uses stock footage (Pexels or Pixabay, whichever key is configured) matched to script keywords. Free, fast. Best for: educational, listicles, news commentary, explainers.",
}


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/templates")
async def list_templates(
    mode: str = Query("stock", description="stock"),
    niche: str = Query("", description="Filter by niche (empty = all niches)"),
    db: AsyncSession = Depends(get_db),
):
    """Return dynamic templates for a mode, optionally filtered by niche."""
    now = datetime.utcnow()

    q = select(DynamicTemplate).where(
        and_(
            DynamicTemplate.mode == mode,
            DynamicTemplate.expires_at > now,
        )
    )
    if niche:
        q = q.where(DynamicTemplate.niche == niche.strip().lower())

    q = q.order_by(DynamicTemplate.trend_score.desc()).limit(12)
    result = await db.execute(q)
    templates = result.scalars().all()

    return {
        "templates": [
            {
                "id": t.id,
                "name": t.name,
                "desc": t.description,
                "icon": t.icon,
                "tags": json.loads(t.tags_json or "[]"),
                "defaults": json.loads(t.defaults_json or "{}"),
                "niche": t.niche,
                "trend_source": t.trend_source,
                "trend_score": t.trend_score,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "is_trending": True,
            }
            for t in templates
        ],
    }


@router.post("/templates/refresh")
async def refresh_templates(
    mode: str = Query("stock", description="stock"),
    niche: str = Query("", description="Niche to generate for (required)"),
    db: AsyncSession = Depends(get_db),
):
    """Generate fresh trending templates for a niche + mode using AI."""

    if not niche.strip():
        return {"error": "Niche is required", "templates": []}

    niche_clean = niche.strip().lower()

    # Gather trend data (always returns something — falls back to niche description)
    try:
        trend_data = await _gather_trend_data(niche_clean, db)
    except Exception as e:
        logger.error(f"Trend data gathering failed: {e}", exc_info=True)
        trend_data = f"USER REQUESTED NICHE: {niche_clean}\n(Trend data unavailable)"

    # Generate via AI
    try:
        templates = await _generate_templates_ai(niche_clean, mode, trend_data)
    except Exception as e:
        logger.error(f"Template generation failed: {e}", exc_info=True)
        return {"error": f"AI generation failed: {str(e)}", "templates": []}

    if not templates:
        return {"message": "AI returned no templates. Try again.", "templates": []}

    # Delete old templates for this niche+mode
    await db.execute(
        delete(DynamicTemplate).where(
            and_(
                DynamicTemplate.niche == niche_clean,
                DynamicTemplate.mode == mode,
            )
        )
    )

    # Save new ones
    now = datetime.utcnow()
    expires = now + timedelta(days=TEMPLATE_TTL_DAYS)
    saved = []

    for t in templates[:MAX_TEMPLATES_PER_NICHE_MODE]:
        dt = DynamicTemplate(
            id=str(uuid4()),
            user_id="local",
            mode=mode,
            niche=niche_clean,
            name=t.get("name", "Trending Template"),
            description=t.get("description", ""),
            icon=t.get("icon", "🔥"),
            tags_json=json.dumps(t.get("tags", ["trending"])),
            defaults_json=json.dumps(t.get("defaults", {})),
            trend_source=t.get("trend_source", "search_demand"),
            trend_score=float(t.get("trend_score", 50)),
            source_data_json=json.dumps({"niche": niche_clean, "generated_at": now.isoformat()}),
            created_at=now,
            expires_at=expires,
        )
        db.add(dt)
        saved.append({
            "id": dt.id,
            "name": dt.name,
            "desc": dt.description,
            "icon": dt.icon,
            "tags": t.get("tags", ["trending"]),
            "defaults": t.get("defaults", {}),
            "niche": niche_clean,
            "trend_source": dt.trend_source,
            "trend_score": dt.trend_score,
            "is_trending": True,
        })

    await db.commit()

    logger.info(f"Generated {len(saved)} templates for '{niche_clean}' / {mode}")
    return {"templates": saved, "message": f"Generated {len(saved)} trending templates"}


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete a single dynamic template."""
    result = await db.execute(
        select(DynamicTemplate).where(DynamicTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    if not template:
        # 404, not a 200 with an error body. Every other delete in the API
        # 404s on a missing row, and the frontend checks `response.ok` — so
        # returning 200 here made a failed delete read as a success and the
        # row silently stayed in the list.
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(template)
    await db.commit()
    return {"ok": True}


@router.post("/templates/refresh-all")
async def refresh_all_templates(
    db: AsyncSession = Depends(get_db),
):
    """Refresh templates for all niches that have been scouted. Manually triggered."""
    # Find distinct niches from recent scout results
    result = await db.execute(
        select(ScoutResult.niche)
        .where(ScoutResult.niche.isnot(None))
        .distinct()
        .limit(10)
    )
    niches = [row[0] for row in result.fetchall() if row[0]]

    if not niches:
        return {"message": "No niches found. Scout some videos first."}

    total = 0
    for niche in niches:
        for mode in ["stock"]:
            try:
                trend_data = await _gather_trend_data(niche, db)
                if not trend_data:
                    continue
                templates = await _generate_templates_ai(niche, mode, trend_data)
                if not templates:
                    continue

                # Delete old
                await db.execute(
                    delete(DynamicTemplate).where(
                        and_(
                            DynamicTemplate.niche == niche,
                            DynamicTemplate.mode == mode,
                        )
                    )
                )

                now = datetime.utcnow()
                expires = now + timedelta(days=TEMPLATE_TTL_DAYS)
                for t in templates[:MAX_TEMPLATES_PER_NICHE_MODE]:
                    db.add(DynamicTemplate(
                        id=str(uuid4()),
                        user_id="local",
                        mode=mode,
                        niche=niche,
                        name=t.get("name", "Trending"),
                        description=t.get("description", ""),
                        icon=t.get("icon", "🔥"),
                        tags_json=json.dumps(t.get("tags", ["trending"])),
                        defaults_json=json.dumps(t.get("defaults", {})),
                        trend_source=t.get("trend_source", "search_demand"),
                        trend_score=float(t.get("trend_score", 50)),
                        source_data_json=json.dumps({"niche": niche}),
                        created_at=now,
                        expires_at=expires,
                    ))
                    total += 1

                await db.commit()
            except Exception as e:
                logger.error(f"Failed to refresh templates for '{niche}/{mode}': {e}")
                continue

    return {"message": f"Refreshed {total} templates across {len(niches)} niches"}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _extract_keywords(niche: str, max_words: int = 5) -> list[str]:
    """Extract short keyword phrases from a long niche description for YouTube suggest."""
    import re
    # Split on commas, 'and', 'etc', 'especially', 'about', 'like'
    chunks = re.split(r"[,;]|\b(?:and|etc|especially|about|like|such as)\b", niche, flags=re.IGNORECASE)
    keywords = []
    for chunk in chunks:
        words = chunk.strip().split()
        if not words:
            continue
        # Take meaningful words (skip filler)
        phrase = " ".join(w for w in words if len(w) > 1)[:60]
        if phrase and len(phrase) > 2:
            keywords.append(phrase)
    # Also try the first few words as a short query
    first_words = " ".join(niche.split()[:max_words])
    if first_words not in keywords:
        keywords.insert(0, first_words)
    return keywords[:6]


async def _gather_trend_data(niche: str, db: AsyncSession) -> str:
    """Collect trend signals from scout results + YouTube suggest."""
    parts = []

    # 1. Top scout results for this niche (by virality score)
    result = await db.execute(
        select(ScoutResult)
        .where(
            and_(
                ScoutResult.niche == niche,
                ScoutResult.virality_score > 30,
            )
        )
        .order_by(ScoutResult.virality_score.desc())
        .limit(15)
    )
    scouts = result.scalars().all()
    if scouts:
        lines = []
        for s in scouts:
            lines.append(
                f"- \"{s.title}\" (score: {s.virality_score:.0f}, views: {s.views:,}, "
                f"platform: {s.platform})"
            )
        parts.append("TOP TRENDING VIDEOS:\n" + "\n".join(lines))

    # 2. YouTube search suggest (free, no key)
    # Extract shorter keywords if niche is too long for the suggest API
    try:
        from backend.services.youtube_suggest_service import get_search_demand
        keywords = _extract_keywords(niche) if len(niche) > 50 else [niche]

        for kw in keywords[:3]:
            demand = await get_search_demand(kw, language="en")
            if not demand:
                continue
            suggestions = demand.get("primary_suggestions", [])[:8]
            questions = demand.get("question_keywords", [])[:6]
            long_tail = demand.get("long_tail_keywords", [])[:6]

            if suggestions:
                parts.append(f"YOUTUBE SEARCH SUGGESTIONS for '{kw}':\n" + "\n".join(f"- {s}" for s in suggestions))
            if questions:
                parts.append(f"QUESTION KEYWORDS for '{kw}':\n" + "\n".join(f"- {q}" for q in questions))
            if long_tail:
                parts.append(f"LONG-TAIL KEYWORDS for '{kw}':\n" + "\n".join(f"- {lt}" for lt in long_tail))
            if suggestions or questions or long_tail:
                break  # Got good data from one keyword, don't over-fetch
    except Exception as e:
        logger.warning(f"Search demand fetch failed: {e}")

    # 3. If no trend data at all, provide the niche itself as context
    # so the AI can still generate relevant templates
    if not parts:
        parts.append(f"USER REQUESTED NICHE: {niche}\n(No scout data or search suggestions available — generate templates based on general knowledge of this topic area)")

    return "\n\n".join(parts)


async def _generate_templates_ai(niche: str, mode: str, trend_data: str) -> list[dict]:
    """Call AI to generate templates from trend data."""
    from backend.core.ai_provider import get_ai_client
    from backend.models.user_settings import UserSettings
    from backend.database import AsyncSessionLocal

    # Load user settings for AI client
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == "local")
        )
        user_settings = result.scalar_one_or_none()

    ai = get_ai_client(user_settings)

    prompt = GENERATION_PROMPT.format(
        count=MAX_TEMPLATES_PER_NICHE_MODE,
        mode=mode,
        niche=niche,
        trend_data=trend_data[:4000],  # cap to avoid token overflow
        mode_details=MODE_DETAILS.get(mode, ""),
    )

    response = await ai.chat(
        messages=[{"role": "user", "content": prompt}],
        system="You are a viral video strategist. Output ONLY valid JSON arrays. No markdown fences.",
        max_tokens=2048,
    )

    # Parse JSON from response
    import re
    text = response.strip()
    # Strip markdown fences if AI adds them despite instructions
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        text = text.strip()

    try:
        templates = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON array in the text
        arr_match = re.search(r'\[.*\]', text, re.DOTALL)
        if arr_match:
            templates = json.loads(arr_match.group(0))
        else:
            logger.warning(f"Could not parse AI template response: {text[:300]}")
            return []
    if not isinstance(templates, list):
        templates = [templates]

    # Validate + enrich
    valid = []
    for t in templates:
        if not isinstance(t, dict) or "name" not in t:
            continue
        # Ensure defaults has scriptInstructions
        defaults = t.get("defaults", {})
        if not defaults.get("scriptInstructions"):
            continue
        t["trend_source"] = "search_demand"
        valid.append(t)

    return valid
