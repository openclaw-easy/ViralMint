# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for MessagingManager — channel registry, concurrent start-up, fan-out
notification, and the typed-event → chat-payload translation.

start_all runs channels CONCURRENTLY and one channel's failure is isolated so a
single bad token can't stop the others. All channel clients are mocked — no real
Telegram / Slack / Discord / WhatsApp session is opened.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.messaging.base import NotificationEvent, NotificationPayload
from backend.messaging.manager import MessagingManager, _build_payload


def _channel(name, *, configured=True, send_ok=True, start_error=None):
    ch = AsyncMock()
    ch.channel_name = name
    ch.is_configured = AsyncMock(return_value=configured)
    ch.send = AsyncMock(return_value=send_ok)
    if start_error is not None:
        ch.start = AsyncMock(side_effect=start_error)
    else:
        ch.start = AsyncMock()
    ch.stop = AsyncMock()
    return ch


# ── registry / callbacks ────────────────────────────────────────────────────

class TestRegistry:
    def test_register_and_get_channel(self):
        m = MessagingManager()
        ch = _channel("telegram")
        m.register(ch)
        assert m.get_channel("telegram") is ch
        assert m.get_channel("nope") is None

    def test_set_planner_callback_propagates(self):
        m = MessagingManager()
        ch = _channel("telegram")
        ch.set_planner_callback = MagicMock()   # real channels expose it as sync
        m.register(ch)

        async def cb(text, uid):
            return "ok"

        m.set_planner_callback(cb)
        assert m.get_planner_callback() is cb
        ch.set_planner_callback.assert_called_once_with(cb)

    def test_set_planner_callback_skips_channels_without_setter(self):
        m = MessagingManager()
        ch = _channel("telegram")
        # Remove the attribute so getattr(...) returns None → skipped, no crash.
        del ch.set_planner_callback

        async def cb(text, uid):
            return "ok"

        m.set_planner_callback(cb)   # must NOT raise
        assert m.get_planner_callback() is cb


# ── start_all — concurrent + failure-isolated ───────────────────────────────

class TestStartAll:
    async def test_starts_every_channel(self):
        m = MessagingManager()
        a, b = _channel("a"), _channel("b")
        m.register(a)
        m.register(b)
        await m.start_all()
        a.start.assert_awaited_once()
        b.start.assert_awaited_once()

    async def test_one_failure_does_not_block_the_rest(self):
        m = MessagingManager()
        bad = _channel("bad", start_error=RuntimeError("bad token"))
        good = _channel("good")
        m.register(bad)
        m.register(good)
        await m.start_all()          # must NOT raise
        good.start.assert_awaited_once()

    async def test_channels_start_concurrently(self):
        """Two slow channels overlap — gather, not a serial loop."""
        m = MessagingManager()
        order = []

        async def slow_start(tag):
            order.append(f"{tag}-enter")
            await asyncio.sleep(0.05)
            order.append(f"{tag}-exit")

        a, b = _channel("a"), _channel("b")
        a.start = lambda: slow_start("a")
        b.start = lambda: slow_start("b")
        m.register(a)
        m.register(b)
        await asyncio.wait_for(m.start_all(), timeout=0.5)
        # both entered before either exited → they ran in parallel
        assert order[:2] == ["a-enter", "b-enter"]

    async def test_start_all_no_channels(self):
        await MessagingManager().start_all()   # no-op, no raise


class TestStopAll:
    async def test_stops_all_and_isolates_errors(self):
        m = MessagingManager()
        bad = _channel("bad")
        bad.stop = AsyncMock(side_effect=RuntimeError("stop boom"))
        good = _channel("good")
        m.register(bad)
        m.register(good)
        await m.stop_all()           # must NOT raise
        good.stop.assert_awaited_once()


# ── notify — fan-out ────────────────────────────────────────────────────────

class TestNotify:
    async def test_sends_only_to_configured_channels(self):
        m = MessagingManager()
        on = _channel("on", configured=True)
        off = _channel("off", configured=False)
        m.register(on)
        m.register(off)
        await m.notify(NotificationEvent.VIDEO_GENERATED, title="T")
        on.send.assert_awaited_once()
        off.send.assert_not_called()

    async def test_send_receives_built_payload(self):
        m = MessagingManager()
        on = _channel("on", configured=True)
        m.register(on)
        await m.notify(NotificationEvent.VIDEO_GENERATED, title="My Short",
                       duration_seconds=12)
        args, _ = on.send.call_args
        user_id, payload = args
        assert user_id == "local"
        assert isinstance(payload, NotificationPayload)
        assert payload.event == NotificationEvent.VIDEO_GENERATED
        assert "My Short" in payload.body

    async def test_channel_exception_never_propagates(self):
        m = MessagingManager()
        boom = _channel("boom")
        boom.is_configured = AsyncMock(side_effect=RuntimeError("down"))
        m.register(boom)
        await m.notify(NotificationEvent.JOB_FAILED)   # swallowed

    async def test_send_failure_isolated_from_other_channels(self):
        m = MessagingManager()
        boom = _channel("boom", configured=True)
        boom.send = AsyncMock(side_effect=RuntimeError("send down"))
        good = _channel("good", configured=True)
        m.register(boom)
        m.register(good)
        await m.notify(NotificationEvent.SCOUT_COMPLETE, total=1)  # no raise
        good.send.assert_awaited_once()

    async def test_notify_no_channels_is_noop(self):
        await MessagingManager().notify(NotificationEvent.JOB_FAILED)


# ── _build_payload — every event branch ─────────────────────────────────────

class TestBuildPayload:
    def test_scout_complete_with_top_pick(self):
        p = _build_payload(NotificationEvent.SCOUT_COMPLETE, {
            "total": 12, "niche": "ai", "platforms": ["youtube", "tiktok"],
            "top_title": "How LLMs Work", "top_score": 87.5})
        assert isinstance(p, NotificationPayload)
        assert "12" in p.body and "ai" in p.body
        assert "youtube, tiktok" in p.body
        assert "How LLMs Work" in p.body and "87.5" in p.body

    def test_scout_complete_defaults(self):
        p = _build_payload(NotificationEvent.SCOUT_COMPLETE, {})
        assert "your niche" in p.body
        assert "the platforms" in p.body

    def test_download_single_titled(self):
        p = _build_payload(NotificationEvent.DOWNLOAD_COMPLETE,
                           {"downloaded": 1, "total": 1, "title": "Clip"})
        assert "Clip" in p.body

    def test_download_multi(self):
        p = _build_payload(NotificationEvent.DOWNLOAD_COMPLETE,
                           {"downloaded": 3, "total": 5})
        assert "*3* of 5" in p.body

    def test_download_zero(self):
        p = _build_payload(NotificationEvent.DOWNLOAD_COMPLETE, {})
        assert "Download finished" in p.body

    def test_video_generated_with_duration(self):
        p = _build_payload(NotificationEvent.VIDEO_GENERATED,
                           {"title": "My Short", "duration_seconds": 30})
        assert "My Short" in p.body and "(30s)" in p.body

    def test_video_generated_defaults(self):
        p = _build_payload(NotificationEvent.VIDEO_GENERATED, {})
        assert "Your video" in p.body

    def test_video_uploaded_with_url(self):
        p = _build_payload(NotificationEvent.VIDEO_UPLOADED,
                           {"title": "V", "platform": "YouTube", "url": "https://y.t/x"})
        assert "YouTube" in p.body and "https://y.t/x" in p.body

    def test_video_uploaded_no_url(self):
        p = _build_payload(NotificationEvent.VIDEO_UPLOADED,
                           {"title": "V", "platform": "TikTok"})
        assert "TikTok" in p.body
        assert p.title == "Published"

    def test_job_failed(self):
        p = _build_payload(NotificationEvent.JOB_FAILED,
                           {"job_type": "download", "error": "429"})
        assert p.title == "Download hit a snag"
        assert "429" in p.body
