# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""backend/messaging/discord_channel.py — boot, connect (REST token check +
gateway start), disconnect, status, sends and the DM handler, against fakes.

discord.py's gateway client and the REST probe (httpx) are replaced wherever
a real one would be built.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

discord = pytest.importorskip("discord")

from backend.messaging import discord_channel as dcm  # noqa: E402
from backend.messaging.base import NotificationEvent, NotificationPayload  # noqa: E402
from tests._messaging_fakes import add_row, capture_ws, get_rows, no_retry_sleep, reset_channel_rows  # noqa: E402,F401


class FakeClient:
    """Enough of discord.Client for _UserBot + the handlers."""

    def __init__(self, *, ready=True, user=None, fail_start=None, never_ready=False):
        self.handlers = {}
        self.user = user
        self._ready = ready
        self._closed = False
        self.fail_start = fail_start
        self.never_ready = never_ready
        self.close_error = None
        self.users = {}
        self.fetch_user = AsyncMock(side_effect=self._fetch)

    def event(self, fn):
        self.handlers[fn.__name__] = fn
        return fn

    async def start(self, token):
        if self.fail_start:
            raise self.fail_start
        self._stopped = asyncio.Event()
        await self._stopped.wait()         # a gateway runs until closed

    async def wait_until_ready(self):
        if self.never_ready or self.fail_start:
            await asyncio.sleep(60)

    def is_ready(self):
        return self._ready

    def is_closed(self):
        return self._closed

    async def close(self):
        if self.close_error:
            raise self.close_error
        self._closed = True
        if getattr(self, "_stopped", None):
            self._stopped.set()

    def get_user(self, uid):
        return self.users.get(uid)

    async def _fetch(self, uid):
        return self.users.get(uid)


def _dm(author_id, content, *, bot=False):
    channel = MagicMock(spec=discord.DMChannel)
    channel.send = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    channel.typing = MagicMock(return_value=cm)
    return SimpleNamespace(author=SimpleNamespace(id=author_id, bot=bot), content=content, channel=channel)


@pytest.fixture
def quiet_db(monkeypatch):
    touched, persisted = [], []

    async def touch(user_id, channel):
        touched.append((user_id, channel))

    async def persist(user_id, channel, chat_id):
        persisted.append((user_id, channel, chat_id))
    monkeypatch.setattr(dcm, "touch_last_message", touch)
    monkeypatch.setattr(dcm, "persist_chat_id", persist)
    return SimpleNamespace(touched=touched, persisted=persisted)


@pytest.fixture
def owned(quiet_db, monkeypatch):
    ws = capture_ws(monkeypatch)
    ch = dcm.DiscordChannel()
    planner = AsyncMock(return_value="reply <action>{}</action>")
    ch.set_planner_callback(planner)
    client = FakeClient(user=SimpleNamespace(id=1, bot=False))
    bot = dcm._UserBot("local", client, 7, "vm", "https://invite")
    ch._bots["local"] = bot
    ch._register_handlers(client, "local")
    return SimpleNamespace(ch=ch, bot=bot, client=client, planner=planner,
                           on_message=client.handlers["on_message"], db=quiet_db, ws=ws)


@pytest_asyncio.fixture
async def dc_rows():
    await reset_channel_rows("discord")
    yield
    await reset_channel_rows("discord")


# ── _UserBot ─────────────────────────────────────────────────────────────────

class TestUserBot:
    @pytest.mark.asyncio
    async def test_start_returns_once_ready_and_stop_closes(self):
        client = FakeClient()
        ub = dcm._UserBot("u", client, None, "", "")
        await ub.start("tok", ready_timeout=1)
        assert not ub._task.done()
        await ub.stop()
        assert client.is_closed()
        await asyncio.sleep(0)
        assert ub._task.done()

    @pytest.mark.asyncio
    async def test_a_login_failure_surfaces_its_own_error(self):
        client = FakeClient(fail_start=discord.LoginFailure("Improper token"))
        ub = dcm._UserBot("u", client, None, "", "")
        with pytest.raises(discord.LoginFailure):
            await ub.start("tok", ready_timeout=1)
        assert client.is_closed()

    @pytest.mark.asyncio
    async def test_stop_survives_a_close_error_and_a_stuck_task(self, monkeypatch):
        client = FakeClient()
        client.close_error = RuntimeError("socket gone")
        ub = dcm._UserBot("u", client, None, "", "")
        ub._task = asyncio.create_task(asyncio.sleep(60))

        real_wait_for = asyncio.wait_for

        async def fast_wait_for(aw, timeout):
            return await real_wait_for(aw, timeout=0.01)
        monkeypatch.setattr(dcm.asyncio, "wait_for", fast_wait_for)
        await ub.stop()
        await asyncio.sleep(0)
        assert ub._task.cancelled()


# ── start / stop / status ────────────────────────────────────────────────────

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_boot_starts_valid_rows_and_isolates_failures(self, dc_rows, monkeypatch):
        from backend.core.crypto import encrypt
        await add_row("discord", user_id="a", is_active=True, bot_token_encrypted=encrypt("ta"), chat_id="77")
        await add_row("discord", user_id="b", is_active=True, bot_token_encrypted=encrypt("tb"))
        await add_row("discord", user_id="c", is_active=True, bot_token_encrypted=None)
        ch = dcm.DiscordChannel()
        spun = []

        async def spin(user_id, token, chat_id):
            spun.append((user_id, token, chat_id))
            if user_id == "a":
                raise RuntimeError("gateway down")
        monkeypatch.setattr(ch, "_spin_up_bot", spin)
        await ch.start()
        assert sorted(spun) == [("a", "ta", 77), ("b", "tb", None)]

    @pytest.mark.asyncio
    async def test_without_discord_py_nothing_starts(self, monkeypatch):
        monkeypatch.setattr(dcm, "_DISCORD_AVAILABLE", False)
        monkeypatch.setattr(dcm, "_DISCORD_IMPORT_ERROR", "No module named 'discord'")
        ch = dcm.DiscordChannel()
        await ch.start()
        assert ch.status("u") == {"connected": False, "installed": False,
                                  "error": "No module named 'discord'"}
        with pytest.raises(ValueError, match="discord.py not installed"):
            await ch.configure("u", "tok")

    @pytest.mark.asyncio
    async def test_stop_stops_every_bot(self):
        ch = dcm.DiscordChannel()
        a, b = FakeClient(), FakeClient()
        ch._bots = {"a": dcm._UserBot("a", a, 1, "", ""), "b": dcm._UserBot("b", b, 2, "", "")}
        await ch.stop()
        assert a.is_closed() and b.is_closed() and not ch._bots

    def test_status_shapes(self):
        ch = dcm.DiscordChannel()
        assert ch.status("u") == {"connected": False, "installed": True, "awaiting_dm": False,
                                  "bot_username": None, "chat_id": None,
                                  "bot_invite_url": None, "pair_code": None}
        unpaired = dcm._UserBot("u", FakeClient(), None, "vm", "inv")
        ch._bots["u"] = unpaired
        st = ch.status("u")
        assert st["awaiting_dm"] and not st["connected"] and st["pair_code"] == unpaired.pair_code
        ch._bots["u"] = dcm._UserBot("u", FakeClient(ready=False), 7, "vm", "inv")
        st = ch.status("u")
        assert not st["connected"] and not st["awaiting_dm"] and st["chat_id"] == "7"
        assert st["pair_code"] is None
        ch._bots["u"].client._ready = True
        assert ch.status("u")["connected"] is True


# ── send ─────────────────────────────────────────────────────────────────────

class TestSend:
    @pytest.mark.asyncio
    async def test_not_sendable(self, owned):
        p = NotificationPayload(event=NotificationEvent.JOB_FAILED, title="t", body="b")
        assert await owned.ch.send("nobody", p) is False
        owned.client._ready = False
        assert await owned.ch.is_configured("local") is False
        assert await owned.ch.send("local", p) is False
        owned.client._ready = True
        owned.client.fetch_user = AsyncMock(side_effect=RuntimeError("404"))
        assert await owned.ch.send("local", p) is False
        owned.client.fetch_user = AsyncMock(return_value=None)
        assert await owned.ch.send("local", p) is False

    @pytest.mark.asyncio
    async def test_dm_the_owner_in_chunks(self, owned, monkeypatch):
        monkeypatch.setattr(dcm, "DISCORD_MSG_LIMIT", 14)
        user = SimpleNamespace(send=AsyncMock())
        owned.client.users[7] = user
        assert await owned.ch.is_configured("local") is True
        p = NotificationPayload(event=NotificationEvent.VIDEO_GENERATED, title="Done", body="aaaa\n\nbbbbbbbb")
        assert await owned.ch.send("local", p) is True
        sent = [c.args[0] for c in user.send.await_args_list]
        assert sent[0].startswith("**Done**") and len(sent) == 2
        assert owned.db.touched == [("local", "discord")]

    @pytest.mark.asyncio
    async def test_fetch_is_used_when_the_cache_misses(self, owned):
        user = SimpleNamespace(send=AsyncMock())
        owned.client.fetch_user = AsyncMock(return_value=user)
        assert await owned.ch.send_test("local") is True
        owned.client.fetch_user.assert_awaited_once_with(7)
        assert "Discord bot is wired up" in user.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_a_failed_dm_is_not_counted(self, owned, no_retry_sleep):
        owned.client.users[7] = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("403")))
        p = NotificationPayload(event=NotificationEvent.JOB_FAILED, title="", body="b")
        assert await owned.ch.send("local", p) is False
        assert owned.db.touched == []
        assert owned.ws.warnings[0]["constraint"] == "discord_delivery"


# ── configure / disconnect ───────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status, data=None, text=""):
        self.status_code = status
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data


def _fake_http(resp):
    calls = []

    class Http:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers):
            calls.append((url, headers))
            return resp
    return Http, calls


class TestConfigure:
    @pytest.mark.asyncio
    async def test_blank_token(self):
        with pytest.raises(ValueError, match="bot_token is required"):
            await dcm.DiscordChannel().configure("u", "   ")

    @pytest.mark.asyncio
    async def test_a_401_is_an_invalid_token(self, monkeypatch):
        Http, calls = _fake_http(FakeResp(401))
        monkeypatch.setattr(dcm.httpx, "AsyncClient", Http)
        with pytest.raises(ValueError, match="Invalid Discord bot token"):
            await dcm.DiscordChannel().configure("u", " tok ")
        assert calls[0][1] == {"Authorization": "Bot tok"}

    @pytest.mark.asyncio
    async def test_other_http_errors_carry_the_status(self, monkeypatch):
        Http, _ = _fake_http(FakeResp(503, text="upstream down"))
        monkeypatch.setattr(dcm.httpx, "AsyncClient", Http)
        with pytest.raises(ValueError, match=r"\(503\): upstream down"):
            await dcm.DiscordChannel().configure("u", "tok")

    @pytest.mark.asyncio
    async def test_connect_new_then_same_token_keeps_owner_then_new_token_clears(self, dc_rows, monkeypatch):
        from backend.core.crypto import decrypt_safe
        Http, _ = _fake_http(FakeResp(200, {"id": "999", "username": "vm", "discriminator": "1234"}))
        monkeypatch.setattr(dcm.httpx, "AsyncClient", Http)
        ch = dcm.DiscordChannel()
        spun = []

        async def spin(user_id, token, chat_id):
            spun.append(chat_id)
            ch._bots[user_id] = dcm._UserBot(user_id, FakeClient(), chat_id, "", "")
        monkeypatch.setattr(ch, "_spin_up_bot", spin)

        out = await ch.configure("cov-d", "tok-1")
        # The gateway bot had no name yet: the REST answer fills the gaps.
        assert out["bot_username"] == "vm#1234"
        assert out["bot_invite_url"] == dcm._build_invite_url(999)
        assert out["awaiting_dm"] and out["pair_code"]
        (row,) = await get_rows("discord", "cov-d")
        assert decrypt_safe(row.bot_token_encrypted) == "tok-1" and row.is_active

        # The owner pairs; a plain reconnect with the same token keeps them.
        from backend.database import AsyncSessionLocal
        from backend.models.messaging_config import MessagingConfig
        from sqlalchemy import update
        async with AsyncSessionLocal() as db:
            await db.execute(update(MessagingConfig).where(MessagingConfig.id == row.id).values(chat_id="55"))
            await db.commit()
        old = ch._bots["cov-d"]
        await ch.configure("cov-d", "tok-1")
        assert spun == [None, 55] and old.client.is_closed()

        # A different bot must be paired again.
        Http2, _ = _fake_http(FakeResp(200, {"id": "5", "username": "other", "discriminator": "0"}))
        monkeypatch.setattr(dcm.httpx, "AsyncClient", Http2)
        out = await ch.configure("cov-d", "tok-2")
        assert spun[-1] is None and out["bot_username"] == "other"
        (row,) = await get_rows("discord", "cov-d")
        assert row.chat_id is None

    @pytest.mark.asyncio
    async def test_a_gateway_failure_is_a_400_not_a_500(self, dc_rows, monkeypatch):
        Http, _ = _fake_http(FakeResp(200, {"id": "1", "username": "vm"}))
        monkeypatch.setattr(dcm.httpx, "AsyncClient", Http)
        ch = dcm.DiscordChannel()
        monkeypatch.setattr(ch, "_spin_up_bot", AsyncMock(side_effect=RuntimeError("not ready in 15s")))
        with pytest.raises(ValueError, match="gateway didn't connect: not ready"):
            await ch.configure("cov-d", "tok")

    @pytest.mark.asyncio
    async def test_disconnect(self, dc_rows):
        from backend.core.crypto import encrypt
        await add_row("discord", user_id="cov-d", is_active=True, bot_token_encrypted=encrypt("t"), chat_id="7")
        ch = dcm.DiscordChannel()
        client = FakeClient()
        ch._bots["cov-d"] = dcm._UserBot("cov-d", client, 7, "", "")
        assert await ch.disconnect("cov-d") is True
        assert client.is_closed() and "cov-d" not in ch._bots
        (row,) = await get_rows("discord", "cov-d")
        assert not row.is_active and row.bot_token_encrypted is None and row.chat_id is None
        assert await ch.disconnect("cov-d") is True


class TestSpinUp:
    @pytest.mark.asyncio
    async def test_spin_up_names_the_bot_from_the_gateway(self, monkeypatch):
        made = []

        class Me:
            id = 42

            def __str__(self):
                return "vm#0001"

        def fake_client(intents):
            c = FakeClient(user=Me())
            c.intents = intents
            made.append(c)
            return c
        monkeypatch.setattr(dcm.discord, "Client", fake_client)
        ch = dcm.DiscordChannel()
        await ch._spin_up_bot("u", "tok", chat_id=None)
        bot = ch._bots["u"]
        assert made[0].intents.dm_messages is True
        assert bot.bot_username == "vm#0001" and bot.bot_invite_url == dcm._build_invite_url(42)
        assert {"on_ready", "on_message"} <= set(made[0].handlers)
        await made[0].handlers["on_ready"]()
        await ch.stop()


# ── on_message ───────────────────────────────────────────────────────────────

class TestOnMessage:
    @pytest.mark.asyncio
    async def test_bots_itself_and_guild_messages_are_ignored(self, owned):
        await owned.on_message(_dm(7, "hi", bot=True))
        own = _dm(1, "echo")
        own.author = owned.client.user
        await owned.on_message(own)
        guild = SimpleNamespace(author=SimpleNamespace(id=7, bot=False), content="hi",
                                channel=SimpleNamespace(send=AsyncMock()))
        await owned.on_message(guild)
        owned.planner.assert_not_called()
        guild.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_dm_after_disconnect_is_ignored(self, owned):
        owned.ch._bots.clear()
        m = _dm(7, "hi")
        await owned.on_message(m)
        owned.planner.assert_not_called()
        m.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_pairing_announces_itself(self, owned):
        owned.bot.chat_id, owned.bot.pair_code = None, "ABC234"
        m = _dm(9, "abc-234")
        await owned.on_message(m)
        assert owned.bot.chat_id == 9 and owned.bot.pair_code is None
        assert owned.db.persisted == [("local", "discord", "9")]
        (msg, uid), = owned.ws.sent
        assert msg == {"type": "discord_connected", "chat_id": "9", "user_id": "local"}
        assert "connected" in m.channel.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_owner_turn(self, owned):
        m = _dm(7, "scout cooking")
        await owned.on_message(m)
        owned.planner.assert_awaited_once_with("scout cooking", "local")
        m.channel.typing.assert_called_once()
        m.channel.send.assert_awaited_once_with("reply")
        assert owned.db.touched == [("local", "discord")]

    @pytest.mark.asyncio
    async def test_no_planner_yet(self, owned):
        owned.ch._planner_callback = None
        m = _dm(7, "hi")
        await owned.on_message(m)
        assert "Planner not ready" in m.channel.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_a_planner_crash_is_reported(self, owned):
        owned.planner.side_effect = RuntimeError("boom")
        m = _dm(7, "hi")
        await owned.on_message(m)
        assert "Something went wrong" in m.channel.send.await_args.args[0]
        assert owned.db.touched == []

    @pytest.mark.asyncio
    async def test_an_empty_reply_says_done(self, owned):
        owned.planner.return_value = ""
        m = _dm(7, "hi")
        await owned.on_message(m)
        m.channel.send.assert_awaited_once_with("Done. ✅")


def test_invite_url():
    url = dcm._build_invite_url(123)
    assert "client_id=123" in url and "scope=bot" in url and "permissions=117760" in url
