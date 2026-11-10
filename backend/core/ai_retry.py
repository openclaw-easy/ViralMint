# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
AI-assisted error recovery for download, scout, and parsing failures.
Sends error context to the user's AI provider and asks for corrections.
Falls back gracefully — if AI retry fails, original error is preserved.
"""
import json
import logging
from backend.core.ai_provider import get_ai_client
import re
from backend.core.prompt_rules import extract_json_text

logger = logging.getLogger(__name__)

URL_FIX_PROMPT = """A video download failed with the following error.
Analyze the URL and error, then return ONLY a corrected URL that might work.
If the URL looks correct and the error is not URL-related (e.g. private video, rate limit, network error), return exactly "SKIP".

Original URL: {url}
Error: {error}

Rules:
- Fix common URL issues: missing https://, wrong domain, malformed query params, mobile URLs (m.youtube.com → youtube.com)
- Convert short URLs to full URLs if possible (youtu.be/xxx → youtube.com/watch?v=xxx)
- If the URL contains a playlist parameter but the user likely wanted a single video, strip &list=...
- Return ONLY the corrected URL or "SKIP" — no explanation."""

SEARCH_REFINE_PROMPT = """A video search on "{platform}" for "{niche}" returned 0 results.
Suggest a better search query that is more likely to find trending content.

Rules:
- Keep it concise (2-5 words)
- Use broader or more common terms
- If the niche is too specific, generalize it
- Write the query in the language the platform's audience searches in
  (Douyin and Bilibili: Chinese; YouTube/TikTok: the niche's own language)
- Return ONLY the refined search query on one line — no explanation."""

JSON_FIX_PROMPT = """The following text was supposed to be valid JSON but failed to parse.
Fix it and return ONLY the corrected valid JSON — no explanation, no markdown fences.

Error: {error}

Broken text:
{broken_json}"""

ACTION_FIX_PROMPT = """The following was supposed to be a valid JSON action block but failed to parse.
It should be a JSON object with a "type" field and action-specific parameters.

Error: {error}

Broken text:
{broken_json}

Return ONLY the corrected valid JSON object — no explanation, no markdown fences."""


def _is_public_web_url(candidate: str) -> bool:
    """True for an http(s) URL whose host is a public name or address.

    No DNS lookup: this guards against a model (or text it was shown)
    steering the downloader at localhost, a private/link-local range or a
    cloud metadata address — not against a public name that resolves
    somewhere odd, which no URL-shape check could catch.
    """
    import ipaddress
    from urllib.parse import urlparse
    try:
        parsed = urlparse(candidate)
        hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not hostname:
        return False
    host = hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." in host   # a bare single-label name is a LAN host
    return ip.is_global


async def ai_fix_url(url: str, error: str, user_settings=None) -> str | None:
    """Ask AI to fix a failed download URL. Returns corrected URL or None."""
    try:
        ai = get_ai_client(user_settings)
        response = await ai.chat(
            messages=[{"role": "user", "content": URL_FIX_PROMPT.format(url=url, error=str(error)[:500])}],
            max_tokens=256,
        )
        # The first URL in the reply — a URL followed by an explanation used to
        # be taken whole as the "URL".
        _m = re.search(r"https?://\S+", response or "")
        corrected = (_m.group(0) if _m else response.strip()).strip().strip('"').strip("'").strip("`").rstrip(".,)")

        if corrected == "SKIP" or not corrected:
            logger.info(f"AI says URL error is not fixable: {url}")
            return None

        # The model's URL goes straight to yt-dlp, so it is validated like
        # user input: http(s), a real public host — never this machine, a
        # LAN address or a metadata endpoint. The error text the model read
        # can carry page content, i.e. text a site controls.
        if corrected != url and _is_public_web_url(corrected):
            logger.info(f"AI suggested URL fix: {url} → {corrected}")
            return corrected
        if corrected != url:
            logger.warning("AI URL fix rejected (not a public http(s) URL): %r", corrected[:120])

        return None
    except Exception as e:
        logger.debug(f"AI URL fix failed (non-critical): {e}")
        return None


async def ai_refine_search(platform: str, niche: str, user_settings=None) -> str | None:
    """Ask AI to suggest better search terms when scout returns 0 results."""
    try:
        ai = get_ai_client(user_settings)
        response = await ai.chat(
            messages=[{"role": "user", "content": SEARCH_REFINE_PROMPT.format(platform=platform, niche=niche)}],
            max_tokens=64,
        )
        refined = response.strip().strip('"').strip("'")

        if refined and refined.lower() != niche.lower() and len(refined) < 100:
            logger.info(f"AI refined search: '{niche}' → '{refined}' for {platform}")
            return refined

        return None
    except Exception as e:
        logger.debug(f"AI search refinement failed (non-critical): {e}")
        return None


async def ai_fix_json(broken_text: str, error: str, user_settings=None) -> dict | list | None:
    """Ask AI to repair malformed JSON from its own output. Returns parsed object or None."""
    try:
        ai = get_ai_client(user_settings)
        response = await ai.chat(
            messages=[{"role": "user", "content": JSON_FIX_PROMPT.format(
                error=str(error)[:200],
                broken_json=broken_text[:2000],
            )}],
            max_tokens=1024,
        )
        clean = response.strip()
        clean = extract_json_text(clean)  # fence anywhere / leading prose (prompt_rules)
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(clean)
        logger.info("AI successfully repaired malformed JSON")
        return result
    except Exception as e:
        logger.debug(f"AI JSON repair failed (non-critical): {e}")
        return None


API_PARSE_PROMPT = """A video search API or webpage returned data, but our parser couldn't extract the videos.
The response structure may have changed. Extract video information from this raw response.

Platform: {platform}
Search keyword: "{keyword}"

Raw response (truncated):
{raw_response}

Return a JSON array of video objects. Each object MUST have exactly these fields:
- "aweme_id": string (the video ID — use whatever unique ID identifies the video)
- "desc": string (video title or description/caption)
- "create_time": number (unix timestamp) or null
- "author": {{"unique_id": string (username/handle), "nickname": string (display name), "uid": string}}
- "statistics": {{"play_count": number, "digg_count": number (likes), "comment_count": number, "share_count": number}}
- "video": {{"duration": number (seconds or 0 if unknown), "cover": {{"url_list": [string (thumbnail URL)]}}}}
- "video_url": string (full URL to watch the video, if available)

Platform-specific ID hints:
- YouTube: video ID is the 11-char string (e.g. "dQw4w9WgXcQ")
- TikTok/Douyin: aweme_id is the long numeric string
- Reddit: post ID is the alphanumeric string

Rules:
- Only include items that are clearly video results (not ads, not users, not channels, not hashtags)
- The response may be JSON, HTML, or mixed — extract what you can
- If a field is missing, use 0 for numbers, "" for strings, null for timestamps
- Return ONLY the JSON array — no explanation, no markdown fences
- If you truly cannot find any video data, return an empty array: []"""


async def ai_parse_api_response(
    raw_response: str,
    platform: str,
    keyword: str,
    user_settings=None,
) -> list[dict] | None:
    """
    AI fallback parser for when rule-based API response parsing fails.
    Sends a truncated sample of the raw response to AI and asks it to extract videos.
    Returns a list of aweme-format dicts, or None on failure.
    """
    try:
        ai = get_ai_client(user_settings)
        response = await ai.chat(
            messages=[{"role": "user", "content": API_PARSE_PROMPT.format(
                platform=platform,
                keyword=keyword,
                raw_response=raw_response[:4000],
            )}],
            max_tokens=2048,
        )
        clean = response.strip()
        clean = extract_json_text(clean)  # fence anywhere / leading prose (prompt_rules)
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(clean)
        if isinstance(result, list) and len(result) > 0:
            logger.info(f"AI parsed {len(result)} videos from raw {platform} API response")
            return result
        return None
    except Exception as e:
        logger.debug(f"AI API response parsing failed (non-critical): {e}")
        return None


async def ai_fix_action(broken_text: str, error: str, user_settings=None) -> dict | None:
    """Ask AI to repair a malformed action block JSON. Returns parsed dict or None."""
    try:
        ai = get_ai_client(user_settings)
        response = await ai.chat(
            messages=[{"role": "user", "content": ACTION_FIX_PROMPT.format(
                error=str(error)[:200],
                broken_json=broken_text[:500],
            )}],
            max_tokens=256,
        )
        clean = response.strip()
        clean = extract_json_text(clean)  # fence anywhere / leading prose (prompt_rules)
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(clean)
        if isinstance(result, dict) and "type" in result:
            logger.info(f"AI repaired action block: type={result['type']}")
            return result
        return None
    except Exception as e:
        logger.debug(f"AI action repair failed (non-critical): {e}")
        return None
