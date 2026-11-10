# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""24-hour cache for stock-footage search responses.

Pexels rate-limits a key (200 requests/hour by default), and a Studio plan
fires one search per scene, often with the same query twice — every cache hit
is quota kept.

Design: a standalone SQLite file in the disposable cache dir (NOT the main app
DB — this is throwaway infrastructure, safe to `rm`, no migration surface).
The searcher caches the PARSED candidate list keyed on every parameter that
shapes the result. Empty results are cached too (a valid answer) but with a
short TTL so a transient API failure can't blank a query for a day.

Every entry point is fail-open: any sqlite error (locked, corrupt, disk full)
logs at debug and behaves as a miss — a broken cache must never break a
render. All sqlite work runs via asyncio.to_thread.

INTERNAL-ONLY by license design: this caches search *responses*, never clip
files, and must never be exposed through an API endpoint or UI — a
user-facing clip browser would drift toward the "competing service" clause
in the Pexels license.
"""
import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from contextlib import closing
from contextvars import ContextVar
from pathlib import Path

from backend.config import settings

logger = logging.getLogger(__name__)

TTL_SECONDS = 24 * 3600
# Empty results expire fast so an API hiccup doesn't hide a query for a day.
EMPTY_TTL_SECONDS = 3600
# Sweep expired rows roughly every N writes (cheap, keeps the file small).
_SWEEP_EVERY = 50
_put_count = 0


def _db_path() -> Path:
    d = settings.CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "stock_search.db"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), timeout=5)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS search_cache ("
        "  key TEXT PRIMARY KEY,"
        "  payload TEXT NOT NULL,"
        "  fetched_at REAL NOT NULL,"
        "  empty INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    return con


def make_key(source: str, query: str, **params) -> str:
    """Stable cache key over every parameter that shapes the API response.
    Query is case/whitespace-normalized; params are canonically ordered."""
    canonical = json.dumps(
        {"source": source, "query": (query or "").strip().lower(), **params},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_sync(key: str) -> list | None:
    with closing(_connect()) as con:
        row = con.execute(
            "SELECT payload, fetched_at, empty FROM search_cache WHERE key = ?",
            (key,),
        ).fetchone()
    if not row:
        return None
    payload, fetched_at, empty = row
    ttl = EMPTY_TTL_SECONDS if empty else TTL_SECONDS
    if time.time() - float(fetched_at) > ttl:
        return None
    parsed = json.loads(payload)
    return parsed if isinstance(parsed, list) else None


def _put_sync(key: str, candidates: list) -> None:
    global _put_count
    now = time.time()
    with closing(_connect()) as con:
        con.execute(
            "INSERT OR REPLACE INTO search_cache (key, payload, fetched_at, empty) "
            "VALUES (?, ?, ?, ?)",
            (key, json.dumps(candidates, ensure_ascii=False), now,
             0 if candidates else 1),
        )
        _put_count += 1
        if _put_count % _SWEEP_EVERY == 0:
            con.execute(
                "DELETE FROM search_cache WHERE fetched_at < ?",
                (now - TTL_SECONDS,),
            )
        con.commit()


async def get(source: str, query: str, **params) -> list | None:
    """Fresh cached candidate list for this exact search, or None (miss/stale/
    error). Returned lists are fresh json.loads copies — callers may mutate."""
    try:
        return await asyncio.to_thread(_get_sync, make_key(source, query, **params))
    except Exception as e:
        logger.debug("stock search cache get failed (treating as miss): %s", e)
        return None


async def put(source: str, query: str, candidates: list, **params) -> None:
    """Best-effort store; never raises."""
    try:
        await asyncio.to_thread(
            _put_sync, make_key(source, query, **params), candidates or [],
        )
    except Exception as e:
        logger.debug("stock search cache put failed (skipping): %s", e)


# A search can survive a refusal (Pixabay answered, the first Pexels call had
# clips), so nothing raises as far as the planner: this is how it can still say
# why a scene came up short. The list is shared with the gathered searches.
_refusals: ContextVar[list | None] = ContextVar("stock_search_refusals", default=None)


def watch_refusals() -> list:
    """From here on in this task, rate-limited searches land in the returned list."""
    seen: list = []
    _refusals.set(seen)
    return seen


def note_refusal(source: str) -> None:
    seen = _refusals.get()
    if seen is not None:
        seen.append(source)


# In-flight dedup: a plan fires up to 18 scene
# searches concurrently and continuity makes identical queries common — on a
# cache miss, every duplicate used to hit the API. One task per key; joiners
# await it and get a private copy.
_inflight: dict[str, "asyncio.Task"] = {}


async def cached_search(source: str, query: str, fetch, **params) -> list:
    """Cache-through search with single-flight semantics.

    ``fetch`` is a zero-arg async callable performing the real API call and
    returning the candidate list. Flow: fresh cache hit → return copy;
    miss → join (or start) the single in-flight fetch for this key, store the
    result, return a private copy to every joiner. Cache failures fall
    through to a live fetch (fail-open); fetch failures propagate to every
    joiner (they would all have failed identically).
    """
    key = make_key(source, query, **params)
    try:
        cached = await asyncio.to_thread(_get_sync, key)
    except Exception as e:
        logger.debug("stock search cache get failed (treating as miss): %s", e)
        cached = None
    if cached is not None:
        return cached

    task = _inflight.get(key)
    if task is None:
        async def _run() -> list:
            try:
                result = await fetch()
                try:
                    await asyncio.to_thread(_put_sync, key, result or [])
                except Exception as e:  # best-effort store
                    logger.debug("stock search cache put failed (skipping): %s", e)
                return result or []
            finally:
                _inflight.pop(key, None)
        task = asyncio.create_task(_run())
        _inflight[key] = task

    result = await asyncio.shield(task)
    # Private copies: callers mutate candidates (score bonuses) — sharing one
    # list across joiners would double-apply them.
    return [dict(c) if isinstance(c, dict) else c for c in result]
