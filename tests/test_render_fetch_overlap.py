# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Render v2 fetches overlap the encodes (`fetch` used to run serially ahead of,
and between, the scene encodes).

  * a bound stock clip downloads under the VOICES step, not after it;
  * the second shot downloads before the encode slot, never inside it;
  * one download per clip (provider + id), however many scenes bind it;
  * the timing line keeps its old fields and gains download / fetch_b /
    fetch_exposed, the last one the wall seconds fetch really cost.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from tests.test_studio_render import (  # noqa: F401 — fixtures
    SCRIPT, client, env, generated_for, media, run, start_render,
)


# ── the clock ────────────────────────────────────────────────────────────────

def test_fetch_exposed_counts_only_waits_with_no_encode_running(monkeypatch):
    from backend.services import studio_render
    now = {"t": 0.0}
    monkeypatch.setattr(studio_render.time, "monotonic", lambda: now["t"])
    clock = studio_render._Clock()
    clock.waiting(+1)                 # before watch(): hidden behind the voices step, not exposed
    now["t"] = 10.0
    clock.watch()                     # visuals step begins: a scene waits, nothing encodes → exposed
    now["t"] = 13.0
    clock.encoding(+1)                # an encode starts: the wait is covered now
    now["t"] = 14.0
    clock.waiting(-1)
    now["t"] = 15.0
    clock.encoding(-1)                # nothing waits, nothing encodes: not exposed
    now["t"] = 16.0
    clock.waiting(+1)                 # a late AI purchase, encoders idle
    now["t"] = 18.0
    clock.waiting(-1)
    assert clock.sums["fetch_exposed"] == pytest.approx(5.0)
    assert "fetch_exposed=5.0" in clock.line()


def test_fetch_exposed_is_on_the_line_even_at_zero():
    from backend.services import studio_render
    clock = studio_render._Clock()
    clock.watch()
    assert "fetch_exposed=0.0" in clock.line()


# ── the fetcher ──────────────────────────────────────────────────────────────

def test_one_download_per_clip_whatever_the_scene(monkeypatch, tmp_path):
    from backend.services import studio_media, studio_render
    calls: list[str] = []

    async def download(url, dest, max_bytes=None):
        calls.append(url)
        if "dead" in url:
            raise RuntimeError("410")
        Path(dest).write_bytes(b"clip")

    monkeypatch.setattr(studio_media, "download", download)

    async def go():
        fetcher = studio_render._StockFetcher(asyncio.Semaphore(2), studio_render._Clock())
        key = studio_render._StockFetcher.key({"provider": "pexels", "provider_id": "7"}, "u")
        assert key == "pexels:7"
        assert studio_render._StockFetcher.key({}, "https://x/y.mp4") == "url:https://x/y.mp4"
        a, b = await asyncio.gather(fetcher.fetch(key, "https://a/7.mp4", tmp_path / "s00.mp4"),
                                    fetcher.fetch(key, "https://a/7.mp4", tmp_path / "s01.mp4"))
        assert a == b == tmp_path / "s00.mp4" and calls == ["https://a/7.mp4"]
        other = await fetcher.fetch("pexels:8", "https://a/8.mp4", tmp_path / "s02.mp4")
        assert other == tmp_path / "s02.mp4" and len(calls) == 2
        # A failed clip fails every scene that asks for it (each falls back to a search).
        with pytest.raises(RuntimeError):
            await fetcher.fetch("pexels:9", "https://a/dead.mp4", tmp_path / "s03.mp4")
        with pytest.raises(RuntimeError):
            await fetcher.fetch("pexels:9", "https://a/dead.mp4", tmp_path / "s04.mp4")
        # A prefetched clip nobody awaited: cancel() takes its error, cancels the rest.
        fetcher.start("pexels:10", "https://a/dead.mp4", tmp_path / "s05.mp4")
        slow = fetcher.start("pexels:11", "https://a/11.mp4", tmp_path / "s06.mp4")
        await asyncio.sleep(0)
        fetcher.cancel()
        await asyncio.sleep(0)
        assert slow.done()
    asyncio.run(go())


# ── the render ───────────────────────────────────────────────────────────────

def _bind(client, scenes, same_clip=False, alternates=False):
    """Every scene bound to a Pexels clip (the same one for all when same_clip),
    with an unused alternate each when `alternates`."""
    ops = []
    for i, sc in enumerate(scenes):
        pid = "100" if same_clip else str(100 + i)
        alts = [{"provider": "pexels", "provider_id": str(500 + i), "license": "Pexels License", "creator": "C",
                 "download_url": f"https://videos.pexels.com/video-files/{500 + i}/hd.mp4"}] if alternates else []
        ops.append({"op": "plan_result", "uid": sc["uid"], "expect_line": sc["line"], "visual": {
            "kind": "stock", "state": "bound", "query": "coins jar", "fit": "loop", "pinned": False,
            "reason": None, "alternates": alts,
            "asset": {"provider": "pexels", "provider_id": pid, "download_url": f"https://videos.pexels.com/video-files/{pid}/hd.mp4",
                      "license": "Pexels License", "creator": "C"}}})
    return ops


def _project(client, *, same_clip=False, alternates=False):
    from backend.services import studio_store as store
    p = client.post("/api/studio/projects", json={"start": {"script": SCRIPT}}).json()["project"]
    ops = _bind(client, p["doc"]["scenes"], same_clip=same_clip, alternates=alternates)
    ops.append({"op": "set_setting", "path": "music", "value": {"enabled": False}})
    asyncio.run(store.write_ops(p["id"], ops, if_revision=None, internal=True))
    return client.get(f"/api/studio/projects/{p['id']}").json()["project"]


def test_two_scenes_on_one_clip_download_it_once(client, env):
    project = _project(client, same_clip=True)
    assert len(project["doc"]["scenes"]) == 2
    assert start_render(client, project).status_code == 200
    run(env)
    assert len(generated_for(project["id"])) == 1
    assert env["downloads"] == ["https://videos.pexels.com/video-files/100/hd.mp4"]
    assert not env["warnings"]


def test_clips_download_under_the_voices_step_and_never_inside_an_encode_slot(client, env, monkeypatch, caplog):
    from backend.services import studio_doc as sd
    from backend.services import studio_media, studio_render
    from backend.services import voice_take_service as vts

    monkeypatch.setattr(sd, "SECOND_SHOT_MIN_S", 0.5)        # the 1.2 s test takes earn a second shot
    monkeypatch.setattr(studio_render, "ENCODE_SLOTS", 1)      # one slot: held = 0 free
    events: list[tuple[str, int | None]] = []
    real_download, real_synth = studio_media.download, vts._synthesise

    async def download(url, dest, max_bytes=None):
        events.append(("download", studio_render.encode_slots()._value))
        return await real_download(url, dest, max_bytes)

    async def synth(text, spec, out):
        events.append(("synth", None))
        return await real_synth(text, spec, out)

    monkeypatch.setattr(studio_media, "download", download)
    monkeypatch.setattr(vts, "_synthesise", synth)
    project = _project(client, alternates=True)
    assert start_render(client, project).status_code == 200
    with caplog.at_level(logging.INFO, logger="backend.services.studio_render"):
        run(env)
    assert len(generated_for(project["id"])) == 1
    kinds = [k for k, _ in events]
    assert kinds.index("download") < kinds.index("synth"), events       # prefetched under the voices step
    assert kinds.count("download") == 4, events                          # 2 clips + 2 second shots
    assert all(free == 1 for k, free in events if k == "download"), events   # never inside the held slot
    urls = env["downloads"]
    assert {u for u in urls if "/500/" in u or "/501/" in u} == {
        "https://videos.pexels.com/video-files/500/hd.mp4", "https://videos.pexels.com/video-files/501/hd.mp4"}
    line = next(r.getMessage() for r in caplog.records if "studio render timing" in r.getMessage())
    for field in ("encode=", "encode_wait=", "fetch=", "fetch_b=", "download=", "fetch_exposed="):
        assert field in line, line
    assert "| scene sums:" in line and "visuals=" in line
