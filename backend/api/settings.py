# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""REST /api/settings — user settings CRUD + credential testing + OAuth callbacks."""
import logging
from datetime import datetime
from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.user_settings import UserSettings
from backend.core.crypto import encrypt
from backend.core.http_utils import get_user_agent
from backend.config import settings as app_settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class SettingsResponse(BaseModel):
    # AI provider preference (BYOK)
    ai_provider: str = "openai"
    ai_model: Optional[str] = None
    ai_api_key_set: bool = False  # masked — never returns the actual key

    # Service API keys (BYOK) — booleans only, never the plaintext key
    youtube_api_key_set: bool = False

    # OAuth / cookie status
    douyin_cookie_set: bool = False
    tiktok_cookie_set: bool = False
    youtube_channel_title: Optional[str] = None
    youtube_connected: bool = False
    tiktok_upload_connected: bool = False

    # Preferences
    preferred_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    tts_provider: str = "edge_tts"
    preferred_tts_voice: Optional[str] = None
    caption_style: str = "viral"
    caption_enabled: bool = True
    music_enabled: bool = True
    music_genre: str = "lofi"
    music_volume_db: float = -14.0
    upload_to_youtube: bool = True
    upload_to_tiktok: bool = False
    tiktok_default_privacy: str = "PUBLIC_TO_EVERYONE"

    class Config:
        from_attributes = True


class SettingsUpdate(BaseModel):
    # AI provider preference (BYOK)
    ai_provider: Optional[str] = None
    ai_model: Optional[str] = None
    ai_api_key: Optional[str] = None  # plaintext on input — encrypted before storage

    # Service API keys (BYOK) — plaintext on input, encrypted before storage.
    # Send empty string to clear a stored key.
    youtube_api_key: Optional[str] = None

    # Preferences
    preferred_voice_id: Optional[str] = None
    tts_provider: Optional[str] = None
    preferred_tts_voice: Optional[str] = None
    caption_style: Optional[str] = None
    caption_enabled: Optional[bool] = None
    music_enabled: Optional[bool] = None
    music_genre: Optional[str] = None
    music_volume_db: Optional[float] = None
    upload_to_youtube: Optional[bool] = None
    upload_to_tiktok: Optional[bool] = None
    tiktok_default_privacy: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/settings", response_model=SettingsResponse)
async def get_settings():
    """Return user settings with secrets masked."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == "local")
        )
        s = result.scalar_one_or_none()

    if not s:
        return SettingsResponse()

    return SettingsResponse(
        ai_provider=s.ai_provider or "openai",
        ai_model=s.ai_model,
        ai_api_key_set=bool(s.ai_api_key_encrypted),
        youtube_api_key_set=bool(s.youtube_api_key_encrypted),
        douyin_cookie_set=bool(s.douyin_cookie_encrypted),
        tiktok_cookie_set=bool(s.tiktok_cookie_encrypted),
        youtube_channel_title=s.youtube_channel_title,
        youtube_connected=bool(s.youtube_credentials_json_encrypted),
        tiktok_upload_connected=bool(s.tiktok_upload_token_encrypted),
        preferred_voice_id=s.preferred_voice_id or "21m00Tcm4TlvDq8ikWAM",
        tts_provider=s.tts_provider or "edge_tts",
        preferred_tts_voice=s.preferred_tts_voice,
        caption_style=s.caption_style or "viral",
        caption_enabled=s.caption_enabled if s.caption_enabled is not None else True,
        music_enabled=s.music_enabled if s.music_enabled is not None else True,
        music_genre=s.music_genre or "lofi",
        music_volume_db=s.music_volume_db if s.music_volume_db is not None else -14.0,
        upload_to_youtube=s.upload_to_youtube if s.upload_to_youtube is not None else True,
        upload_to_tiktok=s.upload_to_tiktok or False,
        tiktok_default_privacy=s.tiktok_default_privacy or "PUBLIC_TO_EVERYONE",
    )


@router.post("/settings", response_model=SettingsResponse)
async def update_settings(update: SettingsUpdate):
    """Update user settings. Keys are encrypted before saving."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == "local")
        )
        s = result.scalar_one_or_none()
        if not s:
            s = UserSettings(user_id="local")
            db.add(s)

        # AI provider preference (BYOK)
        if update.ai_provider is not None:
            s.ai_provider = update.ai_provider
        if update.ai_model is not None:
            s.ai_model = update.ai_model or None
        if update.ai_api_key is not None:
            # Empty string clears the stored key; otherwise encrypt + store
            s.ai_api_key_encrypted = encrypt(update.ai_api_key) if update.ai_api_key else None

        # Service API keys (BYOK) — empty string clears, populated value encrypts
        if update.youtube_api_key is not None:
            s.youtube_api_key_encrypted = encrypt(update.youtube_api_key) if update.youtube_api_key else None

        # Preference fields
        if update.preferred_voice_id is not None:
            s.preferred_voice_id = update.preferred_voice_id
        if update.tts_provider is not None:
            s.tts_provider = update.tts_provider
        if update.preferred_tts_voice is not None:
            s.preferred_tts_voice = update.preferred_tts_voice
        if update.caption_style is not None:
            s.caption_style = update.caption_style
        if update.caption_enabled is not None:
            s.caption_enabled = update.caption_enabled
        if update.music_enabled is not None:
            s.music_enabled = update.music_enabled
        if update.music_genre is not None:
            s.music_genre = update.music_genre
        if update.music_volume_db is not None:
            s.music_volume_db = update.music_volume_db
        if update.upload_to_youtube is not None:
            s.upload_to_youtube = update.upload_to_youtube
        if update.upload_to_tiktok is not None:
            s.upload_to_tiktok = update.upload_to_tiktok
        if update.tiktok_default_privacy is not None:
            s.tiktok_default_privacy = update.tiktok_default_privacy

        await db.commit()

    return await get_settings()


# ── OAuth endpoints ──────────────────────────────────────────────────────────


@router.get("/settings/youtube-auth")
async def youtube_auth():
    """Redirect user to Google OAuth for YouTube upload."""
    if not app_settings.YOUTUBE_CLIENT_ID or not app_settings.YOUTUBE_CLIENT_SECRET:
        return {"error": "YouTube OAuth client ID/secret not configured in .env"}
    from backend.services.youtube_uploader import build_youtube_auth_url
    url = build_youtube_auth_url()
    return RedirectResponse(url)


@router.get("/settings/youtube-callback")
async def youtube_callback(code: str = Query(...), state: str = Query(None)):
    """Handle YouTube OAuth callback — exchange code for credentials."""
    from backend.services.youtube_uploader import exchange_youtube_code
    try:
        result = await exchange_youtube_code(code)
        creds_json = result["credentials"]
        channel_title = result.get("channel_title", "")

        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(UserSettings).where(UserSettings.user_id == "local")
            )
            s = res.scalar_one_or_none()
            if not s:
                s = UserSettings(user_id="local")
                db.add(s)
            import json
            s.youtube_credentials_json_encrypted = encrypt(json.dumps(creds_json))
            s.youtube_channel_title = channel_title
            await db.commit()

        return RedirectResponse(f"/?youtube_connected=true&channel={channel_title}")
    except Exception as e:
        logger.error(f"YouTube OAuth callback failed: {e}")
        return RedirectResponse(f"/?youtube_error={str(e)[:200]}")


@router.get("/settings/tiktok-upload-auth")
async def tiktok_upload_auth():
    """Redirect user to TikTok OAuth for upload."""
    if not app_settings.TIKTOK_CLIENT_KEY or not app_settings.TIKTOK_CLIENT_SECRET:
        return {"error": "TikTok client key/secret not configured in .env"}
    from backend.services.tiktok_uploader import build_tiktok_auth_url
    url = build_tiktok_auth_url()
    return RedirectResponse(url)


@router.get("/settings/tiktok-upload-callback")
async def tiktok_upload_callback(code: str = Query(...), state: str = Query(None)):
    """Handle TikTok OAuth callback — exchange code for tokens."""
    from backend.services.tiktok_uploader import exchange_tiktok_code
    try:
        result = await exchange_tiktok_code(code)

        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(UserSettings).where(UserSettings.user_id == "local")
            )
            s = res.scalar_one_or_none()
            if not s:
                s = UserSettings(user_id="local")
                db.add(s)
            s.tiktok_upload_token_encrypted = encrypt(result["access_token"])
            if result.get("refresh_token"):
                s.tiktok_upload_refresh_token_encrypted = encrypt(result["refresh_token"])
            if result.get("expires_in"):
                from datetime import timedelta
                s.tiktok_upload_token_expiry = datetime.utcnow() + timedelta(seconds=result["expires_in"])
            await db.commit()

        return RedirectResponse("/?tiktok_connected=true")
    except Exception as e:
        logger.error(f"TikTok OAuth callback failed: {e}")
        return RedirectResponse(f"/?tiktok_error={str(e)[:200]}")


@router.post("/settings/test-cookie")
async def test_cookie(body: dict):
    """Test a platform session cookie."""
    platform = body.get("platform", "")
    cookie = body.get("cookie", "")
    if not cookie:
        return {"ok": False, "error": "No cookie provided"}

    if platform == "tiktok":
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://www.tiktok.com/api/user/detail/",
                    cookies={"sessionid": cookie},
                    headers={"User-Agent": get_user_agent()},
                )
                if resp.status_code == 200 and resp.json().get("userInfo"):
                    return {"ok": True}
                return {"ok": False, "error": "Cookie appears invalid"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif platform == "douyin":
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://www.douyin.com/aweme/v1/web/user/profile/self/",
                    cookies={"sessionid": cookie},
                    headers={"User-Agent": get_user_agent()},
                )
                if resp.status_code == 200:
                    return {"ok": True}
                return {"ok": False, "error": "Cookie appears invalid"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"Unknown platform: {platform}"}


@router.get("/settings/health")
async def get_health():
    """Return system health status."""
    import shutil
    import subprocess

    health = {}

    # Cookie health
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(UserSettings).where(UserSettings.user_id == "local")
        )
        s = res.scalar_one_or_none()

    if s:
        for platform, cookie_field, set_at_field in [
            ("douyin_cookie", "douyin_cookie_encrypted", "douyin_cookie_set_at"),
            ("tiktok_cookie", "tiktok_cookie_encrypted", "tiktok_cookie_set_at"),
        ]:
            cookie = getattr(s, cookie_field, None) if s else None
            set_at = getattr(s, set_at_field, None) if s else None
            if cookie and set_at:
                age = (datetime.utcnow() - set_at).days
                status = "valid" if age < 25 else ("expiring_soon" if age < 30 else "expired")
                health[platform] = {"status": status, "age_days": age}
            elif cookie:
                health[platform] = {"status": "unknown_age"}
            else:
                health[platform] = {"status": "not_configured"}
    else:
        health["douyin_cookie"] = {"status": "not_configured"}
        health["tiktok_cookie"] = {"status": "not_configured"}

    # ImageMagick
    if shutil.which("magick") or shutil.which("convert"):
        health["imagemagick"] = {"status": "ok"}
    else:
        health["imagemagick"] = {"status": "not_found"}

    # yt-dlp
    try:
        r = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5)
        health["ytdlp"] = {"status": "ok", "version": r.stdout.strip()}
    except Exception:
        health["ytdlp"] = {"status": "not_found"}

    # AI provider configured (BYOK)
    has_ai_key = bool(
        app_settings.ANTHROPIC_API_KEY
        or app_settings.OPENAI_API_KEY
        or (s and s.ai_api_key_encrypted)
    )
    health["ai_provider"] = {"status": "configured" if has_ai_key else "not_configured"}

    # YouTube API key (BYOK — per-user encrypted or .env fallback)
    has_youtube_key = bool(
        app_settings.YOUTUBE_API_KEY
        or (s and s.youtube_api_key_encrypted)
    )
    health["youtube_api_key"] = {"status": "configured" if has_youtube_key else "not_configured"}

    # YouTube quota
    from backend.services.youtube_quota import _usage_today, DAILY_LIMIT
    health["youtube_quota"] = {"used": _usage_today.get("count", 0), "limit": DAILY_LIMIT}

    # Occupancy of the download / transcription thread pools. A non-zero
    # `abandoned` is the one clear "this process wants a restart" signal: a
    # stalled download or Whisper call that was given up on is still holding
    # its worker thread (Python cannot kill a thread).
    from backend.core.executors import all_pool_stats
    health["thread_pools"] = all_pool_stats()

    return health


@router.post("/settings/open-folder")
async def open_folder(body: dict):
    """Open a storage folder in the OS file manager."""
    import platform, subprocess
    from pathlib import Path

    folder_map = {
        "videos": app_settings.VIDEOS_DIR,
        "audio": app_settings.AUDIO_DIR,
        "generated": app_settings.GENERATED_DIR,
        "thumbnails": app_settings.THUMBNAILS_DIR,
        "storage": app_settings.STORAGE_ROOT,
    }

    folder_key = body.get("folder", "storage")
    target = folder_map.get(folder_key, app_settings.STORAGE_ROOT)
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", str(target)])
        elif system == "Windows":
            subprocess.Popen(["explorer", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return {"ok": True, "path": str(target)}
    except Exception as e:
        return {"ok": False, "error": str(e), "path": str(target)}


# ── Motion Graphics (HyperFrames) — on-demand Node plugin ─────────────────────
# Nothing motion-related ships in the base install. The portable Node runtime
# and the HyperFrames npm package are downloaded on opt-in and live entirely in
# the user's data dir, so the feature can be added and removed without touching
# the app. Rendering is local (headless Chrome + FFmpeg) and costs nothing.

@router.get("/settings/motion-graphics/status")
async def motion_graphics_status():
    """Plugin install state for the Settings card to poll (every 2s)."""
    from backend.services.hyperframes_service import HyperFramesService
    return HyperFramesService.get_install_state()


@router.post("/settings/motion-graphics/install")
async def motion_graphics_install():
    """Trigger the plugin install (portable Node + npm install) in the
    background. Idempotent; returns immediately. The UI polls status for
    progress."""
    from backend.services.hyperframes_service import HyperFramesService
    return await HyperFramesService.install_plugin()


@router.post("/settings/motion-graphics/uninstall")
async def motion_graphics_uninstall():
    """Remove the entire motion/ plugin dir."""
    from backend.services.hyperframes_service import HyperFramesService
    return await HyperFramesService.uninstall_plugin()


@router.post("/settings/motion-graphics/test")
async def motion_graphics_test():
    """End-to-end smoke render of the bundled kinetic-hook template.

    Confirms the whole chain (portable Node → HyperFrames CLI → headless Chrome
    → FFmpeg) still produces a real MP4. The status is always 200 — the `ok`
    flag carries the verdict, so the UI renders a result rather than an error.
    """
    import time as _time
    from backend.services.hyperframes_service import HyperFramesService
    from backend.services.motion_render_service import MotionRenderService

    if not HyperFramesService.is_installed():
        return {
            "ok": False, "latency_ms": 0, "output_size_kb": None,
            "error": "not_installed",
            "detail": "Motion Graphics isn't installed yet. Click Install above first.",
        }

    start = _time.monotonic()
    try:
        out = await MotionRenderService.render(
            template_id="kinetic_hook",
            variables={
                "kicker": "MOTION GRAPHICS",
                "title": "It Works",
                "subtitle": "rendered locally",
                "accent": "#ffd60a",
                "bg": "#0b0b14",
            },
            aspect="9:16",
            # A verification render, not a deliverable: draft quality and no
            # supersample keep it fast.
            quality="draft",
            supersample=False,
        )
        latency_ms = round((_time.monotonic() - start) * 1000)
        size_kb = round(out.stat().st_size / 1024)
        # Smoke test only — don't keep the output around.
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "ok": True, "latency_ms": latency_ms,
            "output_size_kb": size_kb, "error": None, "detail": None,
        }
    except Exception as e:
        latency_ms = round((_time.monotonic() - start) * 1000)
        logger.info(
            "Motion Graphics smoke test failed in %dms: %s: %s",
            latency_ms, type(e).__name__, str(e)[:200],
        )
        return {
            "ok": False, "latency_ms": latency_ms, "output_size_kb": None,
            "error": type(e).__name__, "detail": str(e)[:200],
        }
