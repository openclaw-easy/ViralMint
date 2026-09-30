# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""backend/messaging/telegram_channel.py — lifecycle, retry-on-boot, sends,
connect/disconnect and every inbound handler, all against fakes.

python-telegram-bot is never allowed near the network: `Bot`,
`ApplicationBuilder` and `_spin_up_bot` are replaced wherever a test would
otherwise build a real client.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from telegram.error import BadRequest, InvalidToken, NetworkError, TelegramError

from backend.messaging import telegram_channel as tc
from backend.messaging._shared import UNPAIRED_REPLY
from backend.messaging.base import NotificationEvent, NotificationPayload
from tests._messaging_fakes import add_row, capture_ws, get_rows, no_retry_sleep, reset_channel_rows  # noqa: F401


def _app(*, updater_running=True, running=True):
    app = MagicMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.running = running
    app.updater.running = updater_running
    app.updater.start_polling = AsyncMock()
    app.updater.stop = AsyncMock()
    app.bot.send_message = AsyncMock()
    return app


def _update(chat_id, text="hi", chat_type="private", *, reply_side_effect=None):
    msg = SimpleNamespace(text=text, chat_id=chat_id,
                          reply_text=AsyncMock(side_effect=reply_side_effect))
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
                           effective_message=msg, callback_query=None)


def _ctx(args=None, chat_action_error=None):
    return SimpleNamespace(args=args or [], bot=SimpleNamespace(
        send_chat_action=AsyncMock(side_effect=chat_action_error)))


@pytest.fixture
def quiet_db(monkeypatch):
    """Handlers touch MessagingConfig through these — record instead."""
    touched, persisted = [], []

    async def touch(user_id, channel):
        touched.append((user_id, channel))

    async def persist(user_id, channel, chat_id):
        persisted.append((user_id, channel, chat_id))
    monkeypatch.setattr(tc, "touch_last_message", touch)
    monkeypatch.setattr(tc, "persist_chat_id", persist)
    return SimpleNamespace(touched=touched, persisted=persisted)


@pytest.fixture
def owned(quiet_db, monkeypatch):
    """A channel whose bot is already paired to private chat 222."""
    ws = capture_ws(monkeypatch)
    ch = tc.TelegramChannel()
    planner = AsyncMock(return_value="agent reply <action>{}</action>")
    ch.set_planner_callback(planner)
    app = _app()
    bot = tc._UserBot("local", app, 222, "vm_bot")
    ch._bots["local"] = bot
    return SimpleNamespace(ch=ch, bot=bot, app=app, planner=planner, db=quiet_db, ws=ws)


@pytest_asyncio.fixture
async def tg_rows():
    await reset_channel_rows("telegram")
    yield
    await reset_channel_rows("telegram")


# ── _UserBot polling lifecycle ───────────────────────────────────────────────

class TestUserBot:
    def test_a_saved_owner_needs_no_code(self):
        assert tc._UserBot("u", _app(), 5, "b").pair_code is None
        assert tc._UserBot("u", _app(), None, "b").pair_code

    @pytest.mark.asyncio
    async def test_start_polling_drops_the_backlog(self):
        app = _app()
        await tc._UserBot("u", app, 1, "b").start_polling()
        app.initialize.assert_awaited_once()
        app.start.assert_awaited_once()
        app.updater.start_polling.assert_awaited_once_with(drop_pending_updates=True)

    @pytest.mark.asyncio
    async def test_stop_polling_stops_what_runs(self):
        app = _app()
        await tc._UserBot("u", app, 1, "b").stop_polling()
        app.updater.stop.assert_awaited_once()
        app.stop.assert_awaited_once()
        app.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_polling_skips_what_is_not_running(self):
        app = _app(updater_running=False, running=False)
        await tc._UserBot("u", app, 1, "b").stop_polling()
        app.updater.stop.assert_not_called()
        app.stop.assert_not_called()
        app.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_hung_shutdown_is_abandoned(self, monkeypatch):
        monkeypatch.setattr(tc, "STOP_TIMEOUT_S", 0.01)
        app = _app()

        async def hang():
            await asyncio.sleep(5)
        app.updater.stop = hang
        t0 = asyncio.get_running_loop().time()
        await tc._UserBot("u", app, 1, "b").stop_polling()
        assert asyncio.get_running_loop().time() - t0 < 1

    @pytest.mark.asyncio
    async def test_a_shutdown_error_is_swallowed(self):
        app = _app()
        app.shutdown = AsyncMock(side_effect=RuntimeError("boom"))
        await tc._UserBot("u", app, 1, "b").stop_polling()      # must not raise


# ── start(): boot from the DB ────────────────────────────────────────────────

class TestStart:
    @pytest.mark.asyncio
    async def test_boot_spins_up_valid_rows_and_retries_network_failures(self, tg_rows, monkeypatch):
        from backend.core.crypto import encrypt
        await add_row("telegram", user_id="ok", is_active=True,
                      bot_token_encrypted=encrypt("1:ok"), chat_id="222")
        await add_row("telegram", user_id="notoken", is_active=True, bot_token_encrypted=None)
        await add_row("telegram", user_id="badcipher", is_active=True, bot_token_encrypted="garbage")
        await add_row("telegram", user_id="revoked", is_active=True,
                      bot_token_encrypted=encrypt("2:revoked"))
        await add_row("telegram", user_id="offline", is_active=True,
                      bot_token_encrypted=encrypt("3:offline"), chat_id="9")
        await add_row("telegram", user_id="inactive", is_active=False,
                      bot_token_encrypted=encrypt("4:x"))

        ch = tc.TelegramChannel()
        spun, retries = [], []

        async def spin(user_id, token, chat_id):
            spun.append((user_id, token, chat_id))
            if user_id == "revoked":
                raise InvalidToken()
            if user_id == "offline":
                raise NetworkError("dns")
        monkeypatch.setattr(ch, "_spin_up_bot", spin)
        monkeypatch.setattr(ch, "_schedule_retry", lambda *a: retries.append(a))

        await ch.start()
        assert sorted(u for u, *_ in spun) == ["offline", "ok", "revoked"]
        assert ("ok", "1:ok", 222) in spun
        assert retries == [("offline", "3:offline", 9)], "only a network failure is retried"


# ── _schedule_retry / stop ───────────────────────────────────────────────────

class TestRetry:
    @pytest.mark.asyncio
    async def test_retry_until_the_bot_starts(self, monkeypatch):
        monkeypatch.setattr(tc, "START_RETRY_DELAYS_S", (0, 0, 0))
        ch = tc.TelegramChannel()
        attempts = []

        async def spin(user_id, token, chat_id):
            attempts.append(chat_id)
            if len(attempts) < 2:
                raise NetworkError("still offline")
            ch._bots[user_id] = tc._UserBot(user_id, _app(), chat_id, "b")
        monkeypatch.setattr(ch, "_spin_up_bot", spin)

        ch._schedule_retry("u", "tok", 7)
        assert ch.status("u")["retrying"] is True
        task = ch._retry_tasks["u"]
        await task
        await asyncio.sleep(0)                  # let the done-callback run
        assert attempts == [7, 7]
        assert "u" in ch._bots and "u" not in ch._retry_tasks

    @pytest.mark.asyncio
    async def test_retry_gives_up_on_an_invalid_token(self, monkeypatch):
        monkeypatch.setattr(tc, "START_RETRY_DELAYS_S", (0, 0, 0))
        ch = tc.TelegramChannel()
        spin = AsyncMock(side_effect=InvalidToken())
        monkeypatch.setattr(ch, "_spin_up_bot", spin)
        ch._schedule_retry("u", "tok", None)
        await ch._retry_tasks["u"]
        assert spin.await_count == 1 and "u" not in ch._bots

    @pytest.mark.asyncio
    async def test_retry_exhausts_its_schedule(self, monkeypatch):
        monkeypatch.setattr(tc, "START_RETRY_DELAYS_S", (0, 0))
        ch = tc.TelegramChannel()
        spin = AsyncMock(side_effect=NetworkError("x"))
        monkeypatch.setattr(ch, "_spin_up_bot", spin)
        ch._schedule_retry("u", "tok", None)
        await ch._retry_tasks["u"]
        assert spin.await_count == 2 and "u" not in ch._bots

    @pytest.mark.asyncio
    async def test_a_manual_connect_that_won_stops_the_retry(self, monkeypatch):
        monkeypatch.setattr(tc, "START_RETRY_DELAYS_S", (0,))
        ch = tc.TelegramChannel()
        ch._bots["u"] = tc._UserBot("u", _app(), 1, "b")
        spin = AsyncMock()
        monkeypatch.setattr(ch, "_spin_up_bot", spin)
        ch._schedule_retry("u", "tok", 1)
        await ch._retry_tasks["u"]
        spin.assert_not_called()

    @pytest.mark.asyncio
    async def test_rescheduling_cancels_the_older_retry(self, monkeypatch):
        monkeypatch.setattr(tc, "START_RETRY_DELAYS_S", (60,))
        ch = tc.TelegramChannel()
        ch._schedule_retry("u", "tok", None)
        first = ch._retry_tasks["u"]
        ch._schedule_retry("u", "tok2", None)
        second = ch._retry_tasks["u"]
        await asyncio.sleep(0)
        assert first.cancelled() and second is not first
        ch._cancel_retry("u")
        await asyncio.sleep(0)
        assert second.cancelled() and "u" not in ch._retry_tasks
        ch._cancel_retry("nobody")               # no task: no error

    @pytest.mark.asyncio
    async def test_stop_cancels_retries_and_stops_every_bot(self, monkeypatch):
        monkeypatch.setattr(tc, "START_RETRY_DELAYS_S", (60,))
        ch = tc.TelegramChannel()
        ch._schedule_retry("r", "tok", None)
        retry = ch._retry_tasks["r"]
        a, b = _app(), _app()
        ch._bots["a"] = tc._UserBot("a", a, 1, "x")
        ch._bots["b"] = tc._UserBot("b", b, 2, "y")
        await ch.stop()
        await asyncio.sleep(0)
        assert retry.cancelled() and not ch._bots and not ch._retry_tasks
        a.shutdown.assert_awaited_once()
        b.shutdown.assert_awaited_once()


# ── send() ───────────────────────────────────────────────────────────────────

class TestSend:
    @pytest.mark.asyncio
    async def test_nothing_to_send_to(self, quiet_db):
        ch = tc.TelegramChannel()
        payload = NotificationPayload(event=NotificationEvent.JOB_FAILED, title="t", body="b")
        assert await ch.send("u", payload) is False
        assert await ch.is_configured("u") is False
        ch._bots["u"] = tc._UserBot("u", _app(), None, "b")        # unpaired
        assert await ch.send("u", payload) is False
        assert await ch.is_configured("u") is False

    @pytest.mark.asyncio
    async def test_title_is_escaped_and_buttons_ride_the_last_chunk(self, owned, monkeypatch):
        monkeypatch.setattr(tc, "TG_MSG_LIMIT", 20)
        payload = NotificationPayload(
            event=NotificationEvent.SCOUT_COMPLETE, title="Top *pick_`x`",
            body="aaaaaaaaaaaaaaa\n\nbbbbbbbbbbbbbbb",
            action_buttons=[{"label": "Download", "callback": "download 1"}])
        assert await owned.ch.is_configured("local") is True
        assert await owned.ch.send("local", payload) is True
        calls = owned.app.bot.send_message.await_args_list
        assert len(calls) == 3
        assert calls[0].kwargs["text"].startswith("*Top pickx*")
        assert all(c.kwargs["chat_id"] == 222 for c in calls)
        assert [c.kwargs["reply_markup"] is None for c in calls] == [True, True, False]
        kb = calls[-1].kwargs["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == "planner:download 1"
        assert owned.db.touched == [("local", "telegram")]

    @pytest.mark.asyncio
    async def test_a_rejected_markdown_message_is_resent_plain(self, owned):
        owned.app.bot.send_message = AsyncMock(side_effect=[BadRequest("can't parse entities"), None])
        payload = NotificationPayload(event=NotificationEvent.JOB_FAILED, title="", body="plain *body")
        assert await owned.ch.send("local", payload) is True
        first, second = owned.app.bot.send_message.await_args_list
        assert first.kwargs["parse_mode"] == "Markdown" and first.kwargs["text"] == "plain *body"
        assert "parse_mode" not in second.kwargs

    @pytest.mark.asyncio
    async def test_a_timeout_is_not_resent_as_plain_text(self, owned, monkeypatch, no_retry_sleep):
        """A timeout may already have been delivered — only BadRequest downgrades."""
        ws = capture_ws(monkeypatch)
        owned.app.bot.send_message = AsyncMock(side_effect=NetworkError("timed out"))
        payload = NotificationPayload(event=NotificationEvent.JOB_FAILED, title="t", body="b")
        assert await owned.ch.send("local", payload) is False
        # One try + one retry, both Markdown — never a plain-text duplicate.
        assert owned.app.bot.send_message.await_count == 2
        assert all(c.kwargs.get("parse_mode") == "Markdown"
                   for c in owned.app.bot.send_message.await_args_list)
        assert owned.db.touched == []
        assert ws.warnings and ws.warnings[0]["constraint"] == "telegram_delivery"

    @pytest.mark.asyncio
    async def test_send_test(self, owned):
        assert await owned.ch.send_test("local") is True
        text = owned.app.bot.send_message.await_args.kwargs["text"]
        assert "ViralMint test" in text and "Telegram" in text


class TestKeyboardAndMarkdown:
    def test_keyboard(self):
        ch = tc.TelegramChannel()
        assert ch._build_keyboard([]) is None
        assert ch._build_keyboard([{"label": "x"}, {"callback": "y"}]) is None
        kb = ch._build_keyboard([{"label": "Go", "callback": "z" * 100}, {"label": ""}])
        (row,) = kb.inline_keyboard
        assert len(row[0].callback_data) == 64 and row[0].callback_data.startswith("planner:")

    def test_md(self):
        assert tc._md("a*b_c`d") == "abcd"
        assert tc._md(None) == ""


# ── configure / disconnect ───────────────────────────────────────────────────

class _ProbeBot:
    error: Exception | None = None

    def __init__(self, token):
        self.token = token

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_me(self):
        if self.error:
            raise self.error
        return SimpleNamespace(username="vm_bot")


class TestConfigure:
    @pytest.mark.asyncio
    async def test_an_invalid_token_is_a_value_error(self, monkeypatch):
        probe = type("P", (_ProbeBot,), {"error": InvalidToken()})
        monkeypatch.setattr(tc, "Bot", probe)
        ch = tc.TelegramChannel()
        with pytest.raises(ValueError, match="Invalid Telegram bot token"):
            await ch.configure("u", "bad")

    @pytest.mark.asyncio
    async def test_any_other_telegram_error_is_a_value_error(self, monkeypatch):
        probe = type("P", (_ProbeBot,), {"error": TelegramError("flood")})
        monkeypatch.setattr(tc, "Bot", probe)
        running = tc._UserBot("u", _app(), 5, "old")
        ch = tc.TelegramChannel()
        ch._bots["u"] = running
        with pytest.raises(ValueError, match="Telegram rejected token: flood"):
            await ch.configure("u", "x")
        assert ch._bots["u"] is running, "a failed probe must not stop the working bot"

    @pytest.mark.asyncio
    async def test_first_connect_creates_the_row_and_replaces_a_running_bot(self, tg_rows, monkeypatch):
        from backend.core.crypto import decrypt_safe
        monkeypatch.setattr(tc, "Bot", _ProbeBot)
        ch = tc.TelegramChannel()
        old_app = _app()
        ch._bots["cov-u"] = tc._UserBot("cov-u", old_app, None, "old")
        ch._retry_tasks["cov-u"] = asyncio.create_task(asyncio.sleep(60))
        retry = ch._retry_tasks["cov-u"]

        async def spin(user_id, token, chat_id):
            ch._bots[user_id] = tc._UserBot(user_id, _app(), chat_id, "vm_bot")
        monkeypatch.setattr(ch, "_spin_up_bot", spin)

        out = await ch.configure("cov-u", "123:abc")
        await asyncio.sleep(0)
        assert retry.cancelled()
        old_app.shutdown.assert_awaited_once()
        assert out["awaiting_start"] and out["bot_username"] == "vm_bot"
        assert out["bot_url"] == f"https://t.me/vm_bot?start={out['pair_code']}"
        (row,) = await get_rows("telegram", "cov-u")
        assert row.is_active and decrypt_safe(row.bot_token_encrypted) == "123:abc"
        assert row.chat_id is None and row.connected_at is not None

    @pytest.mark.asyncio
    async def test_disconnect_stops_the_bot_and_clears_the_row(self, tg_rows, quiet_db, monkeypatch):
        from backend.core.crypto import encrypt
        await add_row("telegram", user_id="cov-u", is_active=True,
                      bot_token_encrypted=encrypt("1:a"), chat_id="222")
        ch = tc.TelegramChannel()
        app = _app()
        ch._bots["cov-u"] = tc._UserBot("cov-u", app, 222, "b")
        monkeypatch.setattr(tc, "START_RETRY_DELAYS_S", (60,))
        ch._schedule_retry("cov-u", "t", None)
        retry = ch._retry_tasks["cov-u"]

        assert await ch.disconnect("cov-u") is True
        await asyncio.sleep(0)
        app.shutdown.assert_awaited_once()
        assert retry.cancelled() and "cov-u" not in ch._bots
        (row,) = await get_rows("telegram", "cov-u")
        assert not row.is_active and row.chat_id is None and row.bot_token_encrypted is None
        st = ch.status("cov-u")
        assert st == {"connected": False, "awaiting_start": False, "bot_username": None,
                      "bot_url": None, "chat_id": None, "pair_code": None, "retrying": False}

        assert await ch.disconnect("cov-u") is True             # idempotent


class TestSpinUp:
    @pytest.mark.asyncio
    async def test_spin_up_registers_handlers_and_starts_polling(self, monkeypatch):
        app = _app()
        app.bot.get_me = AsyncMock(return_value=SimpleNamespace(username=None))
        builder = MagicMock()
        builder.token.return_value.build.return_value = app
        monkeypatch.setattr(tc, "ApplicationBuilder", lambda: builder)
        ch = tc.TelegramChannel()
        await ch._spin_up_bot("u", "1:a", chat_id=None)
        builder.token.assert_called_once_with("1:a")
        app.updater.start_polling.assert_awaited_once()
        assert app.add_handler.call_count == 5
        bot = ch._bots["u"]
        assert bot.bot_username == "" and bot.pair_code
        # No username → no link at all, not a broken "https://t.me/?start=…".
        assert ch.status("u")["bot_url"] is None


# ── inbound handlers ─────────────────────────────────────────────────────────

class TestStartHandler:
    @pytest.mark.asyncio
    async def test_the_owner_restarting_gets_the_welcome(self, owned):
        up = _update(222, "/start")
        await owned.ch._make_start_handler("local")(up, _ctx())
        up.effective_message.reply_text.assert_awaited_once()
        assert "connected" in up.effective_message.reply_text.await_args.args[0]
        assert owned.db.persisted == [], "re-starting must not re-persist"

    @pytest.mark.asyncio
    async def test_an_update_without_chat_is_ignored(self, owned):
        up = SimpleNamespace(effective_chat=None, effective_message=None)
        await owned.ch._make_start_handler("local")(up, _ctx())

    @pytest.mark.asyncio
    async def test_no_bot_refuses_quietly(self, quiet_db):
        ch = tc.TelegramChannel()
        up = _update(1, "/start X")
        await ch._make_start_handler("gone")(up, _ctx(["X"]))
        up.effective_message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refusal_whose_reply_fails_is_swallowed(self, quiet_db):
        ch = tc.TelegramChannel()
        ch._bots["u"] = tc._UserBot("u", _app(), None, "b")
        up = _update(5, "/start", reply_side_effect=TelegramError("blocked"))
        await ch._make_start_handler("u")(up, _ctx())          # must not raise
        up.effective_message.reply_text.assert_awaited_once_with(UNPAIRED_REPLY)


class TestHelpHandler:
    @pytest.mark.asyncio
    async def test_owner_gets_help_stranger_gets_nothing(self, owned):
        h = owned.ch._make_help_handler("local")
        stranger = _update(333, "/help")
        await h(stranger, _ctx())
        stranger.effective_message.reply_text.assert_not_called()
        owner = _update(222, "/help")
        await h(owner, _ctx())
        assert "/status" in owner.effective_message.reply_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_an_unpaired_bot_tells_a_private_chat_it_is_private(self, quiet_db):
        ch = tc.TelegramChannel()
        ch._bots["u"] = tc._UserBot("u", _app(), None, "b")
        up = _update(5, "/help")
        await ch._make_help_handler("u")(up, _ctx())
        up.effective_message.reply_text.assert_awaited_once_with(UNPAIRED_REPLY)
        group = _update(-5, "/help", chat_type="group")
        await ch._make_help_handler("u")(group, _ctx())
        group.effective_message.reply_text.assert_not_called()


def _callback_update(chat_id, data, *, chat_type="private"):
    query = SimpleNamespace(
        data=data, answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()))
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
                           effective_message=None, callback_query=query), query


class TestCallbackHandler:
    @pytest.mark.asyncio
    async def test_no_query_is_ignored(self, owned):
        up = SimpleNamespace(callback_query=None, effective_chat=None)
        await owned.ch._make_callback_handler("local")(up, _ctx())

    @pytest.mark.asyncio
    async def test_a_stranger_pressing_an_old_button_runs_nothing(self, owned):
        up, q = _callback_update(333, "planner:download 1")
        await owned.ch._make_callback_handler("local")(up, _ctx())
        q.answer.assert_awaited_once()               # the spinner is always cleared
        owned.planner.assert_not_called()
        q.edit_message_reply_markup.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_owner_button_runs_the_planner(self, owned):
        up, q = _callback_update(222, "planner:download 1")
        await owned.ch._make_callback_handler("local")(up, _ctx())
        owned.planner.assert_awaited_once_with("download 1", "local")
        q.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        q.message.reply_text.assert_awaited_once()
        assert q.message.reply_text.await_args.args[0] == "agent reply"
        assert owned.db.touched == [("local", "telegram")]

    @pytest.mark.asyncio
    async def test_a_non_planner_payload_only_answers(self, owned):
        up, q = _callback_update(222, "other:thing")
        await owned.ch._make_callback_handler("local")(up, _ctx())
        owned.planner.assert_not_called()
        q.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_planner_just_clears_the_buttons(self, owned):
        owned.ch._planner_callback = None
        up, q = _callback_update(222, "planner:x")
        await owned.ch._make_callback_handler("local")(up, _ctx())
        q.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        q.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_planner_crash_and_a_markdown_failure(self, owned):
        owned.planner.side_effect = RuntimeError("boom")
        up, q = _callback_update(222, "planner:x")
        q.message.reply_text = AsyncMock(side_effect=[TelegramError("parse"), None])
        await owned.ch._make_callback_handler("local")(up, _ctx())
        first, second = q.message.reply_text.await_args_list
        assert first.args[0] == "Something went wrong handling that action."
        assert first.kwargs["parse_mode"] == "Markdown" and "parse_mode" not in second.kwargs


class TestTextHandler:
    @pytest.mark.asyncio
    async def test_empty_or_missing_messages_are_ignored(self, owned):
        h = owned.ch._make_text_handler("local")
        await h(SimpleNamespace(effective_message=None, effective_chat=None), _ctx())
        await h(_update(222, ""), _ctx())
        owned.planner.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_turn_strips_actions_and_marks_activity(self, owned):
        up = _update(222, "scout cooking")
        ctx = _ctx(chat_action_error=NetworkError("typing failed"))   # non-fatal
        await owned.ch._make_text_handler("local")(up, ctx)
        owned.planner.assert_awaited_once_with("scout cooking", "local")
        up.effective_message.reply_text.assert_awaited_once()
        assert up.effective_message.reply_text.await_args.args[0] == "agent reply"
        assert owned.db.touched == [("local", "telegram")]

    @pytest.mark.asyncio
    async def test_an_action_only_reply_becomes_done(self, owned):
        owned.planner.return_value = "<action>{\"type\":\"start_scout\"}</action>"
        up = _update(222, "go")
        await owned.ch._make_text_handler("local")(up, _ctx())
        assert up.effective_message.reply_text.await_args.args[0] == "Done. ✅"

    @pytest.mark.asyncio
    async def test_no_planner_yet(self, owned):
        owned.ch._planner_callback = None
        up = _update(222, "hi")
        await owned.ch._make_text_handler("local")(up, _ctx())
        assert "Planner not ready" in up.effective_message.reply_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_a_planner_crash_is_reported(self, owned):
        owned.planner.side_effect = RuntimeError("boom")
        up = _update(222, "hi")
        await owned.ch._make_text_handler("local")(up, _ctx())
        assert "Something went wrong" in up.effective_message.reply_text.await_args.args[0]
        assert owned.db.touched == []

    @pytest.mark.asyncio
    async def test_a_markdown_failure_falls_back_to_plain(self, owned):
        up = _update(222, "hi", reply_side_effect=[TelegramError("parse"), None])
        await owned.ch._make_text_handler("local")(up, _ctx())
        first, second = up.effective_message.reply_text.await_args_list
        assert first.kwargs["parse_mode"] == "Markdown" and "parse_mode" not in second.kwargs
        assert owned.db.touched == [("local", "telegram")]

    @pytest.mark.asyncio
    async def test_a_reply_that_never_lands_is_not_counted(self, owned, no_retry_sleep):
        up = _update(222, "hi", reply_side_effect=TelegramError("down"))
        await owned.ch._make_text_handler("local")(up, _ctx())
        assert owned.db.touched == []


class TestRecentJobs:
    @pytest.mark.asyncio
    async def test_summary_reads_the_latest_five(self):
        from backend.database import AsyncSessionLocal, init_db
        from backend.models.job import Job
        from sqlalchemy import delete
        await init_db()
        uid = "cov-tg-jobs"
        ch = tc.TelegramChannel()
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Job).where(Job.user_id == uid))
            await db.commit()
        assert await ch._recent_jobs_summary(uid) == "No recent jobs."
        base = datetime(2026, 1, 1)
        statuses = ["success", "failed", "running", "pending", "cancelled", "weird"]
        async with AsyncSessionLocal() as db:
            for i, st in enumerate(statuses):
                db.add(Job(user_id=uid, job_type="scout", status=st,
                           title=("T" * 80 if i == 5 else None),
                           created_at=base + timedelta(minutes=i)))
            await db.commit()
        try:
            text = await ch._recent_jobs_summary(uid)
            lines = text.splitlines()
            assert lines[0] == "Recent jobs:" and len(lines) == 6
            assert lines[1] == "• scout — " + "T" * 60 + " (weird)"      # newest first, clipped
            assert lines[2] == "🚫 scout — scout (cancelled)"            # title falls back to type
            assert lines[-1].startswith("❌")
            assert not any("success" in l for l in lines), "only the latest five"
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(Job).where(Job.user_id == uid))
                await db.commit()
