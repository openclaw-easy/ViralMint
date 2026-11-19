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

    voices: list = []

    def __init__(self, *a, **k):
        type(self).voices.append(a[1] if len(a) > 1 else k.get("voice"))

    async def stream(self):
        type(self).calls += 1
        if type(self).plan:
            raise type(self).plan.pop(0)
        yield {"type": "audio", "data": b"ID3fakeaudio"}


@pytest.fixture
def fake_edge(monkeypatch):
    import edge_tts
    _FakeCommunicate.plan, _FakeCommunicate.calls, _FakeCommunicate.voices = [], 0, []
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


# 2026-11-16: the two Andrew voices took turns answering "No audio was
# received" in bursts (the default, en-US-AndrewMultilingualNeural, 5 of 6;
# later en-US-AndrewNeural 6 of 8) while four other voices never failed, and
# the Studio harness lost a take after all 8 attempts on one voice. A refused
# handshake is the endpoint; "no audio" is the voice that was asked. After
# three in a row an English text goes to the same speaker's other voice.
def _no_audio():
    from edge_tts.exceptions import NoAudioReceived
    return NoAudioReceived("No audio was received. Please verify that your parameters are correct.")


ANDREW_ML, ANDREW = "en-US-AndrewMultilingualNeural", "en-US-AndrewNeural"


def test_a_voice_that_sends_no_audio_falls_back_to_its_plain_sibling(fake_edge, tmp_path):
    fake_edge.plan = [_no_audio(), _no_audio(), _no_audio()]
    out = asyncio.run(ets.generate_voice("Hello there.", voice_id=ANDREW_ML, output_path=tmp_path / "s.mp3"))
    assert out.stat().st_size > 0
    assert fake_edge.voices == [ANDREW_ML, ANDREW_ML, ANDREW_ML, ANDREW]


def test_the_sibling_is_kept_for_the_rest_of_the_script(fake_edge, tmp_path, monkeypatch):
    monkeypatch.setattr(ets, "split_into_parts", lambda text: ["Part one.", "Part two.", "Part three."])
    fake_edge.plan = [_no_audio(), _no_audio(), _no_audio()]
    asyncio.run(ets.generate_voice("Part one. Part two. Part three.", voice_id=ANDREW_ML, output_path=tmp_path / "k.mp3"))
    assert fake_edge.voices == [ANDREW_ML] * 3 + [ANDREW] * 3     # the next parts start on the voice that worked


def test_a_sibling_that_fails_as_often_hands_back(fake_edge, tmp_path):
    fake_edge.plan = [_no_audio()] * 6
    asyncio.run(ets.generate_voice("Hello there.", voice_id=ANDREW_ML, output_path=tmp_path / "b.mp3"))
    assert fake_edge.voices == [ANDREW_ML] * 3 + [ANDREW] * 3 + [ANDREW_ML]


def test_a_plain_voice_that_sends_no_audio_goes_to_its_multilingual_twin(fake_edge, tmp_path):
    # Measured the same day: en-US-AndrewNeural failed 6 of 8 while the multilingual one answered 24 of 24.
    fake_edge.plan = [_no_audio()] * 3
    asyncio.run(ets.generate_voice("Hello there.", voice_id=ANDREW, output_path=tmp_path / "p.mp3"))
    assert fake_edge.voices == [ANDREW] * 3 + [ANDREW_ML]


def test_two_no_audio_answers_stay_on_the_chosen_voice(fake_edge, tmp_path):
    fake_edge.plan = [_no_audio(), _no_audio()]
    asyncio.run(ets.generate_voice("Hello there.", voice_id=ANDREW_ML, output_path=tmp_path / "t.mp3"))
    assert fake_edge.voices == [ANDREW_ML] * 3


def test_refused_handshakes_never_change_the_voice(fake_edge, tmp_path):
    fake_edge.plan = [_handshake(403)] * 6
    asyncio.run(ets.generate_voice("Hello there.", voice_id=ANDREW_ML, output_path=tmp_path / "h.mp3"))
    assert set(fake_edge.voices) == {ANDREW_ML}


@pytest.mark.parametrize("text", ["今天我们聊三件最重要的事情。", "Hola, ¿cómo estás?", "Schöne Grüße aus Berlin."])
def test_a_text_that_is_not_plain_english_keeps_the_multilingual_voice(fake_edge, tmp_path, text):
    # The plain voice is English only: it would read this with the wrong accent, or not at all.
    fake_edge.plan = [_no_audio()] * 5
    asyncio.run(ets.generate_voice(text, voice_id=ANDREW_ML, output_path=tmp_path / "m.mp3"))
    assert set(fake_edge.voices) == {ANDREW_ML}


def test_a_voice_with_no_sibling_keeps_trying_itself(fake_edge, tmp_path):
    fake_edge.plan = [_no_audio()] * 5
    asyncio.run(ets.generate_voice("Hello there.", voice_id="en-US-AriaNeural", output_path=tmp_path / "n.mp3"))
    assert set(fake_edge.voices) == {"en-US-AriaNeural"}


def test_every_sibling_is_the_same_speaker_without_multilingual():
    for chosen, plain in ets._PLAIN_SIBLING.items():
        assert "Multilingual" in chosen and plain == chosen.replace("Multilingual", "")
