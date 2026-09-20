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

from backend.api import captions, channels, chat, chat_sessions, config as config_router, downloaded, generate, jobs, library, media, messaging as messaging_router, news, scout, settings as settings_router, templates, tools, tts_preview, videos

# Initialize logging before anything else
setup_logging(debug=settings.DEBUG)


async def _watch_handoff_jobs(poll_seconds: int = 60, watch_ids=None):
    """Watch the running/pending jobs that survived the boot sweep because their
    heartbeat was FRESH — they may belong to a PREDECESSOR backend still draining
    through a restart handoff (init_db's sweep fails only STALE jobs; see
    database.sweep_stale_jobs).

    The watch-set is the boot sweep's OWN output (database.BOOT_FRESH_JOB_IDS),
    not a fresh "everything running" query. That distinction is load-bearing: by
    the time this coroutine first runs, the server may already be serving, so a
    re-query could adopt a job THIS instance just created — and a job with a long
    silent step (Whisper on a big file can outlast the grace period without a
    progress tick) would then be failed while perfectly alive, which is the exact
    bug this machinery exists to prevent.

    Each pass re-checks that set and fails any job that went stale (predecessor
    exited mid-job). Exits once every watched job is terminal or swept.

    Replaces the old _cleanup_orphaned_jobs, which was dead code (init_db's sweep
    always ran first and left it nothing to do) and, worse in spirit, assumed any
    running job at boot was dead — the assumption that stamps a still-completing
    generation "Server restarted — job did not complete".
    """
    import asyncio
    import logging
    from backend.database import BOOT_FRESH_JOB_IDS, sweep_stale_jobs
    logger = logging.getLogger(__name__)

    watched = list(BOOT_FRESH_JOB_IDS if watch_ids is None else watch_ids)
    if not watched:
        return
    logger.info("Handoff watcher: tracking %d possibly-draining job(s)", len(watched))

    while watched:
        await asyncio.sleep(poll_seconds)
        swept, watched = await sweep_stale_jobs(only_ids=watched)
        if swept:
            logger.warning(
                "Handoff watcher: failed %d job(s) whose heartbeat went stale "
                "(predecessor exited mid-job); %d still draining", swept, len(watched))
    logger.info("Handoff watcher: done — all watched jobs terminal or swept")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup + shutdown lifecycle."""
    await init_db()

    # Watch the running/pending jobs the boot sweep left alone (a fresh
    # heartbeat means possibly a predecessor instance still draining them
    # through a restart handoff). Background — must never block startup.
    try:
        from backend.core.task_runner import spawn_background
        spawn_background(_watch_handoff_jobs())
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Handoff job watcher failed to start: {e}")

    # Reclaim clip files orphaned by a process death. Clips are cut straight
    # into GENERATED_DIR under their final names (no scratch dir), so a
    # backend killed between the ffmpeg fan-out and the DB save leaves mp4s no
    # Library row references. They only appear BECAUSE a process died, so boot
    # is exactly the right moment to sweep — no periodic scheduler needed. The
    # 24h age gate inside the purge keeps it away from anything in flight.
    # Background + best-effort: startup must never wait on a filesystem walk.
    try:
        from backend.core.task_runner import spawn_background

        async def _purge_orphan_clips():
            import asyncio as _asyncio
            from backend.services.clip_extractor import purge_orphan_clip_files
            try:
                await _asyncio.to_thread(purge_orphan_clip_files)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(f"Orphan clip purge failed: {exc}")

        spawn_background(_purge_orphan_clips())
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Orphan clip purge failed to start: {e}")

    # Reclaim the cutting bench's image caches. `THUMBNAILS_DIR/frames/` holds
    # one JPEG per (source, mtime, width, 0.1s bucket) and gets written on
    # every drag settle — an editing session leaves hundreds; `strips/` holds
    # one contact sheet per (source, mtime, cells, height). Neither is
    # referenced by a DB row, both rebuild on demand in milliseconds warm, and
    # deleting a source already drops its own entries. This is the backstop for
    # what a crash — or a source deleted before this shipped — left behind.
    #
    # Age, not size, and boot rather than a timer: 30 days keeps a strip warm
    # for a video someone comes back to next week, and there is no scheduler
    # here to hang a daily job on. Background + best-effort.
    try:
        from backend.core.task_runner import spawn_background

        async def _purge_bench_caches():
            import asyncio as _asyncio
            import logging
            import time as _time

            def _sweep() -> int:
                cutoff = _time.time() - 30 * 86400
                removed = 0
                for name in ("frames", "strips"):
                    root = settings.THUMBNAILS_DIR / name
                    if not root.is_dir():
                        continue
                    for entry in root.iterdir():
                        try:
                            if entry.is_file() and entry.stat().st_mtime < cutoff:
                                entry.unlink(missing_ok=True)
                                removed += 1
                        except OSError:
                            continue
                return removed

            try:
                n = await _asyncio.to_thread(_sweep)
                if n:
                    logging.getLogger(__name__).info(
                        "Bench cache purge: removed %d cached image(s)", n)
            except Exception as exc:
                logging.getLogger(__name__).warning(f"Bench cache purge failed: {exc}")

        spawn_background(_purge_bench_caches())
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Bench cache purge failed to start: {e}")

    # Bound the two tables that only ever grew: `jobs` (every scout, download,
    # analyze and tool run adds a row) and `scout_results` (up to 50 per niche
    # per platform per run). There is no periodic scheduler here, and these are
    # not urgent — a table one boot too large is harmless — so boot is a fine
    # cadence. Background + best-effort: startup must never wait on it.
    #
    # ⚠️ Both sweeps are defined by what a row IS, not by age alone: a
    # successful tool job is the Library item for its file, and a scout row a
    # download points back at is still load-bearing. Neither is ever deleted.
    try:
        from backend.core.task_runner import spawn_background

        async def _sweep_retention():
            import logging
            from backend.database import AsyncSessionLocal
            from backend.services import job_retention
            try:
                async with AsyncSessionLocal() as db:
                    await job_retention.sweep(db)
                    await job_retention.sweep_scout(db)
            except Exception as exc:
                logging.getLogger(__name__).warning(f"Retention sweep failed: {exc}")

        spawn_background(_sweep_retention())
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Retention sweep failed to start: {e}")

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
    # Only HOST=0.0.0.0 has an unenumerable origin (the phone hits the machine's
    # LAN IP, unknown ahead of time) → skip the strict check there. A concrete
    # non-loopback bind (e.g. HOST=192.168.1.5) DOES have an enumerable origin,
    # so we keep CSRF enforced and just allowlist it below. Reverse-proxy / custom
    # domain deployments set FRONTEND_URL, which is already allowlisted.
    # The allowlist itself lives in backend/core/origins.py so the chat
    # WebSocket — which this HTTP middleware never sees — judges Origin by the
    # same rule and the same set.
    from backend.core.origins import lan_mode as _lan_mode, origin_ok as _origin_ok
    lan_mode = _lan_mode()
    safe_methods = {"GET", "HEAD", "OPTIONS"}

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
    app.include_router(library.router, prefix="/api")
    app.include_router(messaging_router.router, prefix="/api")
    app.include_router(tts_preview.router, prefix="/api")

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
