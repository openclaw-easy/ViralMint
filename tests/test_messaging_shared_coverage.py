# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""backend/messaging/_shared.py — text helpers, pairing, MessagingConfig
helpers and the delivery-retry wrapper every channel sends through."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from backend.messaging import _shared
from tests._messaging_fakes import add_row, capture_ws, get_rows, no_retry_sleep, reset_channel_rows  # noqa: F401


# ── text helpers ─────────────────────────────────────────────────────────────

class TestText:
    def test_strip_action_blocks(self):
        assert _shared.strip_action_blocks("") == ""
        assert _shared.strip_action_blocks(None) == ""
        assert _shared.strip_action_blocks(
            "Sure! <action>{\"type\":\"x\"}\n</action>  ") == "Sure!"
        assert _shared.strip_action_blocks("<action>a</action>") == ""

    def test_short_text_is_one_chunk(self):
        assert _shared.split_text("hello", 10) == ["hello"]

    def test_paragraphs_are_packed_up_to_the_limit(self):
        text = "aaaa\n\nbbbb\n\ncccc"
        out = _shared.split_text(text, 10)
        assert out == ["aaaa\n\nbbbb", "cccc"]
        assert all(len(c) <= 10 for c in out)

    def test_an_oversize_paragraph_is_hard_chopped(self):
        text = "ab\n\n" + "x" * 25 + "\n\nend"
        out = _shared.split_text(text, 10)
        assert out == ["ab", "x" * 10, "x" * 10, "x" * 5, "end"]
        assert "".join(out).replace("\n", "") == text.replace("\n", "")

    def test_first_paragraph_oversize_with_empty_buffer(self):
        out = _shared.split_text("y" * 12, 5)
        assert out == ["yyyyy", "yyyyy", "yy"]


class TestPairCodeEdges:
    def test_text_without_any_word_characters(self):
        assert not _shared.matches_pair_code("!!! ???", "ABC234")

    def test_new_codes_differ(self):
        codes = {_shared.new_pair_code() for _ in range(20)}
        assert len(codes) > 1
        assert all(set(c) <= set(_shared._PAIR_ALPHABET) for c in codes)


# ── MessagingConfig helpers (real, isolated test DB) ─────────────────────────

CH = "cov_shared"


@pytest_asyncio.fixture
async def rows():
    await reset_channel_rows(CH)
    yield
    await reset_channel_rows(CH)


class TestConfigHelpers:
    @pytest.mark.asyncio
    async def test_load_config(self, rows):
        assert await _shared.load_config("u1", CH) is None
        await add_row(CH, user_id="u1", chat_id="42")
        cfg = await _shared.load_config("u1", CH)
        assert cfg is not None and cfg.chat_id == "42"

    @pytest.mark.asyncio
    async def test_persist_chat_id_stamps_connected_once(self, rows):
        await _shared.persist_chat_id("u1", CH, "nope")          # no row: a no-op
        assert await get_rows(CH) == []

        earlier = datetime(2020, 1, 1)
        await add_row(CH, user_id="u1", connected_at=earlier)
        await _shared.persist_chat_id("u1", CH, 777)
        (cfg,) = await get_rows(CH, "u1")
        assert cfg.chat_id == "777"
        assert cfg.connected_at == earlier, "connected_at is only stamped the first time"
        assert cfg.last_message_at is not None

        await add_row(CH, user_id="u2")
        await _shared.persist_chat_id("u2", CH, "9")
        (cfg2,) = await get_rows(CH, "u2")
        assert cfg2.connected_at is not None

    @pytest.mark.asyncio
    async def test_touch_last_message(self, rows):
        await _shared.touch_last_message("u1", CH)               # no row: no-op
        await add_row(CH, user_id="u1")
        await _shared.touch_last_message("u1", CH)
        (cfg,) = await get_rows(CH, "u1")
        assert cfg.last_message_at is not None

    @pytest.mark.asyncio
    async def test_deactivate_clears_tokens_or_keeps_them(self, rows):
        assert await _shared.deactivate_config("u1", CH) is False

        await add_row(CH, user_id="u1", chat_id="1", bot_token_encrypted="b",
                      api_key_encrypted="a", webhook_url_encrypted="w", is_active=True)
        assert await _shared.deactivate_config("u1", CH) is True
        (cfg,) = await get_rows(CH, "u1")
        assert not cfg.is_active and cfg.chat_id is None
        assert cfg.bot_token_encrypted is None and cfg.api_key_encrypted is None
        assert cfg.webhook_url_encrypted is None

        await add_row(CH, user_id="u2", chat_id="1", bot_token_encrypted="b", is_active=True)
        assert await _shared.deactivate_config("u2", CH, clear_tokens=False) is True
        (cfg2,) = await get_rows(CH, "u2")
        assert not cfg2.is_active and cfg2.chat_id is None
        assert cfg2.bot_token_encrypted == "b"


# ── send_with_retry ──────────────────────────────────────────────────────────

class TestSendWithRetry:
    @pytest.mark.asyncio
    async def test_every_chunk_sent_in_order(self, no_retry_sleep):
        sent = []

        async def send(c):
            sent.append(c)
        assert await _shared.send_with_retry("telegram", "u", ["a", "b", "c"], send)
        assert sent == ["a", "b", "c"] and no_retry_sleep == []

    @pytest.mark.asyncio
    async def test_a_transient_failure_retries_with_the_flat_delay(self, no_retry_sleep):
        calls = []

        async def send(c):
            calls.append(c)
            if len(calls) == 1:
                raise ConnectionError("blip")
        assert await _shared.send_with_retry("slack", "u", ["x"], send, retry_delay=0.25)
        assert calls == ["x", "x"] and no_retry_sleep == [0.25]

    @pytest.mark.asyncio
    async def test_a_second_failure_warns_the_ui_and_stops(self, monkeypatch, no_retry_sleep):
        ws = capture_ws(monkeypatch)
        calls = []

        async def send(c):
            calls.append(c)
            if c == "b":
                raise ConnectionError("down")
        ok = await _shared.send_with_retry("discord", "u9", ["a", "b", "c"], send)
        assert ok is False
        assert calls == ["a", "b", "b"], "chunk c must not be sent after b was dropped"
        (w,) = ws.warnings
        assert w["constraint"] == "discord_delivery" and w["user_id"] == "u9"
        assert w["wizard_id"] == "discord" and "Discord" in w["message"]

    @pytest.mark.asyncio
    async def test_no_retry_mode_fails_at_once(self, monkeypatch, no_retry_sleep):
        ws = capture_ws(monkeypatch)

        async def send(c):
            raise RuntimeError("nope")
        assert await _shared.send_with_retry("whatsapp", "u", ["a"], send, retry_once=False) is False
        assert no_retry_sleep == []
        assert ws.warnings[0]["constraint"] == "whatsapp_delivery"

    @pytest.mark.asyncio
    async def test_a_huge_retry_after_is_capped(self, no_retry_sleep):
        class Limited(Exception):
            retry_after = 999

        n = []

        async def send(c):
            n.append(c)
            if len(n) == 1:
                raise Limited()
        assert await _shared.send_with_retry("telegram", "u", ["a"], send)
        assert no_retry_sleep == [_shared.MAX_RETRY_AFTER_S]

    @pytest.mark.asyncio
    async def test_a_ws_failure_while_warning_is_swallowed(self, monkeypatch, no_retry_sleep):
        async def boom(**kw):
            raise RuntimeError("ws down")
        monkeypatch.setattr("backend.core.ws_manager.ws_manager.send_constraint_warning", boom)

        async def send(c):
            raise RuntimeError("nope")
        assert await _shared.send_with_retry("slack", "u", ["a"], send) is False


class TestRetryAfter:
    def test_attribute_number(self):
        assert _shared._retry_after(SimpleNamespace(retry_after=3)) == 3.0

    def test_attribute_timedelta(self):
        # python-telegram-bot ≥ 22 may hand back a timedelta.
        assert _shared._retry_after(SimpleNamespace(retry_after=timedelta(seconds=4))) == 4.0

    def test_response_header(self):
        err = SimpleNamespace(response=SimpleNamespace(headers={"Retry-After": "12"}))
        assert _shared._retry_after(err) == 12.0
        err = SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "5"}))
        assert _shared._retry_after(err) == 5.0

    def test_headers_that_cannot_be_read(self):
        class Weird:
            def get(self, k):
                raise KeyError(k)

            def __bool__(self):
                return True
        err = SimpleNamespace(response=SimpleNamespace(headers=Weird()))
        assert _shared._retry_after(err) == 0.0

    def test_garbage_and_missing(self):
        assert _shared._retry_after(SimpleNamespace(retry_after="soon")) == 0.0
        assert _shared._retry_after(ValueError("x")) == 0.0
        assert _shared._retry_after(SimpleNamespace(response=None)) == 0.0
