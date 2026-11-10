# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Edge TTS: Microsoft's endpoint rejects a share of handshakes with 403.

A Studio draft voices every scene with Edge, several at a time. Refusals run
~0.39 per handshake, independently (measured against the live service on the
hosted variant), so without retries a draft lost a take or two almost every
run while the job still said "Voices ready". A cap of 2 concurrent
connections + up to 8 jittered retries of a transient failure fixes it; a
permanent error (bad voice) still fails at once.
"""
import asyncio

import aiohttp
import pytest

import backend.services.edge_tts_service as ets
from backend.core.exceptions import VoiceGenerationError


def _handshake(status):
    # str() of the real exception reads request_info.real_url — give it one.
    from types import SimpleNamespace
    info = SimpleNamespace(real_url="wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1")
    return aiohttp.WSServerHandshakeError(request_info=info, history=(), status=status,
                                          message="Invalid response status")


class _FakeCommunicate:
    """Fails with the queued errors, then writes audio."""
    plan: list = []
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def stream(self):
        type(self).calls += 1
        if type(self).plan:
            raise type(self).plan.pop(0)
        yield {"type": "audio", "data": b"ID3fakeaudio"}


@pytest.fixture
def fake_edge(monkeypatch):
    import edge_tts
    _FakeCommunicate.plan, _FakeCommunicate.calls = [], 0
    monkeypatch.setattr(edge_tts, "Communicate", _FakeCommunicate)
    monkeypatch.setattr(ets, "_RETRY_DELAYS_S", (0,) * 7)
    return _FakeCommunicate


def test_403_is_retried_then_succeeds(fake_edge, tmp_path):
    fake_edge.plan = [_handshake(403), _handshake(403)]
    out = asyncio.run(ets.generate_voice("Hello there.", output_path=tmp_path / "a.mp3"))
    assert out.stat().st_size > 0
    assert fake_edge.calls == 3


@pytest.mark.parametrize("err", [_handshake(429), _handshake(503), aiohttp.ClientConnectionError("reset"),
                                 asyncio.TimeoutError()])
def test_other_transient_failures_are_retried(fake_edge, tmp_path, err):
    fake_edge.plan = [err]
    asyncio.run(ets.generate_voice("Hello there.", output_path=tmp_path / "b.mp3"))
    assert fake_edge.calls == 2


def test_permanent_failure_is_not_retried(fake_edge, tmp_path):
    fake_edge.plan = [ValueError("Invalid voice 'xx-XX-Nope'")]
    with pytest.raises(VoiceGenerationError, match="Invalid voice"):
        asyncio.run(ets.generate_voice("Hello there.", voice_id="xx-XX-Nope", output_path=tmp_path / "c.mp3"))
    assert fake_edge.calls == 1


def test_gives_up_after_the_last_attempt_with_the_real_error(fake_edge, tmp_path):
    fake_edge.plan = [_handshake(403)] * 20
    with pytest.raises(VoiceGenerationError, match="403"):
        asyncio.run(ets.generate_voice("Hello there.", output_path=tmp_path / "d.mp3"))
    assert fake_edge.calls == ets._MAX_ATTEMPTS == 8


def test_concurrency_is_capped(fake_edge, tmp_path, monkeypatch):
    live, peak = 0, 0

    class _Slow(_FakeCommunicate):
        async def stream(self):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1
            yield {"type": "audio", "data": b"ID3"}

    import edge_tts
    monkeypatch.setattr(edge_tts, "Communicate", _Slow)

    async def burst():
        await asyncio.gather(*[ets.generate_voice("x.", output_path=tmp_path / f"e{i}.mp3") for i in range(8)])

    asyncio.run(burst())
    assert peak <= ets._MAX_CONCURRENT == 2


def test_cap_survives_a_second_event_loop(fake_edge, tmp_path, monkeypatch):
    """Each asyncio.run is a new loop; a module-level Semaphore contended in
    one loop raises "bound to a different event loop" in the next."""
    class _Slow(_FakeCommunicate):
        async def stream(self):
            await asyncio.sleep(0.01)
            yield {"type": "audio", "data": b"ID3"}

    import edge_tts
    monkeypatch.setattr(edge_tts, "Communicate", _Slow)

    async def burst(tag):
        await asyncio.gather(*[ets.generate_voice("x.", output_path=tmp_path / f"{tag}{i}.mp3") for i in range(5)])

    asyncio.run(burst("a"))
    asyncio.run(burst("b"))   # contended again, on a different loop
