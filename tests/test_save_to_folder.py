"""Save to a folder the user picks (user report 2026-11-12, ported from the
hosted repo: "can't save clips directly to any folder in the pc"). A browser
download always lands in the browser's download folder; the Clipper's "Save N
clips" and every tool result's "Save to folder…" copy into the folder chosen
in the native picker (/api/videos/pick-folder)."""
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture(scope="module")
def client():
    from starlette.testclient import TestClient

    from backend.main import app
    with TestClient(app) as c:
        yield c


async def _seed_clip(path: Path, title: str) -> str:
    from backend.database import AsyncSessionLocal
    from backend.models.generated_video import GeneratedVideo
    async with AsyncSessionLocal() as db:
        v = GeneratedVideo(user_id="local", title=title, status="ready",
                           source_type="clip_extraction", video_path=str(path))
        db.add(v)
        await db.commit()
        return v.id


async def _seed_tool_job(path: Path, status: str = "success") -> str:
    from backend.agents.job_helper import create_job, update_job_status
    job = await create_job("tool:audio_enhance", "local", {})
    await update_job_status(job.id, status, output_data={"file": str(path)} if status == "success" else None)
    return job.id


@pytest.fixture
def clip_files():
    from backend.config import settings
    settings.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    made = []
    for name in ("one", "two"):
        p = settings.VIDEOS_DIR / f"{name}-{uuid4().hex[:8]}.mp4"
        p.write_bytes(name.encode())
        made.append(p)
    yield made
    for p in made:
        p.unlink(missing_ok=True)


@pytest.fixture
def tool_file():
    from backend.config import settings
    d = settings.STORAGE_ROOT / "tools"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{uuid4().hex}.mp3"
    p.write_bytes(b"enhanced")
    yield p
    p.unlink(missing_ok=True)


def test_save_clips_copies_the_checked_clips_into_the_folder(client, clip_files, tmp_path):
    ids = [asyncio.run(_seed_clip(p, f"Clip {p.stem[:3]}")) for p in clip_files]
    r = client.post("/api/videos/save-clips", json={"clip_ids": ids, "destination": str(tmp_path)})
    assert r.status_code == 200 and r.json()["count"] == 2
    assert sorted(f.read_bytes() for f in tmp_path.iterdir()) == [b"one", b"two"]
    # Saved again: nothing is overwritten.
    assert client.post("/api/videos/save-clips", json={"clip_ids": ids, "destination": str(tmp_path)}).json()["count"] == 2
    assert len(list(tmp_path.iterdir())) == 4


def test_save_clips_refuses_a_missing_folder_or_no_ids(client, tmp_path):
    assert client.post("/api/videos/save-clips", json={"clip_ids": ["x"], "destination": str(tmp_path / "nope")}).status_code == 400
    assert client.post("/api/videos/save-clips", json={"clip_ids": [], "destination": str(tmp_path)}).status_code == 400


def test_a_tool_result_is_saved_under_its_download_name_and_never_overwrites(client, tool_file, tmp_path):
    job = asyncio.run(_seed_tool_job(tool_file))
    a = client.post(f"/api/tools/save/{job}", json={"destination": str(tmp_path)}).json()
    b = client.post(f"/api/tools/save/{job}", json={"destination": str(tmp_path)}).json()
    assert Path(a["path"]).name == "viralmint_tool_audio_enhance.mp3"
    assert Path(b["path"]).name == "viralmint_tool_audio_enhance_1.mp3"
    assert Path(a["path"]).read_bytes() == b"enhanced"


def test_a_tool_save_refuses_what_it_should(client, tool_file, tmp_path):
    job = asyncio.run(_seed_tool_job(tool_file))
    assert client.post(f"/api/tools/save/{job}", json={}).status_code == 400
    assert client.post(f"/api/tools/save/{job}", json={"destination": str(tmp_path / "nope")}).status_code == 400
    assert client.post("/api/tools/save/no-such-job", json={"destination": str(tmp_path)}).status_code == 404
    running = asyncio.run(_seed_tool_job(tool_file, status="running"))
    assert client.post(f"/api/tools/save/{running}", json={"destination": str(tmp_path)}).status_code == 400


def test_a_tool_save_never_reads_outside_the_tools_folder(client, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    job = asyncio.run(_seed_tool_job(outside))
    dest = tmp_path / "dest"
    dest.mkdir()
    assert client.post(f"/api/tools/save/{job}", json={"destination": str(dest)}).status_code == 403
    assert list(dest.iterdir()) == []
