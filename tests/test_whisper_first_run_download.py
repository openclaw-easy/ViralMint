# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The first Whisper run downloads 150 MB - 3 GB. Say so, and don't freeze.

The installer ships faster-whisper's CODE but no WEIGHTS, so `WhisperModel(name)`
fetches them from HuggingFace the first time a quality tier is used. Two things
were wrong with that:

  * SILENT — exactly one call site said anything, so on a fresh install every
    tool job, the clip extractor and the analyzer sat on a frozen step for
    minutes;
  * BLOCKING — `transcribe()` called the synchronous `load()` straight on the
    event loop, so a cold cache froze the WHOLE backend (WebSocket, progress,
    every other request) for the length of the download.

`ensure_model()` is the single door. The last test is the drift guard: a call
site added later must not reintroduce either failure.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.exceptions import WhisperModelUnavailableError
from backend.services import whisper_service as ws
from backend.services.whisper_service import WhisperService


@pytest.fixture(autouse=True)
def _clean_state():
    ws._download_state.clear()
    ws._ensure_locks.clear()
    yield
    ws._download_state.clear()
    ws._ensure_locks.clear()


class TestTheDownloadIsAnnounced:
    async def test_a_cold_cache_fires_the_notice_before_the_wait(self):
        order: list[str] = []

        async def _notice(model_name, size):
            order.append(f"notice:{model_name}:{size}")

        def _slow_load(quality):
            order.append("load")
            return MagicMock()

        with patch.object(WhisperService, "is_model_cached", return_value=False), \
             patch.object(WhisperService, "load", staticmethod(_slow_load)):
            await WhisperService.ensure_model("balanced", on_download=_notice)

        assert order[0].startswith("notice:"), order
        assert order[-1] == "load"
        assert "small" in order[0]        # the balanced tier's model

    async def test_a_warm_cache_says_nothing(self):
        notice = AsyncMock()
        with patch.object(WhisperService, "is_model_cached", return_value=True), \
             patch.object(WhisperService, "load", staticmethod(lambda q: MagicMock())):
            await WhisperService.ensure_model("balanced", on_download=notice)
        notice.assert_not_awaited()

    async def test_the_default_notice_is_a_toast_not_silence(self):
        """Deliberately the default, so a call site added later degrades to
        "says something" rather than "says nothing"."""
        with patch.object(WhisperService, "is_model_cached", return_value=False), \
             patch.object(WhisperService, "load", staticmethod(lambda q: MagicMock())), \
             patch("backend.core.ws_manager.ws_manager.send_constraint_warning",
                   new=AsyncMock()) as warn:
            await WhisperService.ensure_model("balanced")
        warn.assert_awaited()
        assert warn.await_args.kwargs["constraint"] == "whisper_model_download"

    async def test_a_notice_that_raises_never_breaks_the_work(self):
        async def _broken(model_name, size):
            raise RuntimeError("no socket")

        with patch.object(WhisperService, "is_model_cached", return_value=False), \
             patch.object(WhisperService, "load", staticmethod(lambda q: MagicMock())):
            got = await WhisperService.ensure_model("balanced", on_download=_broken)
        assert got is not None

    async def test_a_sync_callback_is_accepted(self):
        seen: list = []
        with patch.object(WhisperService, "is_model_cached", return_value=False), \
             patch.object(WhisperService, "load", staticmethod(lambda q: MagicMock())):
            await WhisperService.ensure_model(
                "balanced", on_download=lambda m, s: seen.append(m))
        assert seen == ["small"]

    async def test_the_job_notice_writes_the_row_and_the_socket(self):
        """A first-run download is long enough that the user will reload the page
        part-way through, so the DB row has to carry the step too."""
        with patch("backend.agents.job_helper.update_job_status", new=AsyncMock()) as upd, \
             patch("backend.core.ws_manager.ws_manager.send_progress", new=AsyncMock()) as prog:
            await ws.job_download_notice("job1", "local", 5.0)("small", "~460 MB")
        upd.assert_awaited_once()
        prog.assert_awaited_once()
        assert "Downloading the speech-recognition model" in upd.await_args.kwargs["current_step"]


class TestTheLoopKeepsRunning:
    async def test_the_load_happens_off_the_event_loop(self):
        """`load()` is synchronous and, uncached, blocks for the whole fetch. If
        it runs on the loop, nothing else in the backend is served."""
        beats = 0
        stop = False

        async def _heartbeat():
            nonlocal beats
            while not stop:
                beats += 1
                await asyncio.sleep(0.01)

        def _blocking_load(quality):
            import time
            time.sleep(0.3)          # stands in for the HuggingFace download
            return MagicMock()

        hb = asyncio.create_task(_heartbeat())
        try:
            with patch.object(WhisperService, "is_model_cached", return_value=False), \
                 patch.object(WhisperService, "load", staticmethod(_blocking_load)), \
                 patch("backend.core.ws_manager.ws_manager.send_constraint_warning",
                       new=AsyncMock()):
                await WhisperService.ensure_model("balanced")
        finally:
            stop = True
            await hb
        assert beats > 5, f"the loop was blocked during the load ({beats} beats)"


class TestFailureIsTypedAndReadable:
    async def test_a_failed_fetch_raises_the_typed_error(self):
        def _boom(quality):
            raise OSError("We couldn't connect to huggingface.co")

        with patch.object(WhisperService, "is_model_cached", return_value=False), \
             patch.object(WhisperService, "load", staticmethod(_boom)), \
             patch("backend.core.ws_manager.ws_manager.send_constraint_warning",
                   new=AsyncMock()):
            with pytest.raises(WhisperModelUnavailableError) as e:
                await WhisperService.ensure_model("balanced")
        assert "speech-recognition model" in str(e.value)

    async def test_the_failure_is_recorded_for_the_poller(self):
        """Watching only `cached` told the user a FAILED download was still in
        progress."""
        with patch.object(WhisperService, "is_model_cached", return_value=False), \
             patch.object(WhisperService, "load",
                          staticmethod(lambda q: (_ for _ in ()).throw(OSError("nope")))), \
             patch("backend.core.ws_manager.ws_manager.send_constraint_warning",
                   new=AsyncMock()):
            with pytest.raises(WhisperModelUnavailableError):
                await WhisperService.ensure_model("balanced")
        st = ws.download_state("balanced")
        assert st["downloading"] is False
        assert "nope" in st["error"]

    async def test_a_cancel_clears_the_downloading_flag(self):
        """CancelledError is not an Exception, so it skips the error handler —
        without the `finally` the tier reported "downloading" forever."""
        def _cancelled(quality):
            raise asyncio.CancelledError()

        with patch.object(WhisperService, "is_model_cached", return_value=False), \
             patch.object(WhisperService, "load", staticmethod(_cancelled)), \
             patch("backend.core.ws_manager.ws_manager.send_constraint_warning",
                   new=AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await WhisperService.ensure_model("balanced")
        st = ws.download_state("balanced")
        assert st["downloading"] is False
        assert st["error"] is None      # a cancel is not a failure

    def test_download_state_never_raises_on_an_unknown_tier(self):
        assert ws.download_state("nonexistent") == {"downloading": False, "error": None}


class TestNoCallSiteCanGoSilentAgain:
    """The drift guard. Both halves of the original bug are reintroducible by a
    single line, which is why they are asserted against the source."""

    JOB_MODULES = (
        "backend/core/tool_runners.py",
        "backend/agents/analyzer.py",
        "backend/services/clip_extractor.py",
    )

    def test_every_job_transcribe_names_its_progress_bar(self):
        offenders = []
        for rel in self.JOB_MODULES:
            src = Path(rel).read_text()
            for m in re.finditer(r"whisper_service\.transcribe\((?:[^()]|\([^()]*\))*\)", src):
                if "on_download" not in m.group(0):
                    line = src[:m.start()].count("\n") + 1
                    offenders.append(f"{rel}:{line}")
        assert not offenders, (
            "these own a Job row and must pass on_download=job_download_notice(...), "
            f"or their first run sits on a frozen step for minutes: {offenders}"
        )

    def test_nobody_calls_load_directly(self):
        """`load()` on the loop is the freeze. `ensure_model()` is the door."""
        offenders = []
        for rel in Path("backend").rglob("*.py"):
            if rel.name == "whisper_service.py":
                continue
            src = rel.read_text()
            if re.search(r"whisper_service\.load\(|whisper_service,\s*\"load\"", src):
                offenders.append(str(rel))
        assert not offenders, f"call ensure_model() instead of load(): {offenders}"
