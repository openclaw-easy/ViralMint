# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Blocking file work in two doors runs off the event loop.

`shutil.rmtree(scratch)` at the end of every Studio render (hundreds of MB of
scene files) and the Library import's upload write (up to 2 GB, 1 MB at a
time) ran on the loop: while they did, no other request, socket or job moved.
"""
from __future__ import annotations

import asyncio
import builtins
import io
import threading
from unittest.mock import AsyncMock

import pytest


def test_a_render_removes_its_scratch_folder_off_the_event_loop(monkeypatch):
    import backend.agents.job_helper as jh
    from backend.config import settings
    from backend.core.exceptions import StudioNotFoundError
    from backend.database import init_db
    from backend.services import studio_render

    asyncio.run(init_db())
    monkeypatch.setattr(jh, "update_job_status", AsyncMock())
    seen = {}
    real = studio_render.shutil.rmtree

    def rmtree(path, *a, **kw):
        seen["thread"], seen["path"] = threading.get_ident(), path
        return real(path, *a, **kw)
    monkeypatch.setattr(studio_render.shutil, "rmtree", rmtree)

    async def go():
        seen["loop"] = threading.get_ident()
        await studio_render.render("job-rmtree-0001", "no-such-project")

    with pytest.raises(StudioNotFoundError):
        asyncio.run(go())
    scratch = settings.TMP_DIR / "studio_render_job-rmtr"
    assert seen["path"] == scratch and not scratch.exists()        # same folder, still removed
    assert seen["thread"] != seen["loop"]


def test_the_library_import_writes_the_upload_off_the_event_loop(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api import downloaded

    payload = bytes(range(256)) * (3 * 4096 + 7)                   # ~3 MB: several 1 MB chunks, a ragged tail
    seen = {"writes": [], "opened": [], "paths": []}
    real_open = builtins.open

    class Recorded:
        def __init__(self, f):
            self._f = f

        def write(self, data):
            seen["writes"].append(threading.get_ident())
            return self._f.write(data)

        def __getattr__(self, name):
            return getattr(self._f, name)

    def recording_open(file, mode="r", *a, **kw):
        f = real_open(file, mode, *a, **kw)
        if str(file).endswith(".mp4") and "w" in mode and "videos" in str(file):
            seen["opened"].append(threading.get_ident())
            seen["paths"].append(file)
            return Recorded(f)
        return f
    monkeypatch.setattr(builtins, "open", recording_open)
    monkeypatch.setattr(io, "open", recording_open)

    class Stop(Exception):
        pass

    def no_db():                                                   # the write is all this test is about
        seen["loop"] = threading.get_ident()
        raise Stop
    monkeypatch.setattr(downloaded, "AsyncSessionLocal", no_db)

    app = FastAPI()
    app.include_router(downloaded.router, prefix="/api")
    r = TestClient(app, raise_server_exceptions=False).post(
        "/api/downloaded/import", files={"file": ("clip.mp4", payload, "video/mp4")})
    assert r.status_code == 500                                    # stopped at the DB, after the write
    (path,) = seen["paths"]
    try:
        assert path.read_bytes() == payload                        # the upload arrives whole
    finally:
        path.unlink(missing_ok=True)
    assert len(seen["opened"]) == 1 and len(seen["writes"]) >= 3
    assert seen["loop"] not in seen["opened"] and seen["loop"] not in seen["writes"]
