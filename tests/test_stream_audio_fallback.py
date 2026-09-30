# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""/api/downloaded/{id}/stream plays an audio-only download.

The Library files a download whose video file is gone but whose audio file
remains under Audio, and hands out `/api/downloaded/{id}/stream` as its
stream_url (library_index._downloaded_items). The route only ever looked at
`video_path`, so every such item 404'd in the player.
"""
import asyncio
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models.downloaded_video import DownloadedVideo


@pytest.fixture(scope="module", autouse=True)
def _tables():
    from backend.database import init_db
    asyncio.run(init_db())


@pytest_asyncio.fixture
async def client():
    from backend.main import create_app
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:16888",
        headers={"Origin": "http://127.0.0.1:16888"},
    ) as c:
        yield c


async def _row(video_path, audio_path) -> str:
    async with AsyncSessionLocal() as db:
        v = DownloadedVideo(user_id="local", title="t", platform="youtube",
                            video_path=video_path, audio_path=audio_path)
        db.add(v)
        await db.commit()
        return v.id


@pytest.mark.asyncio
async def test_a_row_whose_video_is_gone_streams_its_audio(client):
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    audio = settings.AUDIO_DIR / f"{uuid.uuid4().hex}.mp3"
    audio.write_bytes(b"ID3" + b"\x00" * 4096)
    vid = await _row(str(settings.VIDEOS_DIR / "gone.mp4"), str(audio))

    r = await client.get(f"/api/downloaded/{vid}/stream")
    assert r.status_code in (200, 206), r.text
    assert r.content.startswith(b"ID3")


@pytest.mark.asyncio
async def test_the_video_still_wins_when_both_exist(client):
    settings.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    video = settings.VIDEOS_DIR / f"{uuid.uuid4().hex}.mp4"
    video.write_bytes(b"VIDEO" + b"\x00" * 4096)
    audio = settings.AUDIO_DIR / f"{uuid.uuid4().hex}.mp3"
    audio.write_bytes(b"ID3" + b"\x00" * 4096)
    vid = await _row(str(video), str(audio))

    r = await client.get(f"/api/downloaded/{vid}/stream")
    assert r.content.startswith(b"VIDEO")


@pytest.mark.asyncio
async def test_neither_file_is_still_a_404(client):
    vid = await _row(str(settings.VIDEOS_DIR / "no.mp4"), str(settings.AUDIO_DIR / "no.mp3"))
    r = await client.get(f"/api/downloaded/{vid}/stream")
    assert r.status_code == 404
