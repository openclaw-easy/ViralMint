# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A long free-voice script used to sit at a flat progress for minutes, then answer "No audio was
received", then read the whole script again. Now: a bound on movement, real progress, only the
failed part retried, and a ceiling. No network: stubs and one silent local websocket."""
import asyncio
import time

import pytest

from backend.core.exceptions import JobCancelledError, VoiceGenerationError
from backend.services import edge_tts_service as ets

LONG = " ".join(f"This is sentence number {i} of a very long voiceover script." for i in range(150))   # ~9,000 characters


class _Stream:
    """Each instance is one attempt at one text; `plan[text]` queues what its attempts do."""
    attempts: list[str] = []
    plan: dict = {}

    def __init__(self, text, *a, **k):
        self.text = text

    async def stream(self):
        type(self).attempts.append(self.text)
        for step in (type(self).plan.get(self.text) or []):
            type(self).plan[self.text] = type(self).plan[self.text][1:]
            if step == "stall":
                await asyncio.sleep(3600)
            raise step
        yield {"type": "WordBoundary", "offset": 0, "duration": 1, "text": "x"}
        yield {"type": "audio", "data": self.text[:8].encode() + b"|"}


@pytest.fixture
def fake(monkeypatch):
    import edge_tts
    _Stream.attempts, _Stream.plan = [], {}
    monkeypatch.setattr(edge_tts, "Communicate", _Stream)
    monkeypatch.setattr(ets, "_RETRY_DELAYS_S", (0,) * 7)
    monkeypatch.setattr(ets, "_STALL_S", 0.05)
    monkeypatch.setattr(ets, "_WATCH_S", 0.01)
    return _Stream


def test_a_long_text_goes_out_in_sentence_parts_and_loses_nothing():
    parts = ets.split_into_parts(LONG)
    assert len(parts) >= 5
    assert all(len(p) <= ets._MAX_PART_CHARS for p in parts)
    assert all(p.rstrip().endswith(".") for p in parts)
    assert " ".join(parts).split() == LONG.split()


def test_short_text_is_one_part_untouched():
    assert ets.split_into_parts("  Hello there.  ") == ["  Hello there.  "]


def test_chinese_stays_under_the_byte_limit_and_loses_nothing():
    text = "这是一个很长的句子，用来测试分段。" * 300
    parts = ets.split_into_parts(text)
    assert all(len(p.encode()) <= ets._MAX_PART_BYTES for p in parts)
    assert "".join(parts) == text


def test_one_endless_sentence_still_splits():
    text = "word " * 2000
    parts = ets.split_into_parts(text)
    assert all(len(p) <= ets._MAX_PART_CHARS for p in parts)
    assert "".join(parts).split() == text.split()
    assert all(len(p) <= ets._MAX_PART_CHARS for p in ets.split_into_parts("x" * 9000))


def test_a_part_that_stalls_is_retried_alone_and_the_good_parts_are_kept(fake, tmp_path):
    parts = ets.split_into_parts(LONG)
    fake.plan[parts[1]] = ["stall"]
    out = asyncio.run(ets.generate_voice(LONG, output_path=tmp_path / "a.mp3", use_ssml=False))
    assert fake.attempts.count(parts[0]) == 1          # never read twice
    assert fake.attempts.count(parts[1]) == 2          # only this one again
    assert all(fake.attempts.count(p) == 1 for p in parts[2:])
    assert out.read_bytes().count(b"|") == len(parts)
    assert list(tmp_path.iterdir()) == [out]           # no scratch left behind


def test_no_audio_on_a_part_retries_that_part_only(fake, tmp_path):
    class NoAudioReceived(Exception):
        pass
    parts = ets.split_into_parts(LONG)
    fake.plan[parts[2]] = [NoAudioReceived("No audio was received.")]
    asyncio.run(ets.generate_voice(LONG, output_path=tmp_path / "b.mp3", use_ssml=False))
    assert fake.attempts.count(parts[0]) == 1 and fake.attempts.count(parts[2]) == 2


def test_a_part_that_keeps_stalling_fails_fast_with_a_plain_reason(fake, tmp_path):
    parts = ets.split_into_parts(LONG)
    fake.plan[parts[1]] = ["stall"] * 8
    started = time.monotonic()
    with pytest.raises(VoiceGenerationError, match=rf"part 2 of {len(parts)}"):
        asyncio.run(ets.generate_voice(LONG, output_path=tmp_path / "c.mp3", use_ssml=False))
    assert time.monotonic() - started < 5
    assert fake.attempts.count(parts[1]) == ets._MAX_STALLS
    assert not (tmp_path / "c.mp3").exists() and list(tmp_path.iterdir()) == []


def test_the_whole_text_has_a_ceiling_even_while_audio_trickles(monkeypatch, tmp_path):
    import edge_tts

    class Trickle(_Stream):
        async def stream(self):
            while True:
                await asyncio.sleep(0.01)
                yield {"type": "audio", "data": b"x"}
    monkeypatch.setattr(edge_tts, "Communicate", Trickle)
    monkeypatch.setattr(ets, "_WATCH_S", 0.01)
    monkeypatch.setattr(ets, "_TOTAL_BASE_S", 0.1)
    monkeypatch.setattr(ets, "_TOTAL_PER_CHAR_S", 0.0)
    with pytest.raises(VoiceGenerationError, match="too slow"):
        asyncio.run(asyncio.wait_for(ets.generate_voice(LONG, output_path=tmp_path / "d.mp3", use_ssml=False), 5))


def test_waiting_for_a_slot_is_not_on_the_ceiling(monkeypatch, tmp_path):
    """Lines queue behind the 2 connections: a short line must not time out in the queue."""
    import edge_tts

    class Slow(_Stream):
        async def stream(self):
            await asyncio.sleep(0.15)
            yield {"type": "audio", "data": b"x"}
    monkeypatch.setattr(edge_tts, "Communicate", Slow)
    monkeypatch.setattr(ets, "_WATCH_S", 0.01)
    monkeypatch.setattr(ets, "_TOTAL_BASE_S", 0.3)
    monkeypatch.setattr(ets, "_TOTAL_PER_CHAR_S", 0.0)

    async def burst():
        await asyncio.gather(*[ets.generate_voice("x.", output_path=tmp_path / f"q{i}.mp3") for i in range(10)])
    asyncio.run(burst())     # 10 lines × 0.15 s on 2 slots is 0.75 s, past the 0.3 s each line may take


def test_progress_moves_and_never_goes_backwards_past_a_retry(fake, tmp_path):
    parts = ets.split_into_parts(LONG)
    fake.plan[parts[1]] = ["stall"]
    seen = []
    asyncio.run(ets.generate_voice(LONG, output_path=tmp_path / "e.mp3", use_ssml=False,
                                   on_progress=lambda f, part, total: seen.append((f, part, total))))
    assert len({part for _, part, _ in seen}) == len(parts)
    assert seen[-1][2] == len(parts) and seen[-1][0] <= 1.0
    fractions = [f for f, _, _ in seen]
    assert fractions == sorted(fractions) and fractions[0] < 0.25


def test_a_cancel_stops_a_silent_part_at_once(fake, tmp_path):
    parts = ets.split_into_parts(LONG)
    fake.plan[parts[0]] = ["stall"]
    monkey_stall = ets._STALL_S
    ets._STALL_S = 3600
    try:
        async def stop():
            return True
        started = time.monotonic()
        with pytest.raises(JobCancelledError):
            asyncio.run(ets.generate_voice(LONG, output_path=tmp_path / "f.mp3", use_ssml=False, should_stop=stop))
        assert time.monotonic() - started < 2
    finally:
        ets._STALL_S = monkey_stall
    assert list(tmp_path.iterdir()) == []


def test_a_silent_websocket_is_what_the_library_alone_never_bounds(monkeypatch, tmp_path):
    """The real edge-tts against a local server that accepts and says nothing: its receive_timeout
    does not reach the socket (the customer's hang), and our watch does."""
    import edge_tts
    import edge_tts.communicate as C
    from aiohttp import web

    async def scenario():
        async def ws(request):
            sock = web.WebSocketResponse()
            await sock.prepare(request)
            await asyncio.sleep(60)
            return sock
        app = web.Application()
        app.router.add_get("/", ws)
        runner = web.AppRunner(app, shutdown_timeout=0.1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(C, "WSS_URL", f"ws://127.0.0.1:{port}/?x=1")

        async def library_alone():
            communicate = edge_tts.Communicate("Hello.", ets.DEFAULT_VOICE, receive_timeout=1, connect_timeout=1)
            async for _ in communicate.stream():
                pass
        with pytest.raises(asyncio.TimeoutError):      # our wait_for, not its receive_timeout=1
            await asyncio.wait_for(library_alone(), 2.5)

        monkeypatch.setattr(ets, "_STALL_S", 0.2)
        monkeypatch.setattr(ets, "_WATCH_S", 0.02)
        monkeypatch.setattr(ets, "_RETRY_DELAYS_S", (0,) * 7)
        started = time.monotonic()
        with pytest.raises(VoiceGenerationError, match="stopped sending audio"):
            await ets.generate_voice("Hello.", output_path=tmp_path / "g.mp3", use_ssml=False)
        assert time.monotonic() - started < 3
        await runner.cleanup()
    asyncio.run(scenario())


def test_the_voiceover_runner_reports_movement_and_a_cancel_reaches_the_synth(monkeypatch, tmp_path):
    from backend.api import tools as tools_api
    from backend.core import tool_runners
    from backend.services import tts_service

    seen, marks = {}, []

    async def fake_generate(text, **kw):
        seen.update(kw)
        kw["on_progress"](0.5, 2, 4)
        await asyncio.sleep(0)
        kw["output_path"].write_bytes(b"x")
        return kw["output_path"]

    async def progress(job_id, pct, step, user_id="local"):
        marks.append((pct, step))

    async def success(*a, **k):
        return True

    async def not_cancelled(job_id):
        return False
    monkeypatch.setattr(tts_service, "generate_tts", fake_generate)
    monkeypatch.setattr(tool_runners, "_tool_progress", progress)
    monkeypatch.setattr(tool_runners, "_tool_success", success)
    monkeypatch.setattr(tools_api, "tool_out_path", lambda job, ext: tmp_path / f"{job}{ext}")
    monkeypatch.setattr("backend.agents.job_helper.job_cancelled", not_cancelled)
    asyncio.run(tool_runners.run_tool_voiceover("j1", "Hello.", "", provider="edge_tts"))
    assert callable(seen["should_stop"])
    assert any("part 2 of 4" in step and 30 < pct < 95 for pct, step in marks), marks


def test_natural_pauses_touch_whole_words_only():
    # "know" once became "k— now" (spoken "okay, now") and "debut" "de— but".
    from backend.services.edge_tts_service import _add_natural_pauses as f
    assert f("I know the answer now.") == "I know the answer now."
    assert f("Their debut album. Snow falls now.") == "Their debut album. Snow falls now."
    assert f("But wait. However, go. but now") == "— But wait. However, go. — but now"
    assert f("I know the answer now.") == "I know the answer now."    # "now." is not a transition
    assert f("Here's the thing: yes") == "— Here's the thing: yes"
    assert f("— But x") == "— But x"
