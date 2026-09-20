# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The ONE browser-origin allowlist for the local server.

Used by the HTTP CSRF middleware (backend/main.py) AND the chat WebSocket
(backend/api/chat.py). Starlette's `@app.middleware("http")` never sees a
websocket scope, so before this module existed a page on ANY origin the user
happened to visit could open `ws://127.0.0.1:16888/ws/chat`, drive the planner
(dispatching real jobs, cancelling the user's) and receive every event the app
streams — transcripts, titles, file paths. Rule #32: derive, don't duplicate;
both checks read this one set.

LAN mode (`HOST=0.0.0.0`, a documented option so a phone on the same WiFi can
drive the UI) has an origin nobody can enumerate ahead of time — the machine's
LAN IP — so the strict check is skipped there, exactly as the HTTP middleware
has always done. A concrete non-loopback bind IS enumerable and stays enforced.
"""
from __future__ import annotations

from urllib.parse import urlparse

from backend.config import settings


def lan_mode() -> bool:
    """True when the user opted into LAN exposure, whose origin is unknowable."""
    return settings.HOST == "0.0.0.0"


def allowed_origins() -> set[str]:
    origins = {
        settings.FRONTEND_URL,
        "http://localhost:5173",
        "http://localhost:3000",
        f"http://localhost:{settings.PORT}",
        f"http://127.0.0.1:{settings.PORT}",
    }
    if settings.HOST not in ("0.0.0.0", "127.0.0.1", "localhost"):
        origins.add(f"http://{settings.HOST}:{settings.PORT}")
    return origins


def origin_ok(header: str | None) -> bool:
    """True when an Origin/Referer header names one of our own origins.

    An ABSENT header is the caller's decision to make (both callers allow it:
    a non-browser client — CLI, messaging bridge, test — sends none). This only
    judges a header that is present.
    """
    if not header:
        return False
    try:
        parsed = urlparse(header)
        origin = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return False
    return origin in allowed_origins()
