# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Stock media search — one shape for every provider.

The stock-video pipeline asks for clips matching a query and gets back
`StockClip` dictionaries. Which provider answered (Pexels, Pixabay, one added
later) never reaches it; the provider name is carried only for diagnostics.

Adding a provider means writing an adapter that turns that provider's JSON
into `StockVariant`s. Orientation filtering, the minimum resolution and the
ranking are shared (`_select`, `_score`) so every provider is judged by the
same rules. Quirks stay in the adapter that owns them: authentication,
paging limits, per-size URL fields, an empty `large` rendition.

Keys come from `.env` like the AI provider factory's do (`PEXELS_API_KEY`,
`PIXABAY_API_KEY`). `STOCK_MEDIA_PROVIDER` can pin one; `auto` — the default —
tries every configured provider with Pexels first, so an install that only
has a Pexels key gets exactly the footage it got before.
"""
import logging
from enum import Enum
from typing import TypedDict

import httpx

from backend.config import settings
from backend.core.exceptions import VideoGenerationError

logger = logging.getLogger(__name__)

# Shared client for provider search calls. Downloads keep streaming through
# pexels_service, which owns that path.
_http = httpx.AsyncClient(timeout=30, follow_redirects=True, limits=httpx.Limits(max_connections=10))

PEXELS_API = "https://api.pexels.com"
PIXABAY_API = "https://pixabay.com/api/videos/"

# Pixabay's videos endpoint rejects per_page outside this range with a 400.
PIXABAY_MIN_PER_PAGE = 3
PIXABAY_MAX_PER_PAGE = 200

# Minimum acceptable resolution for stock clips. The same bar for every
# provider: one that cannot supply it contributes nothing, rather than a clip
# that still has to be scaled up to the 1080x1920 output.
MIN_WIDTH_PORTRAIT = 720
MIN_HEIGHT_PORTRAIT = 1280
MIN_WIDTH_LANDSCAPE = 1280
MIN_HEIGHT_LANDSCAPE = 720


class StockProvider(str, Enum):
    PEXELS = "pexels"
    PIXABAY = "pixabay"


# Auto order. Pexels first: existing installs have that key, and their videos
# must keep the footage they had before this layer existed.
AUTO_ORDER = (StockProvider.PEXELS, StockProvider.PIXABAY)

# Which setting holds each provider's key — also what an error message names,
# so a missing key is a one-line fix.
KEY_SETTINGS = {
    StockProvider.PEXELS: "PEXELS_API_KEY",
    StockProvider.PIXABAY: "PIXABAY_API_KEY",
}


class StockVariant(TypedDict):
    """One downloadable rendition of a clip, provider-neutral."""

    url: str
    width: int
    height: int
    hd: bool


class StockClip(TypedDict):
    """The normalized result every caller sees. Nothing provider-specific."""

    id: int | str
    provider: str
    page_url: str
    download_url: str
    width: int
    height: int
    duration: float
    quality_score: float


def _min_dimensions(orientation: str) -> tuple[int, int]:
    if orientation == "portrait":
        return MIN_WIDTH_PORTRAIT, MIN_HEIGHT_PORTRAIT
    return MIN_WIDTH_LANDSCAPE, MIN_HEIGHT_LANDSCAPE


def _score(width: int, height: int, hd: bool) -> float:
    """Rank a rendition, independent of provider.

    Same rules the Pexels search has always used: prefer an HD rendition,
    prefer a reasonable size over a 4K one that wastes bandwidth, then higher
    resolution. The duration bonus is added per clip by the adapters.
    """
    score = 0.0
    if hd:
        score += 100
    if width <= 1920 and height <= 1920:
        score += 50
    score += min(width, 1920) / 100
    return score


def _select(variants: list[StockVariant], orientation: str) -> tuple[StockVariant, float] | None:
    """Best rendition that clears the resolution bar and matches orientation."""
    min_w, min_h = _min_dimensions(orientation)
    want_portrait = orientation == "portrait"

    best: StockVariant | None = None
    best_score = 0.0
    for variant in variants:
        width = variant.get("width") or 0
        height = variant.get("height") or 0
        if width < min_w or height < min_h:
            continue
        if (height > width) != want_portrait:
            continue
        score = _score(width, height, variant.get("hd", False))
        if best is None or score > best_score:
            best = variant
            best_score = score
    return (best, best_score) if best else None


class PexelsProvider:
    """Pexels videos API — https://www.pexels.com/api/documentation/#videos."""

    name = StockProvider.PEXELS

    def __init__(self, api_key: str, http: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self._http = http or _http

    async def search(
        self, query: str, orientation: str = "portrait", per_page: int = 10,
    ) -> list[StockClip]:
        if not self.api_key:
            raise VideoGenerationError("Pexels API key not configured")

        resp = await self._http.get(
            f"{PEXELS_API}/videos/search",
            params={"query": query, "orientation": orientation, "per_page": per_page, "size": "medium"},
            headers={"Authorization": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json() or {}

        clips: list[StockClip] = []
        for video in data.get("videos") or []:
            variants = [
                {
                    "url": f.get("link", ""),
                    "width": f.get("width") or 0,
                    "height": f.get("height") or 0,
                    "hd": f.get("quality") == "hd",
                }
                for f in (video.get("video_files") or [])
                if f.get("link")
            ]
            chosen = _select(variants, orientation)
            if not chosen:
                continue
            variant, score = chosen
            duration = float(video.get("duration") or 0)
            clips.append({
                "id": video.get("id"),
                "provider": self.name.value,
                "page_url": video.get("url", ""),
                "download_url": variant["url"],
                "width": variant["width"],
                "height": variant["height"],
                "duration": duration,
                # Longer source clips are more flexible and need less looping.
                "quality_score": score + min(duration / 5, 10),
            })

        clips.sort(key=lambda c: c["quality_score"], reverse=True)
        return clips


class PixabayProvider:
    """Pixabay videos API — https://pixabay.com/api/docs/#api_search_videos."""

    name = StockProvider.PIXABAY

    def __init__(self, api_key: str, http: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self._http = http or _http

    async def search(
        self, query: str, orientation: str = "portrait", per_page: int = 10,
    ) -> list[StockClip]:
        if not self.api_key:
            raise VideoGenerationError("Pixabay API key not configured")

        resp = await self._http.get(
            PIXABAY_API,
            params={
                "key": self.api_key,
                "q": query,
                # A caller's 1 or 500 would be a 400 from the API, so clamp it
                # here instead of failing the scene over a paging preference.
                "per_page": max(PIXABAY_MIN_PER_PAGE, min(PIXABAY_MAX_PER_PAGE, per_page)),
                "order": "popular",
                # ViralMint is a general-audience video tool and Pixabay
                # defaults this to "false", so say it explicitly.
                "safesearch": "true",
            },
        )
        resp.raise_for_status()
        data = resp.json() or {}

        clips: list[StockClip] = []
        for hit in data.get("hits") or []:
            variants: list[StockVariant] = []
            for size in (hit.get("videos") or {}).values():
                if not isinstance(size, dict):
                    continue
                url = size.get("url") or ""
                width = size.get("width") or 0
                height = size.get("height") or 0
                # Pixabay returns an empty `large` (0x0) when a video has no
                # 1080p rendition; that is a missing size, not a bad clip.
                if not url or not width or not height:
                    continue
                variants.append({
                    "url": url,
                    "width": width,
                    "height": height,
                    # Pixabay has no quality label: its 720p medium and 1080p
                    # large renditions are HD, small and tiny are not.
                    "hd": max(width, height) >= 1280,
                })
            chosen = _select(variants, orientation)
            if not chosen:
                continue
            variant, score = chosen
            duration = float(hit.get("duration") or 0)
            clips.append({
                "id": hit.get("id"),
                "provider": self.name.value,
                "page_url": hit.get("pageURL", ""),
                "download_url": variant["url"],
                "width": variant["width"],
                "height": variant["height"],
                "duration": duration,
                "quality_score": score + min(duration / 5, 10),
            })

        clips.sort(key=lambda c: c["quality_score"], reverse=True)
        return clips


PROVIDER_CLASSES = {
    StockProvider.PEXELS: PexelsProvider,
    StockProvider.PIXABAY: PixabayProvider,
}


def _api_key(provider: StockProvider) -> str:
    return (getattr(settings, KEY_SETTINGS[provider], "") or "").strip()


def configured_providers(preferred: str | None = None) -> list[StockProvider]:
    """Providers with a key, in the order they should be tried.

    `preferred` wins over `STOCK_MEDIA_PROVIDER`; either may be `auto` (or
    empty), which means every configured provider in `AUTO_ORDER`.
    """
    choice = (preferred or getattr(settings, "STOCK_MEDIA_PROVIDER", "") or "").strip().lower()
    if choice and choice != "auto":
        try:
            pinned = StockProvider(choice)
        except ValueError:
            logger.warning(
                "Unknown STOCK_MEDIA_PROVIDER=%r — expected %s or auto; using auto",
                choice, ", ".join(p.value for p in AUTO_ORDER),
            )
        else:
            return [pinned] if _api_key(pinned) else []
    return [p for p in AUTO_ORDER if _api_key(p)]


def _no_provider_message(preferred: str | None) -> str:
    choice = (preferred or settings.STOCK_MEDIA_PROVIDER or "auto").strip().lower()
    if choice in {p.value for p in AUTO_ORDER}:
        key = KEY_SETTINGS[StockProvider(choice)]
        return f"{choice} is selected as the stock media provider but {key} is not set"
    return "No stock media API key configured — set PEXELS_API_KEY or PIXABAY_API_KEY"


async def search_clips(
    query: str,
    orientation: str = "portrait",
    per_page: int = 10,
    provider: str | None = None,
) -> list[StockClip]:
    """Search stock footage for `query`, best clip first.

    Only one provider normally answers: the first configured one, in order.
    A provider that errors or finds nothing hands the query to the next one —
    an outage at one stock site should cost a fallback, not the scene. Every
    provider is skipped only when nothing is configured, which is a
    configuration error the caller needs to hear about.
    """
    providers = configured_providers(provider)
    if not providers:
        raise VideoGenerationError(_no_provider_message(provider))

    for provider_enum in providers:
        try:
            clips = await PROVIDER_CLASSES[provider_enum](
                _api_key(provider_enum),
            ).search(query, orientation=orientation, per_page=per_page)
        except Exception as e:
            logger.warning("Stock search via %s failed for %r: %s", provider_enum.value, query, e)
            continue
        if clips:
            return clips
        logger.info("No stock results from %s for %r", provider_enum.value, query)
    return []
