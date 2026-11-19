# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Intelligence tools — keyword, trend and viral-formula research.

Read-mostly helpers grounded in public trend data (Google Trends, YouTube
search) and the user's own analyzed downloads, so an agent can advise
BEFORE it acts. Wraps /api/scout/keyword-score, /rising-keywords,
/trend-velocity, /viral-formula(s).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from backend.mcp import client


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def score_keyword(keyword: str) -> dict:
        """Score a keyword/niche for content opportunity (search volume vs. competition).

        Volume and trend direction come from Google Trends; competition from
        a YouTube search count (uses the YouTube API key from Settings when
        set, otherwise a neutral estimate). Use it to pick winnable angles.

        Args:
            keyword: The keyword/niche to score (at least 2 characters).

        Returns:
            {"keyword": str, "opportunity_score": int, "search_volume": str,
             "competition": str, "trend_direction": "rising"|"stable"|...,
             "volume_index": int, "competition_index": int,
             "related_keywords": [str, ...]}
        """
        k = (keyword or "").strip()
        if len(k) < 2:
            raise ValueError("keyword must be at least 2 characters")
        return await client.get("/api/scout/keyword-score", params={"keyword": k})

    @mcp.tool()
    async def rising_keywords(niche: str) -> dict:
        """Discover RISING search terms related to a niche via Google Trends.

        The freshest angles to make content about — queries whose search
        interest is growing fastest right now.

        Args:
            niche: The seed niche (at least 2 characters).

        Returns:
            {"niche": str, "keywords": [{"keyword": str, "growth_pct": int,
             ...}, ...], "count": int}. growth_pct 5000 means Google Trends
             labelled it "Breakout".
        """
        n = (niche or "").strip()
        if len(n) < 2:
            raise ValueError("niche must be at least 2 characters")
        return await client.get("/api/scout/rising-keywords", params={"niche": n})

    @mcp.tool()
    async def trend_velocity(keyword: Optional[str] = None) -> dict:
        """How fast a keyword's search interest is rising or falling right now.

        Omit `keyword` to check every niche the user has scouted.

        Args:
            keyword: The keyword to check, or omit for all tracked niches.

        Returns:
            With keyword: {"keyword": str, "current_interest": float,
             "baseline_interest": float, "velocity": float (current ÷
             baseline), "alert_level": str, "note": str}.
            Without: {"readings": [<same shape>, ...], "alerts": [...]} for
             the five most recently scouted niches; "alerts" keeps only the
             rising ones. alert_level is "spike" | "rising" | "steady" |
             "declining" | "unknown"; "unknown" means no reading, and note
             says why: "lookup_failed" (Google Trends refused or errored) or
             "no_data" (too little search volume). Never read "unknown" as a
             quiet trend.
        """
        params = {"keyword": keyword.strip()} if (keyword or "").strip() else {}
        return await client.get("/api/scout/trend-velocity", params=params)

    @mcp.tool()
    async def list_viral_formulas(niche: Optional[str] = None) -> dict:
        """List saved viral formulas — hook / structure / pacing recipes distilled from top videos.

        Read-only; returns the 20 newest, optionally filtered by niche
        (substring match).

        Args:
            niche: Optional niche filter.

        Returns:
            {"formulas": [{"id": str, "niche": str, "formula": {...},
             "video_count": int, "created_at": str}, ...]}
        """
        params = {"niche": niche.strip()} if (niche or "").strip() else {}
        return await client.get("/api/scout/viral-formulas", params=params)

    @mcp.tool()
    async def generate_viral_formula(
        niche: str, video_ids: Optional[list[str]] = None
    ) -> dict:
        """Distill a NEW viral formula for a niche from analyzed videos and save it.

        Learns the shared hook + structure + pacing recipe from at least 3
        analyzed Library videos, in one AI call on your configured AI
        provider key. Synchronous — returns the formula directly.

        Args:
            niche: The niche to build the formula for.
            video_ids: Specific analyzed downloaded-video ids (from
                list_downloaded) to learn from. Omit to use up to 15 analyzed
                videos whose scout niche matches `niche`.

        Returns:
            {"id": str, "niche": str, "formula": {...}, "video_count": int,
             "created_at": str}

        Raises:
            httpx.HTTPStatusError: 400 when fewer than 3 analyzed videos are
                available — download and analyze more in that niche first.
        """
        n = (niche or "").strip()
        if not n:
            raise ValueError("niche is required")
        body: dict = {"niche": n}
        if video_ids:
            body["video_ids"] = video_ids
        return await client.post("/api/scout/viral-formula", json=body)
