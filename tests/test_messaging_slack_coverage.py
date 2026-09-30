# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""backend/messaging/slack_channel.py — boot, connect (both tokens checked
before the running bot is touched), disconnect, status, sends and the Socket
Mode listener, against fakes. slack-sdk's clients are never built for real.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

pytest.importorskip("slack_sdk")

from backend.messaging import slack_channel as scm  # noqa: E402
from backend.messaging._shared import UNPAIRED_REPLY  # noqa: E402
from backend.messaging.base import NotificationEvent, NotificationPayload  # noqa: E402
from tests._messaging_fakes import add_row, capture_ws, get_rows, no_retry_sleep, reset_channel_rows  # noqa: E402,F401


class FakeSocket:
    def __init__(self, *, connect_error=None, hang=False, disconnect_error=None):
        self.socket_mode_request_listeners = []
        self.connect_error = connect_error
        self.hang = hang
        self.disconnect_error = disconnect_error
        self.closed = False
        self.disconnected = False

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        if self.hang:
            await asyncio.sleep(60)

    async def disconnect(self):
        self.disconnected = True
        if self.disconnect_error:
            raise self.disconnect_error

    async def close(self):
        self.closed = True
        raise RuntimeError("close is best-effort")


def _web_factory(*, auth=None, auth_error=None, open_result=None, open_error=None):
    made = []

    class Web:
        def __init__(self, token):
            self.token = token
            self.chat_postMessage = AsyncMock()
            made.append(self)

        async def auth_test(self):
            if auth_error:
                raise auth_error
            return auth or {"user_id": "UBOT", "team": "Acme"}

        async def apps_connections_open(self):
            if open_error:
                raise open_error
            return open_result if open_result is not None else {"ok": True, "url": "wss://x"}
    return Web, made


@pytest.fixture
def quiet_db(monkeypatch):
    touched, persisted = [], []

    async def touch(user_id, channel):
        touched.append((user_id, channel))

    async def persist(user_id, channel, chat_id):
        persisted.append((user_id, channel, chat_id))
    monkeypatch.setattr(scm, "touch_last_message", touch)
    monkeypatch.setattr(scm, "persist_chat_id", persist)
    return SimpleNamespace(touched=touched, persisted=persisted)


@pytest.fixture
def owned(quiet_db, monkeypatch):
    ws = capture_ws(monkeypatch)
    ch = scm.SlackChannel()
    planner = AsyncMock(return_value="reply <action>{}</action>")
    ch.set_planner_callback(planner)
    socket = FakeSocket()
    web = SimpleNamespace(chat_postMessage=AsyncMock())
    bot = scm._UserBot("local", socket, web, "D-owner", "UBOT", "Acme")
    ch._bots["local"] = bot
    ch._register_handlers(socket, "local", "UBOT")
    listener = socket.socket_mode_request_listeners[0]
    client = SimpleNamespace(send_socket_mode_response=AsyncMock())

    async def deliver(event=None, *, req_type="events_api", payload=None):
        req = SimpleNamespace(type=req_type, envelope_id="env-1",
                              payload=payload if payload is not None else {"event": event})
        await listener(client, req)

    def dm(channel="D-owner", user="U7", text="hi", **extra):
        return {"type": "message", "channel_type": "im", "channel": channel,
                "user": user, "text": text, **extra}
    return SimpleNamespace(ch=ch, bot=bot, web=web, planner=planner, deliver=deliver, dm=dm,
                           client=client, db=quiet_db, ws=ws)


@pytest_asyncio.fixture
async def sl_rows():
    await reset_channel_rows("slack")
    yield
    await reset_channel_rows("slack")


# ── _UserBot ─────────────────────────────────────────────────────────────────

class TestUserBot:
    @pytest.mark.asyncio
    async def test_start_connects(self):
        s = FakeSocket()
        await scm._UserBot("u", s, None, None, "U", "T").start()
        assert not s.disconnected

    @pytest.mark.asyncio
    async def test_a_failed_connect_tears_down_and_reraises(self):
        s = FakeSocket(connect_error=ConnectionError("refused"))
        with pytest.raises(ConnectionError):
            await scm._UserBot("u", s, None, None, "U", "T").start()
        assert s.disconnected and s.closed

    @pytest.mark.asyncio
    async def test_a_hanging_connect_is_bounded(self, monkeypatch):
        monkeypatch.setattr(scm, "SOCKET_CONNECT_TIMEOUT_S", 0.01)
        s = FakeSocket(hang=True)
        with pytest.raises(asyncio.TimeoutError):
            await scm._UserBot("u", s, None, None, "U", "T").start()
        assert s.disconnected

    @pytest.mark.asyncio
    async def test_stop_swallows_errors(self):
        s = FakeSocket(disconnect_error=RuntimeError("gone"))
        await scm._UserBot("u", s, None, "D1", "U", "T").stop()
        assert s.closed

    def test_a_saved_channel_needs_no_code(self):
        assert scm._UserBot("u", None, None, "D1", "U", "T").pair_code is None
        assert scm._UserBot("u", None, None, None, "U", "T").pair_code


# ── start / stop / status ────────────────────────────────────────────────────

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_boot_needs_both_tokens_and_isolates_failures(self, sl_rows, monkeypatch):
        from backend.core.crypto import encrypt
        await add_row("slack", user_id="a", is_active=True, bot_token_encrypted=encrypt("xoxb-a"),
                      api_key_encrypted=encrypt("xapp-a"), chat_id="D1")
        await add_row("slack", user_id="b", is_active=True, bot_token_encrypted=encrypt("xoxb-b"),
                      api_key_encrypted=encrypt("xapp-b"))
        await add_row("slack", user_id="c", is_active=True, bot_token_encrypted=encrypt("xoxb-c"))
        ch = scm.SlackChannel()
        spun = []

        async def spin(user_id, bot_token, app_token, chat_id):
            spun.append((user_id, bot_token, app_token, chat_id))
            if user_id == "a":
                raise RuntimeError("socket down")
        monkeypatch.setattr(ch, "_spin_up_bot", spin)
        await ch.start()
        assert sorted(spun) == [("a", "xoxb-a", "xapp-a", "D1"), ("b", "xoxb-b", "xapp-b", None)]

    @pytest.mark.asyncio
    async def test_without_slack_sdk(self, monkeypatch):
        monkeypatch.setattr(scm, "_SLACK_AVAILABLE", False)
        monkeypatch.setattr(scm, "_SLACK_IMPORT_ERROR", "missing")
        ch = scm.SlackChannel()
        await ch.start()
        assert ch.status("u") == {"connected": False, "installed": False, "error": "missing"}
        with pytest.raises(ValueError, match="slack-sdk not installed"):
            await ch.configure("u", "xoxb-1", "xapp-1")

    @pytest.mark.asyncio
    async def test_stop(self):
        ch = scm.SlackChannel()
        a, b = FakeSocket(), FakeSocket()
        ch._bots = {"a": scm._UserBot("a", a, None, "D", "U", "T"),
                    "b": scm._UserBot("b", b, None, "D", "U", "T")}
        await ch.stop()
        assert a.disconnected and b.disconnected and not ch._bots

    def test_status_shapes(self):
        ch = scm.SlackChannel()
        assert ch.status("u") == {"connected": False, "installed": True, "awaiting_dm": False,
                                  "bot_user_id": None, "team_name": None, "chat_id": None,
                                  "pair_code": None}
        ch._bots["u"] = scm._UserBot("u", None, None, None, "UBOT", "Acme")
        st = ch.status("u")
        assert st["awaiting_dm"] and not st["connected"] and st["pair_code"]
        ch._bots["u"] = scm._UserBot("u", None, None, "D1", "UBOT", "Acme")
        st = ch.status("u")
        assert st["connected"] and st["chat_id"] == "D1" and st["pair_code"] is None
        assert st["team_name"] == "Acme"


# ── send ─────────────────────────────────────────────────────────────────────

class TestSend:
    @pytest.mark.asyncio
    async def test_not_sendable(self, quiet_db):
        ch = scm.SlackChannel()
        p = NotificationPayload(event=NotificationEvent.JOB_FAILED, title="t", body="b")
        assert await ch.send("u", p) is False
        ch._bots["u"] = scm._UserBot("u", None, None, None, "U", "T")
        assert await ch.send("u", p) is False
        assert await ch.is_configured("u") is False

    @pytest.mark.asyncio
    async def test_send_posts_to_the_owner_dm(self, owned):
        assert await owned.ch.is_configured("local")
        p = NotificationPayload(event=NotificationEvent.JOB_FAILED, title="Oops", body="it broke")
        assert await owned.ch.send("local", p) is True
        owned.web.chat_postMessage.assert_awaited_once_with(channel="D-owner", text="*Oops*\nit broke")
        assert owned.db.touched == [("local", "slack")]
        p2 = NotificationPayload(event=NotificationEvent.JOB_FAILED, title="", body="bare")
        await owned.ch.send("local", p2)
        assert owned.web.chat_postMessage.await_args.kwargs["text"] == "bare"

    @pytest.mark.asyncio
    async def test_send_test(self, owned):
        assert await owned.ch.send_test("local") is True
        assert ":tada:" in owned.web.chat_postMessage.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_a_failed_post_is_not_counted(self, owned, no_retry_sleep):
        owned.web.chat_postMessage.side_effect = RuntimeError("ratelimited")
        p = NotificationPayload(event=NotificationEvent.JOB_FAILED, title="", body="b")
        assert await owned.ch.send("local", p) is False
        assert owned.db.touched == []


# ── configure / disconnect ───────────────────────────────────────────────────

class TestConfigure:
    @pytest.mark.asyncio
    async def test_token_prefixes(self):
        ch = scm.SlackChannel()
        with pytest.raises(ValueError, match="xoxb-"):
            await ch.configure("u", "xapp-1", "xapp-2")
        with pytest.raises(ValueError, match="xapp-"):
            await ch.configure("u", " xoxb-1 ", "xoxb-2")
        with pytest.raises(ValueError, match="xoxb-"):
            await ch.configure("u", None, None)

    @pytest.mark.asyncio
    async def test_a_rejected_bot_token(self, monkeypatch):
        Web, _ = _web_factory(auth_error=RuntimeError("invalid_auth"))
        monkeypatch.setattr(scm, "AsyncWebClient", Web)
        with pytest.raises(ValueError, match="Slack rejected bot token: invalid_auth"):
            await scm.SlackChannel().configure("u", "xoxb-1", "xapp-1")

    @pytest.mark.asyncio
    async def test_an_app_token_answer_that_is_not_ok(self, monkeypatch):
        Web, _ = _web_factory(open_result={"ok": False, "error": "not_allowed_token_type"})
        monkeypatch.setattr(scm, "AsyncWebClient", Web)
        ch = scm.SlackChannel()
        running = SimpleNamespace(stop=AsyncMock())
        ch._bots["u"] = running
        with pytest.raises(ValueError, match="not_allowed_token_type"):
            await ch.configure("u", "xoxb-1", "xapp-1")
        running.stop.assert_not_called()

        Web2, _ = _web_factory(open_result={"ok": False})
        monkeypatch.setattr(scm, "AsyncWebClient", Web2)
        with pytest.raises(ValueError, match="rejected"):
            await ch.configure("u", "xoxb-1", "xapp-1")

    @pytest.mark.asyncio
    async def test_connect_then_reconnect_then_switch_app(self, sl_rows, monkeypatch):
        from backend.core.crypto import decrypt_safe
        Web, _ = _web_factory()
        monkeypatch.setattr(scm, "AsyncWebClient", Web)
        ch = scm.SlackChannel()
        spun = []

        async def spin(user_id, bot_token, app_token, chat_id):
            spun.append(chat_id)
            ch._bots[user_id] = scm._UserBot(user_id, FakeSocket(), None, chat_id, "UBOT", "Acme")
        monkeypatch.setattr(ch, "_spin_up_bot", spin)

        out = await ch.configure("cov-s", "xoxb-1", "xapp-1")
        assert out["awaiting_dm"] and out["team_name"] == "Acme" and out["pair_code"]
        (row,) = await get_rows("slack", "cov-s")
        assert decrypt_safe(row.bot_token_encrypted) == "xoxb-1"
        assert decrypt_safe(row.api_key_encrypted) == "xapp-1"

        from backend.database import AsyncSessionLocal
        from backend.models.messaging_config import MessagingConfig
        from sqlalchemy import update
        async with AsyncSessionLocal() as db:
            await db.execute(update(MessagingConfig).where(MessagingConfig.id == row.id).values(chat_id="D9"))
            await db.commit()
        old_socket = ch._bots["cov-s"].socket_client
        out = await ch.configure("cov-s", "xoxb-1", "xapp-2")       # same bot: owner kept
        assert spun == [None, "D9"] and out["connected"] and old_socket.disconnected
        (row,) = await get_rows("slack", "cov-s")
        assert decrypt_safe(row.api_key_encrypted) == "xapp-2"

        await ch.configure("cov-s", "xoxb-OTHER", "xapp-2")          # different bot: pair again
        assert spun[-1] is None
        (row,) = await get_rows("slack", "cov-s")
        assert row.chat_id is None

    @pytest.mark.asyncio
    async def test_a_socket_that_will_not_connect_is_a_value_error(self, sl_rows, monkeypatch):
        Web, _ = _web_factory()
        monkeypatch.setattr(scm, "AsyncWebClient", Web)
        ch = scm.SlackChannel()
        monkeypatch.setattr(ch, "_spin_up_bot", AsyncMock(side_effect=ConnectionError("refused")))
        with pytest.raises(ValueError, match="socket didn't connect: refused"):
            await ch.configure("cov-s", "xoxb-1", "xapp-1")

    @pytest.mark.asyncio
    async def test_a_connect_timeout_names_itself(self, sl_rows, monkeypatch):
        Web, _ = _web_factory()
        monkeypatch.setattr(scm, "AsyncWebClient", Web)
        ch = scm.SlackChannel()
        monkeypatch.setattr(ch, "_spin_up_bot", AsyncMock(side_effect=asyncio.TimeoutError()))
        with pytest.raises(ValueError, match="socket didn't connect: TimeoutError"):
            await ch.configure("cov-s", "xoxb-1", "xapp-1")

    @pytest.mark.asyncio
    async def test_disconnect(self, sl_rows):
        from backend.core.crypto import encrypt
        await add_row("slack", user_id="cov-s", is_active=True, bot_token_encrypted=encrypt("xoxb-1"),
                      api_key_encrypted=encrypt("xapp-1"), chat_id="D1")
        ch = scm.SlackChannel()
        s = FakeSocket()
        ch._bots["cov-s"] = scm._UserBot("cov-s", s, None, "D1", "U", "T")
        assert await ch.disconnect("cov-s") is True
        assert s.disconnected and "cov-s" not in ch._bots
        (row,) = await get_rows("slack", "cov-s")
        assert not row.is_active and row.bot_token_encrypted is None and row.api_key_encrypted is None
        assert await ch.disconnect("cov-s") is True


class TestSpinUp:
    @pytest.mark.asyncio
    async def test_spin_up_wires_the_socket_listener(self, monkeypatch):
        Web, made = _web_factory(auth={"user_id": None, "team": None})
        sockets = []

        def fake_socket(app_token, web_client):
            s = FakeSocket()
            s.app_token, s.web_client = app_token, web_client
            sockets.append(s)
            return s
        monkeypatch.setattr(scm, "AsyncWebClient", Web)
        monkeypatch.setattr(scm, "SocketModeClient", fake_socket)
        ch = scm.SlackChannel()
        await ch._spin_up_bot("u", bot_token="xoxb-1", app_token="xapp-1", chat_id="D1")
        (s,) = sockets
        assert s.app_token == "xapp-1" and s.web_client is made[0]
        assert len(s.socket_mode_request_listeners) == 1
        bot = ch._bots["u"]
        assert bot.bot_user_id == "" and bot.team_name == "" and bot.chat_id == "D1"


# ── Socket Mode listener ─────────────────────────────────────────────────────

class TestListener:
    @pytest.mark.asyncio
    async def test_every_request_is_acked_first(self, owned):
        await owned.deliver(req_type="hello", payload={})
        owned.client.send_socket_mode_response.assert_awaited_once()
        resp = owned.client.send_socket_mode_response.await_args.args[0]
        assert resp.envelope_id == "env-1"
        owned.planner.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_ack_drops_the_event(self, owned):
        owned.client.send_socket_mode_response.side_effect = RuntimeError("socket closed")
        await owned.deliver(owned.dm())
        owned.planner.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event", [
        {"type": "reaction_added"},
        {"type": "message", "channel_type": "channel", "channel": "C1", "user": "U7", "text": "hi"},
        {"type": "message", "channel_type": "im", "channel": "D-owner", "user": "U7", "text": "x", "bot_id": "B1"},
        {"type": "message", "channel_type": "im", "channel": "D-owner", "user": "U7", "text": "x",
         "subtype": "message_changed"},
        {"type": "message", "channel_type": "im", "channel": "D-owner", "user": "UBOT", "text": "echo"},
        {"type": "message", "channel_type": "im", "channel": "D-owner", "user": "U7", "text": ""},
        {"type": "message", "channel_type": "im", "channel": "", "user": "U7", "text": "hi"},
    ])
    async def test_non_owner_dm_events_are_ignored(self, owned, event):
        await owned.deliver(event)
        owned.planner.assert_not_called()
        owned.web.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_payload_at_all(self, owned):
        await owned.deliver(req_type="events_api", payload={})
        owned.planner.assert_not_called()

    @pytest.mark.asyncio
    async def test_after_disconnect(self, owned):
        owned.ch._bots.clear()
        await owned.deliver(owned.dm())
        owned.planner.assert_not_called()

    @pytest.mark.asyncio
    async def test_pairing(self, owned):
        owned.bot.chat_id, owned.bot.pair_code = None, "ABC234"
        await owned.deliver(owned.dm(channel="D-new", text="wrong"))
        owned.web.chat_postMessage.assert_awaited_with(channel="D-new", text=UNPAIRED_REPLY)
        await owned.deliver(owned.dm(channel="D-new", text="abc234"))
        assert owned.bot.chat_id == "D-new" and owned.bot.pair_code is None
        assert owned.db.persisted == [("local", "slack", "D-new")]
        assert owned.ws.sent[0][0] == {"type": "slack_connected", "chat_id": "D-new", "user_id": "local"}
        assert "connected" in owned.web.chat_postMessage.await_args.kwargs["text"]
        owned.planner.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_turn(self, owned):
        await owned.deliver(owned.dm(text="scout cooking"))
        owned.planner.assert_awaited_once_with("scout cooking", "local")
        owned.web.chat_postMessage.assert_awaited_once_with(channel="D-owner", text="reply")
        assert owned.db.touched == [("local", "slack")]

    @pytest.mark.asyncio
    async def test_owner_turn_with_empty_reply(self, owned):
        owned.planner.return_value = "<action>x</action>"
        await owned.deliver(owned.dm())
        assert owned.web.chat_postMessage.await_args.kwargs["text"] == "Done. :white_check_mark:"

    @pytest.mark.asyncio
    async def test_no_planner_yet(self, owned):
        owned.ch._planner_callback = None
        await owned.deliver(owned.dm())
        assert "Planner not ready" in owned.web.chat_postMessage.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_planner_crash(self, owned):
        owned.planner.side_effect = RuntimeError("boom")
        await owned.deliver(owned.dm())
        assert "Something went wrong" in owned.web.chat_postMessage.await_args.kwargs["text"]
        assert owned.db.touched == []
