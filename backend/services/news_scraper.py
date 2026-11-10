# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Multi-source news scraper for ViralMint.
Scrapes Google News, Bing News, Hacker News, Reddit, and direct URLs.
All sources run in parallel via asyncio.gather.
"""
import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse, quote_plus

import httpx

logger = logging.getLogger(__name__)

# In-memory TTL cache: query → (timestamp, results)
_cache: dict[str, tuple[float, list[dict]]] = {}
CACHE_TTL = 900  # 15 minutes

# User agent for HTTP requests
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

SOURCE_FUNCTIONS = {
    "google": "_scrape_google_news",
    "bing": "_scrape_bing_news",
    "hackernews": "_scrape_hackernews",
    "reddit": "_scrape_reddit_news",
    "cnbc": "_scrape_cnbc",
    "bbc": "_scrape_bbc",
    "reuters": "_scrape_reuters",
    "nytimes": "_scrape_nytimes",
    "guardian": "_scrape_guardian",
    "aljazeera": "_scrape_aljazeera",
    "techcrunch": "_scrape_techcrunch",
    "yahoo": "_scrape_yahoo_news",
}


async def scrape_news(
    query: str,
    sources: list[str] | None = None,
    max_per_source: int = 15,
) -> list[dict]:
    """
    Scrape news from multiple sources in parallel.
    Returns deduplicated list of article dicts.
    """
    if not sources:
        sources = list(SOURCE_FUNCTIONS.keys())

    # Check cache
    cache_key = f"{query}:{','.join(sorted(sources))}:{max_per_source}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        logger.info("News cache hit for query=%r", query)
        return cached[1]

    tasks = []
    for source in sources:
        fn_name = SOURCE_FUNCTIONS.get(source)
        if fn_name:
            fn = globals()[fn_name]
            tasks.append(fn(query, max_per_source))
        else:
            logger.warning("Unknown news source: %s", source)

    # Run all sources in parallel, don't fail if one source errors
    all_results = []
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    for i, result in enumerate(gathered):
        if isinstance(result, Exception):
            logger.warning("News source %s failed: %s", sources[i], result)
            continue
        if isinstance(result, list):
            all_results.extend(result)

    # Deduplicate by normalized URL
    deduped = _deduplicate(all_results)
    logger.info("Scraped %d articles (%d after dedup) for query=%r",
                len(all_results), len(deduped), query)

    # Cache results
    _cache[cache_key] = (time.time(), deduped)

    return deduped


async def fetch_article_text(url: str) -> dict:
    """
    Fetch full article text from a URL with multi-layer extraction.
    Returns {text, title, author, date, image, word_count}.

    Extraction chain (stops at first success):
      1. trafilatura (precision mode) — best quality, handles most sites
      2. trafilatura (recall mode) — more aggressive, catches JS-heavy sites
      3. HTML <p> tag extraction — raw but reliable fallback
      4. og:description / meta description — last resort, at least gets a summary

    Title/image always supplemented from og: meta tags.
    """
    empty = {"text": None, "title": None, "author": None, "date": None, "image": None, "word_count": 0}

    # Step 0: fetch HTML
    html = await _fetch_html(url)
    if not html:
        return empty

    # Step 1: trafilatura precision mode
    result = await asyncio.to_thread(_extract_with_trafilatura, html, url, True)
    text = result.get("text")
    method = "trafilatura_precision"

    # Step 2: trafilatura recall mode (more aggressive)
    if not text:
        result2 = await asyncio.to_thread(_extract_with_trafilatura, html, url, False)
        text = result2.get("text")
        if text:
            result = result2
            method = "trafilatura_recall"

    # Step 3: raw <p> tag extraction
    if not text:
        text = _extract_paragraphs(html)
        if text:
            method = "paragraph_extraction"

    # Step 4: og:description / meta description as last resort
    if not text:
        text = _extract_meta_description(html)
        if text:
            method = "meta_description"

    if text:
        logger.info("Extracted %d words from %s via %s", len(text.split()), _extract_domain(url), method)

    # Always try to fill title and image from og: tags
    title = result.get("title") or _extract_og_meta(html, "og:title") or _extract_html_title(html)
    image = result.get("image") or _extract_og_meta(html, "og:image")

    return {
        "text": text,
        "title": title,
        "author": result.get("author"),
        "date": result.get("date"),
        "image": image,
        "word_count": len(text.split()) if text else 0,
    }


async def _fetch_html(url: str) -> str | None:
    """Fetch HTML from URL with browser-like headers. Tries multiple User-Agents on failure."""
    user_agents = [
        USER_AGENT,
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    ]
    for ua in user_agents:
        try:
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*",
                         "Accept-Language": "en-US,en;q=0.9"},
            ) as client:
                resp = await client.get(url)
                if resp.status_code == 403 or resp.status_code == 451:
                    logger.debug("Got %d with UA %s, trying next...", resp.status_code, ua[:30])
                    continue
                resp.raise_for_status()
                return resp.text
        except httpx.HTTPStatusError:
            continue
        except Exception as e:
            logger.warning("Failed to fetch HTML from %s: %s", url, e)
            return None
    logger.warning("All User-Agents failed for %s", url)
    return None


def _extract_with_trafilatura(html: str, url: str, favor_precision: bool = True) -> dict:
    """Extract article content using trafilatura (runs in thread)."""
    import trafilatura

    try:
        # bare_extraction returns Document object in trafilatura 2.x, dict in 1.x
        result = trafilatura.bare_extraction(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_precision=favor_precision,
            favor_recall=not favor_precision,
        )
    except Exception as e:
        logger.warning("trafilatura extraction error: %s", e)
        return {}

    # Handle both Document object (2.x) and dict (1.x)
    if result is None:
        return {}
    elif isinstance(result, dict):
        meta = result
    else:
        meta = {
            "text": getattr(result, "text", None),
            "title": getattr(result, "title", None),
            "author": getattr(result, "author", None),
            "date": getattr(result, "date", None),
            "image": getattr(result, "image", None),
        }

    text = meta.get("text", "") or ""
    return {
        "text": text if len(text) > 50 else None,
        "title": meta.get("title"),
        "author": meta.get("author"),
        "date": meta.get("date"),
        "image": meta.get("image"),
    }


def _extract_paragraphs(html: str) -> str | None:
    """Fallback: extract text from <p> tags using lxml."""
    try:
        from lxml import etree
        from lxml.html import fromstring

        doc = fromstring(html)

        # Remove script, style, nav, footer, header, aside elements
        for tag in doc.iter("script", "style", "nav", "footer", "header", "aside", "noscript"):
            tag.getparent().remove(tag)

        # Find article body — try common selectors
        article = None
        for selector in ["//article", "//*[@role='main']", "//*[@id='article-body']",
                         "//*[contains(@class,'article-body')]", "//*[contains(@class,'story-body')]",
                         "//*[contains(@class,'post-content')]", "//*[contains(@class,'entry-content')]"]:
            try:
                nodes = doc.xpath(selector)
                if nodes:
                    article = nodes[0]
                    break
            except Exception:
                continue

        # Collect paragraphs from article element (or whole body)
        source = article if article is not None else doc
        paragraphs = []
        for p in source.iter("p"):
            text = p.text_content().strip()
            if len(text) > 30:  # Skip tiny fragments (nav items, captions)
                paragraphs.append(text)

        combined = "\n\n".join(paragraphs)
        if len(combined) > 100:
            return combined
    except Exception as e:
        logger.debug("Paragraph extraction failed: %s", e)
    return None


def _extract_meta_description(html: str) -> str | None:
    """Last resort: extract og:description or meta description."""
    desc = _extract_og_meta(html, "og:description")
    if desc and len(desc) > 50:
        return desc
    m = re.search(r'<meta\s[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m and len(m.group(1)) > 50:
        return m.group(1).replace("&amp;", "&").replace("&#x27;", "'").replace("&quot;", '"')
    return None


def _extract_html_title(html: str) -> str | None:
    """Extract <title> from HTML."""
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        # Clean common suffixes like " | CNN" or " - Reuters"
        for sep in [" | ", " - ", " – ", " — "]:
            if sep in title:
                parts = title.rsplit(sep, 1)
                if len(parts[0]) > 15:  # Don't strip if it leaves too little
                    title = parts[0].strip()
                    break
        return title
    return None


async def fetch_direct_url(url: str) -> dict:
    """
    Fetch and structure a single article from a user-provided URL.
    Returns a single article dict ready for analysis.
    """
    article_data = await fetch_article_text(url)
    domain = _extract_domain(url)

    return {
        "title": article_data.get("title") or _extract_title_from_url(url),
        "url": url,
        "source": "Direct URL",
        "source_domain": domain,
        "summary": (article_data.get("text") or "")[:300],
        "full_text": article_data.get("text"),
        "published_at": _parse_date(article_data.get("date")),
        "image_url": article_data.get("image"),
        "author": article_data.get("author"),
        "word_count": article_data.get("word_count", 0),
        "engagement": 0,
    }


# ── Source implementations ──────────────────────────────────────────────────────


async def _scrape_google_news(query: str, max_results: int = 15) -> list[dict]:
    """Scrape Google News RSS feed."""
    import feedparser

    encoded = quote_plus(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=US&ceid=US:en"

    async with httpx.AsyncClient(
        timeout=10,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        resp = await client.get(rss_url)
        resp.raise_for_status()

    feed = await asyncio.to_thread(feedparser.parse, resp.text)
    articles = []

    for entry in feed.entries[:max_results]:
        # Google News wraps URLs — extract the real URL
        link = entry.get("link", "")
        # Try to resolve Google News redirect
        real_url = await _resolve_google_news_url(link)

        pub_date = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            pub_date = datetime(*entry.published_parsed[:6])

        # Extract source from title (Google News format: "Title - Source")
        title = entry.get("title", "")
        source_domain = ""
        if " - " in title:
            parts = title.rsplit(" - ", 1)
            title = parts[0].strip()
            source_domain = parts[1].strip()

        # Try to extract image from summary HTML or media:content
        image_url = _extract_image_from_entry(entry)

        articles.append({
            "title": title,
            "url": real_url or link,
            "source": "Google News",
            "source_domain": source_domain or _extract_domain(real_url or link),
            "summary": _strip_html(entry.get("summary", "")),
            "full_text": None,  # Will be fetched later by trafilatura
            "published_at": pub_date,
            "image_url": image_url,
            "author": None,
            "word_count": 0,
            "engagement": 0,
        })

    return articles


async def _scrape_bing_news(query: str, max_results: int = 15) -> list[dict]:
    """Scrape Bing News RSS feed."""
    import feedparser

    encoded = quote_plus(query)
    rss_url = f"https://www.bing.com/news/search?q={encoded}&format=rss"

    async with httpx.AsyncClient(
        timeout=10,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        resp = await client.get(rss_url)
        resp.raise_for_status()

    feed = await asyncio.to_thread(feedparser.parse, resp.text)
    articles = []

    for entry in feed.entries[:max_results]:
        pub_date = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            pub_date = datetime(*entry.published_parsed[:6])

        link = entry.get("link", "")
        image_url = _extract_image_from_entry(entry)

        articles.append({
            "title": entry.get("title", ""),
            "url": link,
            "source": "Bing News",
            "source_domain": _extract_domain(link),
            "summary": _strip_html(entry.get("summary", "")),
            "full_text": None,
            "published_at": pub_date,
            "image_url": image_url,
            "author": None,
            "word_count": 0,
            "engagement": 0,
        })

    return articles


async def _scrape_hackernews(query: str, max_results: int = 15) -> list[dict]:
    """Scrape Hacker News via Algolia search API."""
    encoded = quote_plus(query)
    api_url = f"https://hn.algolia.com/api/v1/search?query={encoded}&tags=story&hitsPerPage={max_results}"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(api_url)
        resp.raise_for_status()
        data = resp.json()

    articles = []
    for hit in data.get("hits", [])[:max_results]:
        url = hit.get("url", "")
        if not url:
            # Self-post on HN — link to HN comments
            url = f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"

        pub_date = None
        created_at = hit.get("created_at")
        if created_at:
            try:
                pub_date = datetime.fromisoformat(created_at.replace("Z", "+00:00")).replace(tzinfo=None)
            except (ValueError, AttributeError):
                pass

        articles.append({
            "title": hit.get("title", ""),
            "url": url,
            "source": "Hacker News",
            "source_domain": _extract_domain(url),
            "summary": "",
            "full_text": None,
            "published_at": pub_date,
            "image_url": None,
            "author": hit.get("author"),
            "word_count": 0,
            "engagement": hit.get("points", 0),
        })

    return articles


async def _scrape_reddit_news(query: str, max_results: int = 15) -> list[dict]:
    """Scrape Reddit search via JSON API (no auth needed)."""
    encoded = quote_plus(query)
    api_url = f"https://www.reddit.com/search.json?q={encoded}&sort=hot&limit={max_results}&type=link"

    async with httpx.AsyncClient(
        timeout=10,
        headers={"User-Agent": "ViralMint/1.0"},
    ) as client:
        resp = await client.get(api_url)
        resp.raise_for_status()
        data = resp.json()

    articles = []
    for child in data.get("data", {}).get("children", [])[:max_results]:
        post = child.get("data", {})
        url = post.get("url", "")

        # Skip Reddit self-posts that are just text with no external link
        is_self = post.get("is_self", False)
        if is_self:
            url = f"https://reddit.com{post.get('permalink', '')}"

        pub_date = None
        created_utc = post.get("created_utc")
        if created_utc:
            pub_date = datetime.utcfromtimestamp(created_utc)

        articles.append({
            "title": post.get("title", ""),
            "url": url,
            "source": "Reddit",
            "source_domain": _extract_domain(url) if not is_self else "reddit.com",
            "summary": (post.get("selftext", "") or "")[:300],
            "full_text": None,
            "published_at": pub_date,
            "image_url": post.get("thumbnail") if post.get("thumbnail", "").startswith("http") else None,
            "author": post.get("author"),
            "word_count": 0,
            "engagement": post.get("score", 0),
        })

    return articles


# ── RSS-based news sources ─────────────────────────────────────────────────────

# Many major outlets expose RSS feeds that we can search or scrape.
# For outlets without search RSS, we scrape their topic/section feeds and filter by query.


async def _scrape_rss_feed(
    feed_url: str,
    source_name: str,
    query: str,
    max_results: int = 15,
) -> list[dict]:
    """Generic RSS feed scraper. Fetches feed and filters entries by query keywords."""
    import feedparser

    async with httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        resp = await client.get(feed_url)
        resp.raise_for_status()

    feed = await asyncio.to_thread(feedparser.parse, resp.text)
    query_words = set(query.lower().split())
    articles = []

    for entry in feed.entries[:50]:  # scan more entries since we filter
        title = entry.get("title", "")
        summary = _strip_html(entry.get("summary", "") or entry.get("description", ""))
        link = entry.get("link", "")

        # Filter: at least one query word must appear in title or summary
        text_lower = f"{title} {summary}".lower()
        if not any(w in text_lower for w in query_words):
            continue

        pub_date = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                pub_date = datetime(*entry.published_parsed[:6])
            except Exception:
                pass

        image_url = _extract_image_from_entry(entry)

        articles.append({
            "title": title,
            "url": link,
            "source": source_name,
            "source_domain": _extract_domain(link),
            "summary": summary[:300],
            "full_text": None,
            "published_at": pub_date,
            "image_url": image_url,
            "author": entry.get("author"),
            "word_count": 0,
            "engagement": 0,
        })

        if len(articles) >= max_results:
            break

    return articles


async def _scrape_cnbc(query: str, max_results: int = 15) -> list[dict]:
    """Scrape CNBC news via RSS feeds (world, business, tech, politics)."""
    feeds = [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",  # Top News
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",  # World
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147",   # Economy
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",   # Technology
    ]
    all_articles = []
    for feed_url in feeds:
        try:
            articles = await _scrape_rss_feed(feed_url, "CNBC", query, max_results)
            all_articles.extend(articles)
        except Exception as e:
            logger.debug("CNBC feed failed: %s", e)
    return _deduplicate(all_articles)[:max_results]


async def _scrape_bbc(query: str, max_results: int = 15) -> list[dict]:
    """Scrape BBC News via RSS feeds."""
    feeds = [
        "https://feeds.bbci.co.uk/news/rss.xml",           # Top Stories
        "https://feeds.bbci.co.uk/news/world/rss.xml",     # World
        "https://feeds.bbci.co.uk/news/business/rss.xml",  # Business
        "https://feeds.bbci.co.uk/news/technology/rss.xml", # Technology
    ]
    all_articles = []
    for feed_url in feeds:
        try:
            articles = await _scrape_rss_feed(feed_url, "BBC News", query, max_results)
            all_articles.extend(articles)
        except Exception as e:
            logger.debug("BBC feed failed: %s", e)
    return _deduplicate(all_articles)[:max_results]


async def _scrape_reuters(query: str, max_results: int = 15) -> list[dict]:
    """Scrape Reuters via RSS feed."""
    feeds = [
        "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
    ]
    all_articles = []
    for feed_url in feeds:
        try:
            articles = await _scrape_rss_feed(feed_url, "Reuters", query, max_results)
            all_articles.extend(articles)
        except Exception as e:
            logger.debug("Reuters feed failed: %s", e)

    # Reuters RSS can be limited — supplement with Google News filtered to reuters.com
    try:
        from urllib.parse import quote_plus as _qp
        google_reuters_url = f"https://news.google.com/rss/search?q={_qp(query)}+site:reuters.com&hl=en&gl=US&ceid=US:en"
        g_articles = await _scrape_rss_feed(google_reuters_url, "Reuters", query, max_results)
        all_articles.extend(g_articles)
    except Exception as e:
        logger.debug("Google→Reuters fallback failed: %s", e)

    return _deduplicate(all_articles)[:max_results]


async def _scrape_nytimes(query: str, max_results: int = 15) -> list[dict]:
    """Scrape New York Times via RSS feeds."""
    feeds = [
        "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    ]
    all_articles = []
    for feed_url in feeds:
        try:
            articles = await _scrape_rss_feed(feed_url, "NY Times", query, max_results)
            all_articles.extend(articles)
        except Exception as e:
            logger.debug("NYT feed failed: %s", e)
    return _deduplicate(all_articles)[:max_results]


async def _scrape_guardian(query: str, max_results: int = 15) -> list[dict]:
    """Scrape The Guardian via RSS search feed."""
    from urllib.parse import quote_plus as _qp
    # Guardian supports query-based RSS
    feed_url = f"https://www.theguardian.com/search/rss?query={_qp(query)}&order-by=newest"
    try:
        return await _scrape_rss_feed(feed_url, "The Guardian", query, max_results)
    except Exception as e:
        logger.debug("Guardian feed failed: %s", e)
        return []


async def _scrape_aljazeera(query: str, max_results: int = 15) -> list[dict]:
    """Scrape Al Jazeera via RSS feed."""
    feeds = [
        "https://www.aljazeera.com/xml/rss/all.xml",
    ]
    all_articles = []
    for feed_url in feeds:
        try:
            articles = await _scrape_rss_feed(feed_url, "Al Jazeera", query, max_results)
            all_articles.extend(articles)
        except Exception as e:
            logger.debug("Al Jazeera feed failed: %s", e)
    return _deduplicate(all_articles)[:max_results]


async def _scrape_techcrunch(query: str, max_results: int = 15) -> list[dict]:
    """Scrape TechCrunch via RSS feed."""
    feed_url = "https://techcrunch.com/feed/"
    try:
        return await _scrape_rss_feed(feed_url, "TechCrunch", query, max_results)
    except Exception as e:
        logger.debug("TechCrunch feed failed: %s", e)
        return []


async def _scrape_yahoo_news(query: str, max_results: int = 15) -> list[dict]:
    """Scrape Yahoo News via RSS search."""
    from urllib.parse import quote_plus as _qp
    feed_url = f"https://news.yahoo.com/rss/search?p={_qp(query)}"
    try:
        return await _scrape_rss_feed(feed_url, "Yahoo News", query, max_results)
    except Exception as e:
        logger.debug("Yahoo News feed failed: %s", e)
        return []


# ── Utilities ───────────────────────────────────────────────────────────────────


def _extract_og_meta(html: str, prop: str) -> str | None:
    """Extract Open Graph meta tag content from HTML (handles various attribute orders)."""
    # Match: <meta property="og:X" ... content="VALUE" ...>
    # Attributes can appear in any order with extra attrs between them
    pattern = rf'<meta\s[^>]*property=["\']{ re.escape(prop) }["\'][^>]*content=["\']([^"\']+)["\']'
    m = re.search(pattern, html, re.IGNORECASE)
    if m:
        return m.group(1).replace("&amp;", "&")
    # Reverse order: content before property
    pattern2 = rf'<meta\s[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']{ re.escape(prop) }["\']'
    m2 = re.search(pattern2, html, re.IGNORECASE)
    if m2:
        return m2.group(1).replace("&amp;", "&")
    return None


def _extract_image_from_entry(entry) -> str | None:
    """Extract image URL from an RSS feed entry (media:content, enclosure, or summary HTML)."""
    # Check media:content (common in news RSS)
    media_content = getattr(entry, "media_content", None)
    if media_content and isinstance(media_content, list):
        for m in media_content:
            url = m.get("url", "")
            if url and ("image" in m.get("type", "") or m.get("medium") == "image"):
                return url
            if url:  # Some feeds don't set type
                return url

    # Check media:thumbnail
    media_thumbnail = getattr(entry, "media_thumbnail", None)
    if media_thumbnail and isinstance(media_thumbnail, list):
        for t in media_thumbnail:
            if t.get("url"):
                return t["url"]

    # Check enclosures (some RSS feeds use this for images)
    enclosures = getattr(entry, "enclosures", [])
    for enc in enclosures:
        if "image" in enc.get("type", ""):
            return enc.get("href") or enc.get("url")

    # Try to extract <img> from summary HTML
    summary = entry.get("summary", "")
    if summary:
        img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary)
        if img_match:
            img_url = img_match.group(1)
            if img_url.startswith("http"):
                return img_url

    return None


async def _resolve_google_news_url(google_url: str) -> str | None:
    """Try to resolve a Google News redirect to the actual article URL."""
    if "news.google.com" not in google_url:
        return google_url
    try:
        async with httpx.AsyncClient(
            timeout=5,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.head(google_url)
            final_url = str(resp.url)
            if "news.google.com" not in final_url:
                return final_url
    except Exception:
        pass
    return None


def _normalize_url(url: str) -> str:
    """Normalize URL for deduplication."""
    parsed = urlparse(url)
    # Strip tracking params, fragment, trailing slash
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    return clean.lower()


def _deduplicate(articles: list[dict]) -> list[dict]:
    """Remove duplicate articles by normalized URL."""
    seen = set()
    unique = []
    for article in articles:
        key = _normalize_url(article.get("url", ""))
        if key not in seen:
            seen.add(key)
            unique.append(article)
    return unique


def _extract_domain(url: str) -> str:
    """Extract domain from URL (e.g. 'cnbc.com')."""
    try:
        host = urlparse(url).hostname or ""
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _extract_title_from_url(url: str) -> str:
    """Extract a rough title from URL path."""
    path = urlparse(url).path
    # Take last path segment, replace hyphens with spaces
    segments = [s for s in path.split("/") if s]
    if segments:
        title = segments[-1].replace("-", " ").replace("_", " ")
        # Remove file extensions
        title = re.sub(r"\.\w+$", "", title)
        return title.title()
    return "Untitled Article"


def _strip_html(text: str) -> str:
    """Remove HTML tags from text."""
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_date(date_str: str | None) -> datetime | None:
    """Try to parse a date string into a datetime."""
    if not date_str:
        return None
    for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(date_str[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None
