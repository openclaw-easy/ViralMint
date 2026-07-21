# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
News Scout Agent — intelligent news research pipeline.
1. Scrape multiple sources in parallel
2. Deduplicate
3. Fetch full article text for top candidates (trafilatura)
4. AI quality filter + deep analysis
5. Store in scout_results (platform="news")
6. Send results via WS
"""
import asyncio
import hashlib
import json
import logging
from datetime import datetime

from sqlalchemy import select
from backend.database import AsyncSessionLocal
from backend.models.scout_result import ScoutResult
from backend.models.user_settings import UserSettings
from backend.core.ws_manager import ws_manager
from backend.agents.job_helper import update_job_status

logger = logging.getLogger(__name__)

# Max articles to fetch full text for (trafilatura is slow)
MAX_FULL_TEXT_FETCH = 10
# Concurrency limit for fetching article text
TEXT_FETCH_SEMAPHORE = 5


class NewsScoutAgent:
    async def run(
        self,
        job_id: str,
        query: str,
        expanded_queries: list[str] | None = None,
        sources: list[str] | None = None,
        direct_url: str | None = None,
        user_id: str = "local",
    ):
        """
        Full news scout pipeline.
        direct_url: if set, skip search and analyze this single article.
        """
        from backend.services.news_scraper import scrape_news, fetch_article_text, fetch_direct_url
        from backend.services.news_analyzer_service import analyze_articles, analyze_single_article

        await update_job_status(job_id, "running", progress_pct=0, current_step="Starting news research...")

        # Load user settings for AI provider
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            user_settings = result.scalar_one_or_none()

        try:
            # ── Direct URL mode ──────────────────────────────────────────
            if direct_url:
                await update_job_status(job_id, "running", progress_pct=20,
                                        current_step="Fetching article...")
                article = await fetch_direct_url(direct_url)

                if not article.get("full_text"):
                    # All extraction methods failed — try AI extraction as last resort
                    await update_job_status(job_id, "running", progress_pct=35,
                                            current_step="Standard extraction failed, trying AI extraction...")
                    ai_text = await self._ai_extract_text(direct_url, user_settings)
                    if ai_text:
                        article["full_text"] = ai_text
                        article["word_count"] = len(ai_text.split())
                        logger.info("AI extraction succeeded for %s (%d words)", direct_url[:60], article["word_count"])

                if not article.get("full_text"):
                    await update_job_status(job_id, "failed",
                                            error_message="Could not extract text from that URL. It may be paywalled or blocked.")
                    await ws_manager.send({
                        "type": "job_failed", "job_id": job_id,
                        "error": "Could not extract article text. The page may be paywalled or blocked.",
                    }, user_id)
                    return

                await update_job_status(job_id, "running", progress_pct=50,
                                        current_step="AI analyzing article...")
                article = await analyze_single_article(article, user_settings)

                # Store and send
                stored = await self._store_results([article], query, job_id, user_id)
                await self._send_results(stored, query, job_id, user_id)
                await update_job_status(job_id, "success", progress_pct=100,
                                        current_step="Done",
                                        output_data={"total_results": len(stored)})
                await ws_manager.send({
                    "type": "job_complete", "job_id": job_id, "job_type": "news_scout",
                    "result": {"total_results": len(stored)},
                }, user_id)
                return

            # ── Multi-source search mode ─────────────────────────────────
            all_queries = [query]
            if expanded_queries:
                all_queries.extend(expanded_queries[:2])  # Limit to 2 extra queries to avoid massive result sets

            await update_job_status(job_id, "running", progress_pct=10,
                                    current_step=f"Scraping {len(sources) if sources else 12} news sources...")

            # Scrape all queries in parallel. Up to two passes — Google
            # News RSS in particular hands back empty bodies under any
            # mild rate-pressure; a 6s wait usually clears it. Without
            # this retry, a 2-second 0-article "success" looks like an
            # upstream code bug from the caller's vantage point. See
            # 2026-05-16 incident where summit queries returned 0 in 2
            # seconds twice while curl confirmed the upstreams were
            # responding with empty payloads.
            from backend.services.news_scraper import _deduplicate

            async def _scrape_pass() -> list[dict]:
                tasks = [scrape_news(q, sources) for q in all_queries]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                out: list[dict] = []
                for r in results:
                    if isinstance(r, list):
                        out.extend(r)
                    elif isinstance(r, Exception):
                        logger.warning("News scrape failed for a query: %s", r)
                return _deduplicate(out)

            all_articles = await _scrape_pass()
            if not all_articles:
                logger.info(
                    "News scrape returned 0 articles for %r — retrying once after 6s "
                    "(likely upstream RSS throttle)",
                    query,
                )
                await update_job_status(
                    job_id, "running", progress_pct=15,
                    current_step="No articles on first pass — retrying...",
                )
                await asyncio.sleep(6)
                all_articles = await _scrape_pass()

            if not all_articles:
                # Surface as a constraint warning so the chat snackbar +
                # any activity feed make clear it's upstream-empty, not a
                # code failure. The job still completes "success" with 0
                # results — the caller can branch on total_results.
                await ws_manager.send_constraint_warning(
                    constraint="news_sources_empty",
                    message=(
                        f"News sources returned 0 articles for {query!r} after one retry. "
                        "Google News + Bing News RSS feeds occasionally throttle to empty "
                        "bodies; try again in 5-10 minutes or with a different query."
                    ),
                    severity="warning",
                    user_id=user_id,
                )
                await update_job_status(job_id, "success", progress_pct=100,
                                        current_step="No articles found",
                                        output_data={"total_results": 0})
                await ws_manager.send({
                    "type": "job_complete", "job_id": job_id, "job_type": "news_scout",
                    "result": {"total_results": 0},
                }, user_id)
                return

            await update_job_status(job_id, "running", progress_pct=30,
                                    current_step=f"Found {len(all_articles)} articles, fetching full text...")

            # Sort by engagement to prioritize popular articles for full text fetch
            all_articles.sort(key=lambda a: a.get("engagement", 0), reverse=True)

            # Fetch full text for top candidates (trafilatura, with concurrency limit)
            sem = asyncio.Semaphore(TEXT_FETCH_SEMAPHORE)
            to_fetch = all_articles[:MAX_FULL_TEXT_FETCH]

            async def _fetch_one(article):
                async with sem:
                    if article.get("full_text"):
                        return
                    data = await fetch_article_text(article["url"])
                    article["full_text"] = data.get("text")
                    article["word_count"] = data.get("word_count", 0)
                    if data.get("image") and not article.get("image_url"):
                        article["image_url"] = data["image"]

            fetch_tasks = [_fetch_one(a) for a in to_fetch]
            await asyncio.gather(*fetch_tasks, return_exceptions=True)

            # Count how many have full text
            with_text = sum(1 for a in to_fetch if a.get("full_text"))
            logger.info("Fetched full text for %d/%d articles", with_text, len(to_fetch))

            await update_job_status(job_id, "running", progress_pct=60,
                                    current_step=f"AI analyzing {len(to_fetch)} articles...")

            # AI analysis — one article per API call, run in parallel
            try:
                analyzed = await analyze_articles(to_fetch, query, user_settings)
            except Exception as ai_err:
                logger.error("AI analysis failed for news scout: %s", ai_err)
                await update_job_status(job_id, "failed",
                                        error_message=f"AI analysis failed: {str(ai_err)[:200]}")
                await ws_manager.send({
                    "type": "job_failed", "job_id": job_id,
                    "error": f"Found {len(to_fetch)} articles but AI analysis failed: {str(ai_err)[:150]}. Try again in a moment.",
                }, user_id)
                return

            if not analyzed:
                await update_job_status(job_id, "success", progress_pct=100,
                                        current_step="No high-quality articles found",
                                        output_data={"total_results": 0})
                await ws_manager.send({
                    "type": "job_complete", "job_id": job_id, "job_type": "news_scout",
                    "result": {"total_results": 0},
                }, user_id)
                return

            await update_job_status(job_id, "running", progress_pct=85,
                                    current_step=f"Saving {len(analyzed)} results...")

            # Store in DB and send via WS
            stored = await self._store_results(analyzed, query, job_id, user_id)
            await self._send_results(stored, query, job_id, user_id)

            await update_job_status(job_id, "success", progress_pct=100,
                                    current_step="Done",
                                    output_data={"total_results": len(stored)})
            await ws_manager.send({
                "type": "job_complete", "job_id": job_id, "job_type": "news_scout",
                "result": {"total_results": len(stored)},
            }, user_id)

        except Exception as e:
            logger.error("News scout failed: %s", e, exc_info=True)
            await update_job_status(job_id, "failed", error_message=str(e))
            await ws_manager.send({
                "type": "job_failed", "job_id": job_id,
                "error": f"News research failed: {str(e)[:200]}",
            }, user_id)

    async def _store_results(
        self,
        articles: list[dict],
        query: str,
        job_id: str,
        user_id: str,
    ) -> list[dict]:
        """Store analyzed articles as scout_results with platform='news'."""
        stored = []
        async with AsyncSessionLocal() as db:
            for article in articles:
                # Generate a stable video_id from URL for dedup
                url = article.get("url", "")
                video_id = hashlib.md5(url.encode()).hexdigest()[:20]

                # Check for existing result with same video_id
                existing = await db.execute(
                    select(ScoutResult).where(
                        ScoutResult.user_id == user_id,
                        ScoutResult.platform == "news",
                        ScoutResult.video_id == video_id,
                    )
                )
                if existing.scalar_one_or_none():
                    continue  # Skip duplicate

                analysis = article.get("analysis", {})
                # Store full analysis + full text in description field as JSON
                description_json = json.dumps({
                    "full_text": article.get("full_text") or "",
                    "full_text_preview": (article.get("full_text") or "")[:500],
                    "source": article.get("source", ""),
                    "word_count": article.get("word_count", 0),
                    "engagement": article.get("engagement", 0),
                    **analysis,
                }, ensure_ascii=False)

                sr = ScoutResult(
                    user_id=user_id,
                    job_id=job_id,
                    platform="news",
                    video_id=video_id,
                    video_url=url,
                    title=article.get("title", "Untitled"),
                    description=description_json,
                    author=article.get("source_domain", ""),
                    thumbnail_url=article.get("image_url"),
                    views=article.get("engagement", 0),
                    virality_score=article.get("virality_score", 0),
                    niche=query,
                    upload_date=article.get("published_at"),
                )
                db.add(sr)
                await db.flush()

                stored.append({
                    "id": sr.id,
                    "title": sr.title,
                    "source_domain": article.get("source_domain", ""),
                    "url": url,
                    "image_url": article.get("image_url"),
                    "published_at": article.get("published_at").isoformat() if article.get("published_at") else None,
                    "word_count": article.get("word_count", 0),
                    "virality_score": article.get("virality_score", 0),
                    "engagement": article.get("engagement", 0),
                    "analysis": analysis,
                })

            await db.commit()

        return stored

    async def _ai_extract_text(self, url: str, user_settings=None) -> str | None:
        """
        Last-resort AI extraction: fetch raw HTML, truncate, and ask AI to extract article text.
        Used when trafilatura, paragraph extraction, and meta description all fail.
        """
        from backend.services.news_scraper import _fetch_html

        try:
            html = await _fetch_html(url)
            if not html:
                return None

            # Strip scripts/styles/nav to reduce size, keep just the body content
            import re as _re
            # Remove <script>, <style>, <nav>, <footer>, <header>, <svg> blocks
            clean = _re.sub(r'<(script|style|nav|footer|header|svg|noscript)[^>]*>.*?</\1>', '', html, flags=_re.DOTALL | _re.IGNORECASE)
            # Remove all HTML tags but keep text
            text_only = _re.sub(r'<[^>]+>', ' ', clean)
            # Collapse whitespace
            text_only = _re.sub(r'\s+', ' ', text_only).strip()

            if len(text_only) < 100:
                return None

            # Truncate to ~8000 chars to fit in a single AI call
            truncated = text_only[:8000]

            # Use AI to extract the clean article text
            from backend.core.ai_provider import get_ai_client
            prompt = (
                "Extract ONLY the main article text from this web page content. "
                "Remove all navigation, ads, sidebars, comments, related articles, and boilerplate. "
                "Return ONLY the clean article text, nothing else. No commentary, no labels, no formatting instructions.\n\n"
                f"Page content:\n{truncated}"
            )

            response = None
            try:
                ai = get_ai_client(user_settings)
                response = await ai.chat(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=4000,
                )
            except Exception as e:
                logger.debug("AI extraction failed: %s", e)
                return None

            if response and len(response) > 100:
                logger.info("AI extracted %d chars from %s", len(response), url[:60])
                return response.strip()

        except Exception as e:
            logger.warning("AI text extraction failed for %s: %s", url[:60], e)
        return None

    async def _send_results(
        self,
        stored: list[dict],
        query: str,
        job_id: str,
        user_id: str,
    ):
        """Send news results via WebSocket."""
        await ws_manager.send({
            "type": "news_results",
            "job_id": job_id,
            "total": len(stored),
            "query": query,
            "results": stored,
        }, user_id)
