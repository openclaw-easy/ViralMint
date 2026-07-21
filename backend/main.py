# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""FastAPI application factory."""
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path

from backend.config import settings
from backend.core.logging_config import setup_logging
from backend.core import plugins
from backend.database import init_db

# Pillow 11 removed the legacy resampling constants (Image.ANTIALIAS, .BICUBIC, ...).
# moviepy 1.0.3 still uses Image.ANTIALIAS internally. Re-add the constants as
# aliases of the new Resampling enum so resize/clip operations don't crash.
# Drop this shim if/when we move to moviepy 2.x.
try:
    from PIL import Image as _PIL_Image
    if not hasattr(_PIL_Image, "ANTIALIAS"):
        _PIL_Image.ANTIALIAS = _PIL_Image.Resampling.LANCZOS
        _PIL_Image.BICUBIC = _PIL_Image.Resampling.BICUBIC
        _PIL_Image.LINEAR = _PIL_Image.Resampling.BILINEAR
        _PIL_Image.NEAREST = _PIL_Image.Resampling.NEAREST
except Exception:
    pass
from backend.api import captions, channels, chat, chat_sessions, config as config_router, downloaded, generate, jobs, media, messaging as messaging_router, news, scout, settings as settings_router, templates, tools, videos

# Initialize logging before anything else
setup_logging(debug=settings.DEBUG)


async def _cleanup_orphaned_jobs():
    """Mark jobs stuck in pending/running as failed — they were lost on server restart."""
    from datetime import datetime
    from sqlalchemy import select, update
    from backend.database import AsyncSessionLocal
    from backend.models.job import Job
    import logging
    logger = logging.getLogger(__name__)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Job).where(Job.status.in_(["pending", "running"]))
        )
        orphans = result.scalars().all()
        if orphans:
            for job in orphans:
                job.status = "failed"
                job.error_message = "Server restarted while job was in progress"
                job.completed_at = datetime.utcnow()
            await db.commit()
            logger.info(f"Cleaned up {len(orphans)} orphaned job(s)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup + shutdown lifecycle."""
    await init_db()

    # Mark orphaned jobs (pending/running from before restart) as failed
    try:
        await _cleanup_orphaned_jobs()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Orphaned job cleanup failed: {e}")

    # Ensure SFX directory + generated files exist
    try:
        from backend.services.sfx_service import ensure_sfx_dir
        ensure_sfx_dir()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"SFX init failed: {e}")

    # Check yt-dlp version (outdated versions get blocked by YouTube)
    try:
        from backend.services.ytdlp_service import check_ytdlp_version
        check_ytdlp_version()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"yt-dlp version check failed: {e}")

    # Start messaging channels (Telegram, WhatsApp, Discord, Slack)
    try:
        from backend.messaging.manager import messaging
        from backend.agents.planner import PlannerAgent
        from backend.database import AsyncSessionLocal
        from backend.models.user_settings import UserSettings
        from sqlalchemy import select

        _planner = PlannerAgent()

        async def _planner_callback(text: str, user_id: str) -> str:
            async with AsyncSessionLocal() as db:
                row = await db.execute(
                    select(UserSettings).where(UserSettings.user_id == user_id)
                )
                user_settings = row.scalar_one_or_none()
            return await _planner.handle_message_text(
                message=text, user_settings=user_settings, user_id=user_id,
            )

        messaging.set_planner_callback(_planner_callback)
        await messaging.start_all()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Messaging startup failed: {e}")

    yield

    # Cleanup on shutdown
    try:
        from backend.messaging.manager import messaging
        await messaging.stop_all()
    except Exception:
        pass


def create_app() -> FastAPI:
    app = FastAPI(
        title="ViralMint API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.DEBUG else None,
        redoc_url=None,
    )

    # CORS — allow frontend dev server
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.FRONTEND_URL, "http://localhost:5173", "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # CSRF defense: reject state-changing requests whose Origin/Referer is not
    # in the allowlist. Loopback-binding (HOST=127.0.0.1) blocks LAN attacks,
    # but any site the user visits in a browser can still POST to 127.0.0.1
    # with credentials — CORS hides the response on simple requests but the
    # side effect has already happened. This middleware rejects them up front.
    #
    # LAN mode: OSS documents an optional HOST=0.0.0.0 so a phone on the same
    # WiFi can drive the UI at http://<lan-ip>:16888. That legitimate traffic
    # carries an Origin the loopback allowlist can't enumerate (the LAN IP is
    # unknown ahead of time), so when the user has explicitly opted into LAN
    # exposure we skip the strict origin check entirely. On the loopback
    # default (127.0.0.1) we enforce it.
    lan_mode = settings.HOST == "0.0.0.0"
    allowed_origins = {
        settings.FRONTEND_URL,
        "http://localhost:5173",
        "http://localhost:3000",
        f"http://localhost:{settings.PORT}",
        f"http://127.0.0.1:{settings.PORT}",
    }
    safe_methods = {"GET", "HEAD", "OPTIONS"}

    def _origin_ok(header: str) -> bool:
        if not header:
            return False
        try:
            parsed = urlparse(header)
            origin = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            return False
        return origin in allowed_origins

    @app.middleware("http")
    async def csrf_origin_check(request: Request, call_next):
        if lan_mode or request.method in safe_methods:
            return await call_next(request)
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        # No Origin AND no Referer → non-browser client (CLI, server) → allow.
        # If either is present, at least one must be in the allowlist.
        if origin or referer:
            if not (_origin_ok(origin) or _origin_ok(referer)):
                return JSONResponse(
                    {"detail": "Forbidden: invalid origin"}, status_code=403
                )
        return await call_next(request)

    # Register API routers
    app.include_router(chat.router)
    app.include_router(jobs.router, prefix="/api")
    app.include_router(scout.router, prefix="/api")
    app.include_router(settings_router.router, prefix="/api")
    app.include_router(videos.router, prefix="/api")
    app.include_router(downloaded.router, prefix="/api")
    app.include_router(chat_sessions.router, prefix="/api")
    app.include_router(media.router, prefix="/api")
    app.include_router(config_router.router, prefix="/api")
    app.include_router(channels.router, prefix="/api")
    app.include_router(news.router, prefix="/api")
    app.include_router(generate.router, prefix="/api")
    app.include_router(templates.router, prefix="/api")
    app.include_router(captions.router, prefix="/api")
    app.include_router(tools.router, prefix="/api")
    app.include_router(messaging_router.router, prefix="/api")

    # Load proprietary overlay (no-op if not installed) and register plugin routers.
    # See docs/OVERLAY.md for the contract.
    overlay = plugins.load_overlay()
    if overlay:
        import logging
        logging.getLogger(__name__).info(f"Loaded overlay package: {overlay}")
    for plugin_router in plugins.get_routers():
        app.include_router(plugin_router, prefix="/api")

    # Serve built frontend (production) — SPA with catch-all fallback.
    # In packaged builds the frontend lives inside the bundle (read-only) at
    # a path the launcher passes via VIRALMINT_FRONTEND_DIST. In dev mode
    # the env var is unset and we fall back to the relative path.
    import os as _os
    dist = Path(_os.environ.get("VIRALMINT_FRONTEND_DIST", "frontend/dist"))
    if dist.exists():
        # Serve static assets (js, css, images). Vite content-hashes every
        # filename under /assets, so a given URL's bytes never change — mark them
        # `immutable` + long max-age so the browser serves them straight from
        # disk cache on every subsequent load WITHOUT a revalidation round-trip.
        # Plain StaticFiles emits only etag/last-modified (no Cache-Control),
        # forcing a conditional GET per asset on every load. Safe across updates:
        # a content change means a new hashed filename, and the no-cache
        # index.html shell (below) always points at the current set.
        class _ImmutableStaticFiles(StaticFiles):
            async def get_response(self, path, scope):
                response = await super().get_response(path, scope)
                if response.status_code == 200:
                    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return response

        app.mount("/assets", _ImmutableStaticFiles(directory=str(dist / "assets")), name="static_assets")

        # SPA catch-all: any non-API route serves index.html.
        dist_root = dist.resolve()
        index_file = dist / "index.html"

        # The SPA shell must NEVER be heuristically cached. FileResponse sets
        # last-modified + etag but no Cache-Control, so browsers heuristically
        # cache index.html — which means that after an app UPDATE the browser
        # keeps serving the OLD index.html (pointing at old hashed JS). `no-cache`
        # forces revalidation on every load (cheap 304 when unchanged, fresh
        # shell after an update). The /assets/* bundles are content-hashed and
        # served `immutable` by the mount above, so they cache without revalidation.
        shell_headers = {"Cache-Control": "no-cache"}

        @app.get("/{full_path:path}")
        async def serve_spa(request: Request, full_path: str):
            if not full_path:
                return FileResponse(index_file, headers=shell_headers)
            # Resolve path and confirm it lives inside dist_root before serving —
            # prevents `GET /..%2F..%2Fetc%2Fpasswd` from escaping the SPA bundle.
            try:
                candidate = (dist / full_path).resolve()
                if candidate.is_file() and candidate.is_relative_to(dist_root):
                    return FileResponse(candidate)
            except (OSError, ValueError):
                pass
            return FileResponse(index_file, headers=shell_headers)

    return app


app = create_app()
