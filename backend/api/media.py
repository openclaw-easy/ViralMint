# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""REST /api/media — simple image upload + serve for video generation inputs."""
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse

from backend.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


@router.post("/media/upload")
async def upload_media(file: UploadFile = File(...)):
    """Upload an image for use as video generation input (I2V, start/end frame)."""
    suffix = Path(file.filename).suffix.lower() if file.filename else ""
    if suffix not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(400, f"Unsupported image type: {suffix}. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTS))}")

    MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB
    content = await file.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(413, f"Image too large ({len(content) // (1024*1024)}MB). Maximum: 20MB")

    file_id = str(uuid4())[:12]
    filename = f"{file_id}{suffix}"
    dest = settings.TMP_DIR / filename
    dest.parent.mkdir(parents=True, exist_ok=True)

    with open(dest, "wb") as f:
        f.write(content)

    size_kb = len(content) / 1024
    logger.info(f"Media uploaded: {filename} ({size_kb:.0f}KB)")

    return {
        "id": file_id,
        "filename": filename,
        "url": f"/api/media/{filename}",
        "size_kb": round(size_kb, 1),
    }


@router.get("/media/{filename}")
async def serve_media(filename: str):
    """Serve an uploaded media file."""
    # Sanitize filename to prevent path traversal, then serve ONLY the image
    # types the upload above writes. TMP_DIR is shared scratch: it also holds
    # yt-dlp's browser_cookies.txt — the user's logged-in YouTube/TikTok
    # session — and this route used to hand it to anyone who asked for it by
    # name. A page the user visits cannot read the response cross-origin, but
    # the app's own UI can, and so can any local process.
    safe_name = Path(filename).name
    if Path(safe_name).suffix.lower() not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(404, "File not found")
    path = settings.TMP_DIR / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path)
