# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A cancelled batch transcription must not hold Whisper's gate until its decode ends: the next long transcription was queueing behind a job nobody wanted."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.services import whisper_service as ws


class SlowModel:
    """Yields a segment every 20 ms. Unstopped, that's 20 seconds of holding the gate."""
    def __init__(self):
        self.consumed = 0

    def transcribe(self, *a, **k):
        def gen():
            for i in range(1000):
                time.sleep(0.02)
                self.consumed += 1
                yield SimpleNamespace(start=float(i), end=float(i + 1), text="x", words=[], temperature=0.0)
        return gen(), SimpleNamespace(language="en", language_probability=0.9)


def _patched(model, duration=None):
    return (patch.object(ws.WhisperService, "ensure_model", AsyncMock(return_value=model)),
            patch("backend.services.ffmpeg_service.has_audio_stream", AsyncMock(return_value=True)),
            patch("backend.services.video_utils.probe_duration", return_value=duration or ws._SERIALIZE_MIN_SECONDS + 60))


def test_a_cancel_frees_the_gate_within_a_segment_or_two():
    async def go():
        model = SlowModel()
        a, b, c = _patched(model)
        with a, b, c:
            task = asyncio.create_task(ws.whisper_service.transcribe("/tmp/song.wav"))
            for _ in range(100):
                await asyncio.sleep(0.02)
                if model.consumed >= 2:
                    break
            assert ws.WhisperService._transcribe_gate.locked()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            started = time.monotonic()
            while ws.WhisperService._transcribe_gate.locked() and time.monotonic() - started < 3:
                await asyncio.sleep(0.01)
            assert not ws.WhisperService._transcribe_gate.locked()
            assert model.consumed < 100
    asyncio.run(go())


def test_a_wait_for_timeout_frees_it_too():
    async def go():
        model = SlowModel()
        a, b, c = _patched(model)
        with a, b, c:
            try:
                await asyncio.wait_for(ws.whisper_service.transcribe("/tmp/song.wav"), timeout=0.1)
            except asyncio.TimeoutError:
                pass
            started = time.monotonic()
            while ws.WhisperService._transcribe_gate.locked() and time.monotonic() - started < 3:
                await asyncio.sleep(0.01)
            assert not ws.WhisperService._transcribe_gate.locked()
    asyncio.run(go())


def test_nobody_cancels_nothing_changes():
    class Short(SlowModel):
        def transcribe(self, *a, **k):
            def gen():
                for i in range(3):
                    yield SimpleNamespace(start=float(i), end=float(i + 1), text=f"w{i}", words=[], temperature=0.0)
            return gen(), SimpleNamespace(language="en", language_probability=0.9)

    async def go():
        a, b, c = _patched(Short())
        with a, b, c:
            out = await ws.whisper_service.transcribe("/tmp/song.wav")
        assert out["text"] == "w0 w1 w2"
        assert not ws.WhisperService._transcribe_gate.locked()
    asyncio.run(go())
