# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Connecting a bot token must never take a webhook away from whatever owns it.

python-telegram-bot's `start_polling` calls deleteWebhook. Pasting a token that was serving a webhook therefore cut that service off silently.
These run the real python-telegram-bot against a local stub of the Bot API, so what's asserted is the calls Telegram would have seen."""
import asyncio
import json

import pytest
import pytest_asyncio
from telegram import Bot
from telegram.ext import ApplicationBuilder, Updater

from backend.messaging import telegram_channel as tc
from tests._messaging_fakes import add_row, get_rows, reset_channel_rows

TOKEN = "123:stub"
USER = "guard-u"
HOOK = "https://hooks.example.com/secret-path/abc"


class StubTelegram:
    """Just enough Bot API over real HTTP, on a port the OS picks."""

    def __init__(self):
        self.webhook_url = ""
        self.calls = []
        self.fail_webhook_info = False

    def _answer(self, method):
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": 123, "is_bot": True, "first_name": "Stub", "username": "stub_bot"}}
        if method == "getWebhookInfo":
            if self.fail_webhook_info:
                return 500, {"ok": False, "error_code": 500, "description": "Internal Server Error"}
            return 200, {"ok": True, "result": {"url": self.webhook_url, "has_custom_certificate": False, "pending_update_count": 0}}
        if method == "deleteWebhook":
            self.webhook_url = ""
            return 200, {"ok": True, "result": True}
        if method == "getUpdates":
            return 200, {"ok": True, "result": []}
        return 200, {"ok": True, "result": True}

    async def _serve(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            length = next((int(x.split(":")[1]) for x in lines if x.lower().startswith("content-length:")), 0)
            if length:
                await reader.readexactly(length)
            method = lines[0].split(" ")[1].rsplit("/", 1)[-1]
            self.calls.append(method)
            if method == "getUpdates":
                await asyncio.sleep(0.05)
            status, body = self._answer(method)
            raw = json.dumps(body).encode()
            writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
            await writer.drain()
        finally:
            writer.close()

    async def start(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.base_url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/bot"

    async def stop(self):
        self.server.close()


@pytest_asyncio.fixture
async def stub(monkeypatch):
    s = StubTelegram()
    await s.start()
    monkeypatch.setattr(tc, "Bot", lambda token: Bot(token, base_url=s.base_url))
    monkeypatch.setattr(tc, "ApplicationBuilder", lambda: ApplicationBuilder().base_url(s.base_url))
    yield s
    await s.stop()


@pytest_asyncio.fixture
async def rows():
    await reset_channel_rows("telegram")
    yield
    await reset_channel_rows("telegram")


class TestABotWithAWebhook:
    @pytest.mark.asyncio
    async def test_connect_refuses_in_plain_words_and_touches_nothing(self, stub, rows):
        stub.webhook_url = HOOK
        ch = tc.TelegramChannel()
        with pytest.raises(ValueError) as e:
            await ch.configure(USER, TOKEN)
        words = str(e.value)
        assert "hooks.example.com" in words and "@BotFather" in words
        assert "secret-path" not in words, "a webhook's path can hold its secret; only the host is shown"
        assert "deleteWebhook" not in stub.calls and "getUpdates" not in stub.calls
        assert stub.webhook_url == HOOK
        assert await get_rows("telegram", USER) == [], "a refused token is not saved"
        assert USER not in ch._bots

    @pytest.mark.asyncio
    async def test_the_refusal_reaches_the_page_as_a_400_with_those_words(self, stub, rows, monkeypatch):
        from fastapi import HTTPException
        from backend.api import messaging as api
        stub.webhook_url = HOOK
        monkeypatch.setattr(api, "USER_ID", USER)
        channel = tc.TelegramChannel()
        monkeypatch.setattr(api.messaging, "get_channel", lambda name: channel)
        with pytest.raises(HTTPException) as e:
            await api._connect_channel("telegram", bot_token=TOKEN)
        assert e.value.status_code == 400 and "hooks.example.com" in e.value.detail

    @pytest.mark.asyncio
    async def test_a_bot_already_running_here_keeps_running_when_another_token_is_refused(self, stub, rows):
        stub.webhook_url = HOOK
        ch = tc.TelegramChannel()
        running = object()
        ch._bots[USER] = running
        with pytest.raises(ValueError):
            await ch.configure(USER, TOKEN)
        assert ch._bots[USER] is running

    @pytest.mark.asyncio
    async def test_boot_and_retry_paths_are_guarded_too(self, stub):
        """They reach polling without going through configure's probe."""
        stub.webhook_url = HOOK
        with pytest.raises(tc.WebhookInUse):
            await tc.TelegramChannel()._spin_up_bot(USER, TOKEN, chat_id=5)
        assert "deleteWebhook" not in stub.calls and stub.webhook_url == HOOK

    @pytest.mark.asyncio
    async def test_at_boot_it_warns_keeps_the_row_and_does_not_retry(self, stub, rows, monkeypatch):
        from backend.core.crypto import encrypt
        from backend.core.ws_manager import ws_manager
        warnings = []

        async def warn(**kw):
            warnings.append(kw)
        monkeypatch.setattr(ws_manager, "send_constraint_warning", warn)
        await add_row("telegram", user_id=USER, is_active=True, bot_token_encrypted=encrypt(TOKEN), chat_id="5")
        stub.webhook_url = HOOK
        ch = tc.TelegramChannel()
        await ch.start()
        assert USER not in ch._bots and USER not in ch._retry_tasks
        assert "deleteWebhook" not in stub.calls
        assert warnings and "hooks.example.com" in warnings[0]["message"]
        (row,) = await get_rows("telegram", USER)
        assert row.is_active, "the row stays: once the webhook is gone the next start brings the bot back"

    @pytest.mark.asyncio
    async def test_a_retry_stops_at_a_webhook(self, stub, monkeypatch):
        monkeypatch.setattr(tc, "START_RETRY_DELAYS_S", (0, 0, 0))
        stub.webhook_url = HOOK
        ch = tc.TelegramChannel()
        ch._schedule_retry(USER, TOKEN, 5)
        await asyncio.wait_for(ch._retry_tasks[USER], timeout=10)
        assert stub.calls.count("getWebhookInfo") == 1, "one refusal ends the retries"
        assert "deleteWebhook" not in stub.calls


class TestWhenTheCheckFails:
    @pytest.mark.asyncio
    async def test_connect_fails_closed(self, stub, rows):
        stub.webhook_url = HOOK
        stub.fail_webhook_info = True
        ch = tc.TelegramChannel()
        with pytest.raises(ValueError):
            await ch.configure(USER, TOKEN)
        assert "deleteWebhook" not in stub.calls and stub.webhook_url == HOOK
        assert await get_rows("telegram", USER) == []


class TestANormalToken:
    @pytest.mark.asyncio
    async def test_the_flow_is_what_it_was_plus_one_read(self, stub, rows):
        """The same connect, the same polling start (deleteWebhook with no webhook is python-telegram-bot's own bootstrap), the same answer: code, link, saved row."""
        ch = tc.TelegramChannel()
        out = await ch.configure(USER, TOKEN)
        try:
            assert out["awaiting_start"] is True and out["bot_username"] == "stub_bot"
            assert out["bot_url"] == f"https://t.me/stub_bot?start={out['pair_code']}"
            (row,) = await get_rows("telegram", USER)
            assert row.is_active and row.chat_id is None
            assert "getWebhookInfo" in stub.calls[:stub.calls.index("deleteWebhook")], "the check comes first"
            for _ in range(100):
                if "getUpdates" in stub.calls:
                    break
                await asyncio.sleep(0.02)
            assert "getUpdates" in stub.calls, "the bot is really polling"
            assert set(stub.calls) <= {"getMe", "getWebhookInfo", "deleteWebhook", "getUpdates"}
        finally:
            await ch.disconnect(USER)

    @pytest.mark.asyncio
    async def test_polling_still_drops_the_backlog(self, stub, monkeypatch):
        seen = {}
        real = Updater.start_polling

        async def spy(self, **kw):
            seen.update(kw)
            return await real(self, **kw)
        monkeypatch.setattr(Updater, "start_polling", spy)
        ch = tc.TelegramChannel()
        await ch._spin_up_bot(USER, TOKEN, chat_id=5)
        try:
            assert seen == {"drop_pending_updates": True}
        finally:
            await ch._bots[USER].stop_polling()
