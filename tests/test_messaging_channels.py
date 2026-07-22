# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for the pure format/parse seams of the messaging channels + the shared
reliability wrapper. No real bot session is opened — the underlying client
libraries (python-telegram-bot, slack-sdk, discord.py, neonize) are never
connected; only the text-shaping helpers are exercised.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.messaging import _shared
from backend.messaging._shared import (
    send_with_retry,
    split_text,
    strip_action_blocks,
)


# ── _shared text helpers ─────────────────────────────────────────────────────

class TestStripActionBlocks:
    def test_removes_action_block(self):
        text = "Here you go.\n<action>{\"type\": \"start_scout\"}</action>\nDone."
        assert strip_action_blocks(text) == "Here you go.\n\nDone."

    def test_multiline_action_block(self):
        text = "Reply\n<action>\nmulti\nline\n</action>tail"
        assert "action" not in strip_action_blocks(text)
        assert "Reply" in strip_action_blocks(text)

    def test_empty_returns_empty(self):
        assert strip_action_blocks("") == ""
        assert strip_action_blocks(None) == ""

    def test_no_action_passthrough(self):
        assert strip_action_blocks("  plain  ") == "plain"


class TestSplitText:
    def test_short_text_single_chunk(self):
        assert split_text("hello", 100) == ["hello"]

    def test_splits_on_paragraphs(self):
        text = "a" * 30 + "\n\n" + "b" * 30
        chunks = split_text(text, 40)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 30
        assert chunks[1] == "b" * 30

    def test_hard_chops_oversized_paragraph(self):
        text = "x" * 250
        chunks = split_text(text, 100)
        assert all(len(c) <= 100 for c in chunks)
        assert "".join(chunks) == text

    def test_every_chunk_within_limit(self):
        text = "\n\n".join("word " * 40 for _ in range(5))
        chunks = split_text(text, 120)
        assert all(len(c) <= 120 for c in chunks)


# ── send_with_retry reliability wrapper ──────────────────────────────────────

class TestSendWithRetry:
    async def test_all_chunks_ok(self):
        sent = []

        async def send_fn(chunk):
            sent.append(chunk)

        ok = await send_with_retry("telegram", "u1", ["a", "b"], send_fn)
        assert ok is True
        assert sent == ["a", "b"]

    async def test_retry_succeeds_second_try(self):
        calls = {"n": 0}

        async def flaky(chunk):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")

        ok = await send_with_retry(
            "slack", "u1", ["only"], flaky, retry_delay=0)
        assert ok is True
        assert calls["n"] == 2   # failed once, retried once

    async def test_permanent_failure_emits_constraint_warning(self):
        async def always_fail(chunk):
            raise RuntimeError("dead")

        fake_ws = AsyncMock()
        with patch("backend.core.ws_manager.ws_manager", fake_ws):
            ok = await send_with_retry(
                "discord", "u1", ["x"], always_fail, retry_delay=0)
        assert ok is False
        fake_ws.send_constraint_warning.assert_awaited_once()
        kwargs = fake_ws.send_constraint_warning.call_args.kwargs
        assert kwargs["constraint"] == "discord_delivery"
        assert kwargs["user_id"] == "u1"

    async def test_no_retry_mode_fails_immediately(self):
        calls = {"n": 0}

        async def always_fail(chunk):
            calls["n"] += 1
            raise RuntimeError("dead")

        fake_ws = AsyncMock()
        with patch("backend.core.ws_manager.ws_manager", fake_ws):
            ok = await send_with_retry(
                "telegram", "u1", ["x"], always_fail, retry_once=False)
        assert ok is False
        assert calls["n"] == 1   # no retry attempt


# ── Telegram formatting seams ────────────────────────────────────────────────

class TestTelegramSeams:
    def test_md_strips_markdown_control_chars(self):
        from backend.messaging.telegram_channel import _md
        assert _md("a*b_c`d") == "abcd"
        assert _md(None) == ""

    def test_build_keyboard_from_buttons(self):
        from backend.messaging.telegram_channel import telegram_channel
        kb = telegram_channel._build_keyboard(
            [{"label": "Download top 5", "callback": "download:5"}])
        assert kb is not None
        button = kb.inline_keyboard[0][0]
        assert button.text == "Download top 5"
        assert button.callback_data == "planner:download:5"

    def test_build_keyboard_empty_returns_none(self):
        from backend.messaging.telegram_channel import telegram_channel
        assert telegram_channel._build_keyboard([]) is None

    def test_build_keyboard_skips_incomplete_buttons(self):
        from backend.messaging.telegram_channel import telegram_channel
        # Missing callback → dropped; nothing left → None.
        assert telegram_channel._build_keyboard([{"label": "x"}]) is None


# ── Discord formatting seams ─────────────────────────────────────────────────

class TestDiscordSeams:
    def test_build_invite_url(self):
        from backend.messaging.discord_channel import _build_invite_url
        url = _build_invite_url(123456789)
        assert "client_id=123456789" in url
        assert "scope=bot" in url
        assert "permissions=117760" in url


# ── WhatsApp formatting / parsing seams ──────────────────────────────────────

class TestWhatsAppSeams:
    def test_format_body_title_and_body(self):
        from backend.messaging.base import NotificationEvent, NotificationPayload
        from backend.messaging.whatsapp_channel import _format_body
        p = NotificationPayload(
            event=NotificationEvent.VIDEO_GENERATED, title="Ready", body="Your clip.")
        assert _format_body(p) == "*Ready*\nYour clip."

    def test_format_body_body_only(self):
        from backend.messaging.base import NotificationEvent, NotificationPayload
        from backend.messaging.whatsapp_channel import _format_body
        p = NotificationPayload(
            event=NotificationEvent.JOB_FAILED, title="", body="just body")
        assert _format_body(p) == "just body"

    def test_parse_stored_chat_id_with_server(self):
        from backend.messaging.whatsapp_channel import _parse_stored_chat_id
        assert _parse_stored_chat_id("12345@lid") == ("12345", "lid")

    def test_parse_stored_chat_id_legacy_defaults_server(self):
        from backend.messaging.whatsapp_channel import _parse_stored_chat_id
        assert _parse_stored_chat_id("12345") == ("12345", "s.whatsapp.net")

    def test_extract_self_jid_strips_device_suffix(self):
        from backend.messaging.whatsapp_channel import _extract_self_jid
        client = SimpleNamespace(me=SimpleNamespace(JID="12345:17@s.whatsapp.net"))
        assert _extract_self_jid(client) == "12345"

    def test_extract_self_jid_none_when_absent(self):
        from backend.messaging.whatsapp_channel import _extract_self_jid
        client = SimpleNamespace()
        assert _extract_self_jid(client) is None
