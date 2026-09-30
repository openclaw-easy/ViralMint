# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Messaging bots answer their owner and nobody else (a P0 security fix).

Every channel used to run ANY sender's text through the agent as the owner —
spending their AI API credits, starting downloads and clip cuts, reading their
Library — and re-bound the notification target to whoever wrote last:

  * Telegram: bot usernames are public; /start from anyone re-bound the chat.
  * Discord:  anyone sharing (or inviting the bot to) a server could DM it.
  * Slack:    every workspace member can DM an installed app.
  * WhatsApp: a linked device sees every chat on the phone — any contact or
              group message reached the agent, and the reply went to them.

These drive each channel's real inbound handler with fakes (no network, no
bot tokens) and assert who gets through.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.messaging import _shared
from backend.messaging._shared import UNPAIRED_REPLY, matches_pair_code, new_pair_code


# ── shared pieces ────────────────────────────────────────────────────────────

class TestPairCode:
    def test_code_shape(self):
        code = new_pair_code()
        assert len(code) == _shared.PAIR_CODE_LEN
        assert not set(code) & set("01OIL"), "ambiguous characters in a code read off a screen"

    def test_matching_is_forgiving_about_format_not_content(self):
        code = "K7Q4TZ"
        for ok in ("K7Q4TZ", "k7q4tz", "K7Q-4TZ", "/start K7Q4TZ", "my code: K7Q4TZ", " K7Q 4TZ "):
            assert matches_pair_code(ok, code), ok
        for bad in ("K7Q4T", "K7Q4TZX", "hello", "", None, "download https://x/K7Q4TZ/v"):
            assert not matches_pair_code(bad, code), bad
        assert not matches_pair_code("K7Q4TZ", None)
        assert not matches_pair_code("K7Q4TZ", "")


@pytest.mark.asyncio
async def test_send_with_retry_honours_a_rate_limit(monkeypatch):
    """A 429 says how long to wait; a flat half-second retry trips it again."""
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(_shared.asyncio, "sleep", fake_sleep)

    class RateLimited(Exception):
        retry_after = 7

    calls = []

    async def send(chunk):
        calls.append(chunk)
        if len(calls) == 1:
            raise RateLimited()

    assert await _shared.send_with_retry("telegram", "u", ["hi"], send)
    assert slept == [7.0] and len(calls) == 2


# ── fixtures shared by the channel tests ─────────────────────────────────────

@pytest.fixture
def wiring(monkeypatch):
    """Capture persistence + WS events, and give each channel a planner."""
    persisted, events = [], []

    async def fake_persist(user_id, channel, chat_id):
        persisted.append((channel, str(chat_id)))

    async def fake_ws_send(msg, user_id=None):
        events.append(msg.get("type"))

    async def noop(*a, **k):
        return None

    for mod in ("telegram_channel", "discord_channel", "slack_channel"):
        monkeypatch.setattr(f"backend.messaging.{mod}.persist_chat_id", fake_persist)
        monkeypatch.setattr(f"backend.messaging.{mod}.touch_last_message", noop)
    monkeypatch.setattr("backend.messaging.whatsapp_channel.touch_last_message", noop)
    monkeypatch.setattr("backend.core.ws_manager.ws_manager.send", fake_ws_send)
    planner = AsyncMock(return_value="agent reply")
    return SimpleNamespace(persisted=persisted, events=events, planner=planner)


# ── Telegram ─────────────────────────────────────────────────────────────────

def _tg_update(chat_id, text, chat_type="private"):
    msg = SimpleNamespace(text=text, chat_id=chat_id, reply_text=AsyncMock())
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_message=msg, callback_query=None,
    )


@pytest.fixture
def tg(wiring):
    from backend.messaging.telegram_channel import TelegramChannel, _UserBot
    ch = TelegramChannel()
    ch.set_planner_callback(wiring.planner)
    bot = _UserBot(user_id="local", app=MagicMock(), chat_id=None, bot_username="vm_bot")
    ch._bots["local"] = bot
    ctx = SimpleNamespace(args=[], bot=SimpleNamespace(send_chat_action=AsyncMock()))
    return SimpleNamespace(ch=ch, bot=bot, ctx=ctx, text=ch._make_text_handler("local"),
                           start=ch._make_start_handler("local"),
                           status=ch._make_status_handler("local"), w=wiring)


class TestTelegram:
    @pytest.mark.asyncio
    async def test_a_stranger_runs_nothing_before_pairing(self, tg):
        up = _tg_update(111, "download https://evil/x and cut clips")
        await tg.text(up, tg.ctx)
        tg.w.planner.assert_not_called()
        up.effective_message.reply_text.assert_awaited_once_with(UNPAIRED_REPLY)
        assert tg.bot.chat_id is None and not tg.w.persisted

    @pytest.mark.asyncio
    async def test_a_bare_start_does_not_pair(self, tg):
        await tg.start(_tg_update(111, "/start"), tg.ctx)
        assert tg.bot.chat_id is None and not tg.w.persisted
        assert "telegram_connected" not in tg.w.events

    @pytest.mark.asyncio
    async def test_the_deep_link_code_pairs_and_then_strangers_are_ignored(self, tg):
        code = tg.bot.pair_code
        tg.ctx.args = [code]                         # t.me/<bot>?start=<code>
        await tg.start(_tg_update(222, f"/start {code}"), tg.ctx)
        assert tg.bot.chat_id == 222 and tg.bot.pair_code is None
        assert tg.w.persisted == [("telegram", "222")]
        assert "telegram_connected" in tg.w.events

        # Another chat — even one that learned the old code — gets silence.
        tg.ctx.args = [code]
        intruder = _tg_update(333, f"/start {code}")
        await tg.start(intruder, tg.ctx)
        await tg.text(_tg_update(333, "download https://evil/x"), tg.ctx)
        tg.w.planner.assert_not_called()
        intruder.effective_message.reply_text.assert_not_called()
        assert tg.bot.chat_id == 222, "a later /start re-bound the notifications"

        # The owner drives the agent.
        owner = _tg_update(222, "scout cooking videos")
        await tg.text(owner, tg.ctx)
        tg.w.planner.assert_awaited_once_with("scout cooking videos", "local")

    @pytest.mark.asyncio
    async def test_the_code_typed_as_a_message_pairs_too(self, tg):
        await tg.text(_tg_update(222, tg.bot.pair_code.lower()), tg.ctx)
        assert tg.bot.chat_id == 222
        tg.w.planner.assert_not_called()             # pairing is not an agent turn

    @pytest.mark.asyncio
    async def test_a_group_chat_can_never_pair(self, tg):
        await tg.text(_tg_update(-100, tg.bot.pair_code, chat_type="group"), tg.ctx)
        assert tg.bot.chat_id is None

    @pytest.mark.asyncio
    async def test_status_is_owner_only(self, tg, monkeypatch):
        tg.bot.chat_id, tg.bot.pair_code = 222, None
        summary = AsyncMock(return_value="Recent jobs: secret title")
        monkeypatch.setattr(tg.ch, "_recent_jobs_summary", summary)
        stranger = _tg_update(333, "/status")
        await tg.status(stranger, tg.ctx)
        summary.assert_not_called()
        stranger.effective_message.reply_text.assert_not_called()
        owner = _tg_update(222, "/status")
        await tg.status(owner, tg.ctx)
        owner.effective_message.reply_text.assert_awaited_once()

    def test_status_exposes_the_code_and_a_one_tap_link_while_unpaired(self, tg):
        st = tg.ch.status("local")
        assert st["awaiting_start"] and st["pair_code"] == tg.bot.pair_code
        assert st["bot_url"] == f"https://t.me/vm_bot?start={tg.bot.pair_code}"
        tg.bot.chat_id, tg.bot.pair_code = 222, None
        st = tg.ch.status("local")
        assert st["connected"] and st["pair_code"] is None and st["bot_url"] == "https://t.me/vm_bot"


@pytest.mark.asyncio
async def test_telegram_switching_bots_clears_the_owner(monkeypatch):
    """The saved chat belongs to the old bot; keeping it said "connected"
    while the new bot could message nobody."""
    import asyncio as _a
    from backend.core.crypto import encrypt
    from backend.database import AsyncSessionLocal, init_db
    from backend.messaging import telegram_channel as tc
    from backend.models.messaging_config import MessagingConfig
    from sqlalchemy import delete, select

    await init_db()
    async with AsyncSessionLocal() as db:
        await db.execute(delete(MessagingConfig).where(MessagingConfig.channel == "telegram"))
        db.add(MessagingConfig(user_id="local", channel="telegram", is_active=True,
                               bot_token_encrypted=encrypt("111:old"), chat_id="222"))
        await db.commit()

    class FakeBot:
        def __init__(self, token): self.token = token
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get_me(self): return SimpleNamespace(username="new_bot")

    spun = []

    async def fake_spin(user_id, token, chat_id):
        spun.append(chat_id)
        ch._bots[user_id] = tc._UserBot(user_id, MagicMock(), chat_id, "new_bot")

    ch = tc.TelegramChannel()
    monkeypatch.setattr(tc, "Bot", FakeBot)
    monkeypatch.setattr(ch, "_spin_up_bot", fake_spin)

    out = await ch.configure("local", "999:new")
    assert spun == [None], "the old bot's chat was carried over to the new bot"
    assert out["awaiting_start"] and out["pair_code"]
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(MessagingConfig).where(
            MessagingConfig.channel == "telegram"))).scalar_one()
        assert row.chat_id is None

    # Same token again (a plain reconnect) keeps the owner.
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(MessagingConfig).where(
            MessagingConfig.channel == "telegram"))).scalar_one()
        row.chat_id = "444"
        await db.commit()
    spun.clear()
    await ch.configure("local", "999:new")
    assert spun == [444]
    async with AsyncSessionLocal() as db:
        await db.execute(delete(MessagingConfig).where(MessagingConfig.channel == "telegram"))
        await db.commit()


# ── Discord ──────────────────────────────────────────────────────────────────

class _FakeDiscordClient:
    def __init__(self):
        self.handlers = {}
        self.user = SimpleNamespace(id=1)

    def event(self, fn):
        self.handlers[fn.__name__] = fn
        return fn


def _dm(author_id, content):
    import discord
    channel = MagicMock(spec=discord.DMChannel)
    channel.send = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    channel.typing = MagicMock(return_value=cm)
    return SimpleNamespace(author=SimpleNamespace(id=author_id, bot=False), content=content, channel=channel)


@pytest.fixture
def dc(wiring):
    pytest.importorskip("discord")
    from backend.messaging.discord_channel import DiscordChannel, _UserBot
    ch = DiscordChannel()
    ch.set_planner_callback(wiring.planner)
    client = _FakeDiscordClient()
    bot = _UserBot("local", client, None, "vm#0", "")
    ch._bots["local"] = bot
    ch._register_handlers(client, "local")
    return SimpleNamespace(ch=ch, bot=bot, on_message=client.handlers["on_message"], w=wiring)


class TestDiscord:
    @pytest.mark.asyncio
    async def test_pairing_then_owner_only(self, dc):
        stranger = _dm(5, "download https://evil/x")
        await dc.on_message(stranger)
        dc.w.planner.assert_not_called()
        stranger.channel.send.assert_awaited_once_with(UNPAIRED_REPLY)

        await dc.on_message(_dm(7, dc.bot.pair_code))
        assert dc.bot.chat_id == 7 and dc.w.persisted == [("discord", "7")]
        dc.w.planner.assert_not_called()

        intruder = _dm(5, "download https://evil/x")
        await dc.on_message(intruder)
        intruder.channel.send.assert_not_called()
        assert dc.bot.chat_id == 7, "a later DM re-bound the notifications"
        dc.w.planner.assert_not_called()

        await dc.on_message(_dm(7, "scout cooking"))
        dc.w.planner.assert_awaited_once_with("scout cooking", "local")

    @pytest.mark.asyncio
    async def test_an_attachment_only_dm_is_not_an_agent_turn(self, dc):
        dc.bot.chat_id, dc.bot.pair_code = 7, None
        await dc.on_message(_dm(7, ""))
        dc.w.planner.assert_not_called()


@pytest.mark.asyncio
async def test_discord_bot_that_never_gets_ready_is_torn_down():
    """It used to keep running untracked and answer DMs later."""
    from backend.messaging.discord_channel import _UserBot

    class NeverReady:
        closed = False
        async def start(self, token): await asyncio.sleep(60)
        async def wait_until_ready(self): await asyncio.sleep(60)
        def is_closed(self): return self.closed
        async def close(self): self.closed = True

    client = NeverReady()
    ub = _UserBot("local", client, None, "", "")
    with pytest.raises(RuntimeError):
        await ub.start("tok", ready_timeout=0.05)
    assert client.closed and ub._task.done()


# ── Slack ────────────────────────────────────────────────────────────────────

@pytest.fixture
def sl(wiring):
    pytest.importorskip("slack_sdk")
    from backend.messaging.slack_channel import SlackChannel, _UserBot
    ch = SlackChannel()
    ch.set_planner_callback(wiring.planner)
    socket = SimpleNamespace(socket_mode_request_listeners=[])
    web = SimpleNamespace(chat_postMessage=AsyncMock())
    bot = _UserBot("local", socket, web, None, "UBOT", "Acme")
    ch._bots["local"] = bot
    ch._register_handlers(socket, "local", "UBOT")
    listener = socket.socket_mode_request_listeners[0]
    client = SimpleNamespace(send_socket_mode_response=AsyncMock())

    async def dm(channel, user, text):
        req = SimpleNamespace(type="events_api", envelope_id="e", payload={"event": {
            "type": "message", "channel_type": "im", "channel": channel, "user": user, "text": text}})
        await listener(client, req)
    return SimpleNamespace(ch=ch, bot=bot, web=web, dm=dm, w=wiring)


class TestSlack:
    @pytest.mark.asyncio
    async def test_pairing_then_owner_only(self, sl):
        await sl.dm("D-stranger", "U5", "download https://evil/x")
        sl.w.planner.assert_not_called()
        sl.web.chat_postMessage.assert_awaited_with(channel="D-stranger", text=UNPAIRED_REPLY)

        await sl.dm("D-owner", "U7", sl.bot.pair_code)
        assert sl.bot.chat_id == "D-owner" and sl.w.persisted == [("slack", "D-owner")]

        sl.web.chat_postMessage.reset_mock()
        await sl.dm("D-stranger", "U5", "download https://evil/x")
        sl.web.chat_postMessage.assert_not_called()
        assert sl.bot.chat_id == "D-owner"
        sl.w.planner.assert_not_called()

        await sl.dm("D-owner", "U7", "scout cooking")
        sl.w.planner.assert_awaited_once_with("scout cooking", "local")


@pytest.mark.asyncio
async def test_slack_rejects_a_bad_app_token_before_touching_the_running_bot(monkeypatch):
    """A bad app token used to hang the connect forever, after the working
    bot had already been stopped."""
    pytest.importorskip("slack_sdk")
    from backend.messaging import slack_channel as sc

    class FakeWeb:
        def __init__(self, token): self.token = token
        async def auth_test(self): return {"user_id": "UBOT", "team": "Acme"}
        async def apps_connections_open(self):
            raise Exception("invalid_auth")

    monkeypatch.setattr(sc, "AsyncWebClient", FakeWeb)
    ch = sc.SlackChannel()
    running = SimpleNamespace(stop=AsyncMock())
    ch._bots["local"] = running
    with pytest.raises(ValueError, match="app-level token"):
        await asyncio.wait_for(ch.configure("local", "xoxb-1", "xapp-bad"), timeout=5)
    running.stop.assert_not_called()
    assert ch._bots["local"] is running


# ── WhatsApp ─────────────────────────────────────────────────────────────────

def _wa_message(text, *, from_me, chat_user, chat_server="s.whatsapp.net", msg_id="m1"):
    chat = SimpleNamespace(User=chat_user, Server=chat_server)
    src = SimpleNamespace(IsFromMe=from_me, Chat=chat, SenderAlt=None,
                          RecipientAlt=None, Sender=None, AddressingMode=None)
    return SimpleNamespace(
        Info=SimpleNamespace(ID=msg_id, MessageSource=src),
        Message=SimpleNamespace(conversation=text, extendedTextMessage=None),
    )


class _FakeEvent:
    def __init__(self):
        self.by_type = {}

    def qr(self, fn):
        self.by_type["qr"] = fn

    def __call__(self, ev_type):
        def deco(fn):
            self.by_type[ev_type] = fn
            return fn
        return deco


@pytest.fixture
def wa(wiring, tmp_path, monkeypatch):
    from backend.messaging import whatsapp_channel as w
    ch = w.WhatsAppChannel()
    ch.set_planner_callback(wiring.planner)
    client = SimpleNamespace(event=_FakeEvent(), send_message=AsyncMock(return_value=SimpleNamespace(ID="out1")))
    uc = w._UserClient("local", client, None, tmp_path / "s.db")
    uc.paired, uc.online, uc.chat_id = True, True, "15550001111"
    uc.self_ids = {"15550001111", "987654321"}          # own phone + own LID
    ch._clients["local"] = uc
    neo = {k: k for k in ("ConnectedEv", "DisconnectedEv", "LoggedOutEv", "PairStatusEv", "MessageEv")}
    neo["build_jid"] = lambda u, s: f"{u}@{s}"
    ch._register_handlers(uc, neo)
    persisted = []

    async def fake_persist(user_id, chat_id):
        persisted.append(chat_id)
    monkeypatch.setattr(ch, "_persist_connection", fake_persist)
    return SimpleNamespace(ch=ch, uc=uc, client=client, on_message=client.event.by_type["MessageEv"],
                           persisted=persisted, w=wiring)


class TestWhatsApp:
    @pytest.mark.asyncio
    async def test_a_contact_cannot_drive_the_agent(self, wa):
        await wa.on_message(wa.client, _wa_message("download https://evil/x", from_me=False, chat_user="15559998888"))
        wa.w.planner.assert_not_called()
        wa.client.send_message.assert_not_called()
        assert wa.uc.chat_id == "15550001111" and not wa.persisted, "a contact re-pointed notifications"

    @pytest.mark.asyncio
    async def test_a_group_cannot_drive_the_agent(self, wa):
        await wa.on_message(wa.client, _wa_message("scout x", from_me=False, chat_user="1203630@", chat_server="g.us"))
        wa.w.planner.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_owner_writing_to_a_friend_is_not_a_command(self, wa):
        """IsFromMe alone is not enough — the owner's chat with a friend is IsFromMe too."""
        await wa.on_message(wa.client, _wa_message("see you at 8", from_me=True, chat_user="15559998888"))
        wa.w.planner.assert_not_called()
        wa.client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_owner_self_chat_drives_the_agent(self, wa):
        await wa.on_message(wa.client, _wa_message("scout cooking", from_me=True, chat_user="15550001111"))
        wa.w.planner.assert_awaited_once_with("scout cooking", "local")
        wa.client.send_message.assert_awaited()

    @pytest.mark.asyncio
    async def test_the_owner_self_chat_by_lid_counts_too(self, wa):
        await wa.on_message(wa.client, _wa_message("scout cooking", from_me=True,
                                                   chat_user="987654321", chat_server="lid"))
        wa.w.planner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_identity_accepts_nothing(self, wa, monkeypatch):
        wa.uc.self_ids = set()

        async def no_learn(uc):
            return None
        monkeypatch.setattr(wa.ch, "_learn_self", no_learn)
        await wa.on_message(wa.client, _wa_message("scout", from_me=True, chat_user="15550001111"))
        wa.w.planner.assert_not_called()

    def test_connection_state_comes_from_events_not_neonize_properties(self, wa):
        """neonize's is_connected returns an un-awaited coroutine — always truthy."""
        assert wa.ch.status("local")["connected"] is True
        wa.uc.online = False
        assert wa.ch.status("local")["connected"] is False

    def test_session_lives_in_the_data_dir(self):
        from backend.config import settings
        from backend.messaging.whatsapp_channel import _session_path
        assert _session_path("local").parent == settings.STORAGE_ROOT / "messaging"


@pytest.mark.asyncio
async def test_telegram_a_group_bound_by_an_old_build_asks_to_pair_again(monkeypatch):
    """Older builds bound /start from ANY chat, groups included (negative
    ids). The owner gate only accepts a private chat and pairing refuses
    while an owner is set — so a saved group id locked the owner out with the
    tile still saying Connected. Such a binding is dropped
    at start-up and the bot shows a pairing code again."""
    from backend.messaging import telegram_channel as tcm

    fake_app = MagicMock()
    fake_app.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="vm_bot"))
    builder = MagicMock()
    builder.token.return_value.build.return_value = fake_app
    monkeypatch.setattr(tcm, "ApplicationBuilder", lambda: builder)
    monkeypatch.setattr(tcm._UserBot, "start_polling", AsyncMock())

    ch = tcm.TelegramChannel()
    await ch._spin_up_bot("local", "123:abc", chat_id=-100123456)
    bot = ch._bots["local"]
    assert bot.chat_id is None and bot.pair_code, "a group id must not stay the owner"
    assert ch.status("local")["awaiting_start"] is True

    await ch._spin_up_bot("local", "123:abc", chat_id=4242)      # a private chat id is kept
    assert ch._bots["local"].chat_id == 4242


def test_telegram_edited_messages_do_not_rerun_an_agent_turn():
    """PTB's TEXT filter matches edited messages too — each edit re-ran a
    agent turn (a real AI call)."""
    from telegram import Update
    from telegram.ext import MessageHandler
    from backend.messaging.telegram_channel import TelegramChannel

    app = MagicMock()
    TelegramChannel()._register_handlers(app, "local")
    text_handlers = [c.args[0] for c in app.add_handler.call_args_list
                     if isinstance(c.args[0], MessageHandler)]
    assert text_handlers
    edited = MagicMock(spec=Update)
    edited.message = None
    edited.edited_message = SimpleNamespace(text="hi", entities=(), caption=None)
    edited.effective_message = edited.edited_message
    assert not text_handlers[0].filters.check_update(edited)
