# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Discord messaging channel.

Uses discord.py (fully async, gateway WebSocket). No webhook, no HTTPS —
a plain asyncio.Task inside the main event loop receives events.

Lifecycle:
  - start():  load every active Discord MessagingConfig row and spin up one client per user
  - send():   DM the configured Discord user
  - stop():   cleanly shut down every running client

Inbound DMs are routed to the PlannerAgent via the callback wired by
MessagingManager.set_planner_callback().

Setup UX:
  1. Create a Discord application + bot at https://discord.com/developers/applications
  2. Copy the bot token, paste into ViralMint → Connect
  3. Add bot to a server via the invite URL we return, OR just start a DM
  4. DM the bot the pairing code shown on the Messaging page — that Discord account
     becomes the owner. Until then the bot runs nothing; afterwards it answers
     only its owner's DMs (it used to bind whoever DMed
     last and run their text as the owner).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional

import httpx
from sqlalchemy import select

from backend.core.crypto import decrypt_safe, encrypt
from backend.core.ws_manager import ws_manager
from backend.database import AsyncSessionLocal
from backend.messaging._shared import (
    UNPAIRED_REPLY,
    deactivate_config,
    matches_pair_code,
    new_pair_code,
    persist_chat_id,
    send_with_retry,
    split_text,
    strip_action_blocks,
    touch_last_message,
)
from backend.messaging.base import (
    MessagingChannel,
    NotificationEvent,
    NotificationPayload,
)
from backend.models.messaging_config import MessagingConfig

logger = logging.getLogger(__name__)

try:
    import discord  # type: ignore
    _DISCORD_AVAILABLE = True
    _DISCORD_IMPORT_ERROR = ""
except ImportError as _e:  # pragma: no cover
    discord = None  # type: ignore
    _DISCORD_AVAILABLE = False
    _DISCORD_IMPORT_ERROR = str(_e)

PlannerCallback = Callable[[str, str], Awaitable[str]]

DISCORD_MSG_LIMIT = 1900


class _UserBot:
    def __init__(
        self,
        user_id: str,
        client,
        chat_id: Optional[int],
        bot_username: str,
        bot_invite_url: str,
    ) -> None:
        self.user_id = user_id
        self.client = client
        self.chat_id = chat_id
        self.bot_username = bot_username
        self.bot_invite_url = bot_invite_url
        self.pair_code: Optional[str] = None if chat_id else new_pair_code()
        self._task: Optional[asyncio.Task] = None

    async def start(self, token: str, ready_timeout: float = 15.0) -> None:
        self._task = asyncio.create_task(self.client.start(token))
        ready_task = asyncio.create_task(self.client.wait_until_ready())
        try:
            done, _ = await asyncio.wait(
                {self._task, ready_task},
                timeout=ready_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready_task in done:
                return
            failure: BaseException = RuntimeError("Discord bot failed to become ready within 15s")
            if self._task in done and self._task.exception():
                failure = self._task.exception()
        finally:
            if not ready_task.done():
                ready_task.cancel()
        # A client that never became ready must not be left running. Nothing
        # tracks it (it never reaches _bots), yet its on_message stays live —
        # it used to connect later and answer DMs nobody could stop.
        await self.stop()
        raise failure

    async def stop(self) -> None:
        try:
            if not self.client.is_closed():
                await self.client.close()
        except Exception as e:
            logger.warning("Discord shutdown error: %s", e)
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()


class DiscordChannel(MessagingChannel):
    channel_name = "discord"

    def __init__(self) -> None:
        self._bots: dict[str, _UserBot] = {}
        self._planner_callback: Optional[PlannerCallback] = None
        # One connect / disconnect / boot start at a time — overlapping ones
        # each started a gateway client and every DM got two replies.
        self._lock = asyncio.Lock()

    def set_planner_callback(self, callback: PlannerCallback) -> None:
        self._planner_callback = callback

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not _DISCORD_AVAILABLE:
            return
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(MessagingConfig).where(
                    MessagingConfig.channel == "discord",
                    MessagingConfig.is_active == True,  # noqa: E712
                )
            )
            configs = result.scalars().all()

        for cfg in configs:
            token = decrypt_safe(cfg.bot_token_encrypted or "")
            if not token:
                logger.warning("Discord config %s has no valid token", cfg.id)
                continue
            try:
                async with self._lock:
                    await self._spin_up_bot(
                        cfg.user_id,
                        token,
                        chat_id=int(cfg.chat_id) if cfg.chat_id else None,
                    )
            except Exception as e:
                logger.warning("Failed to start Discord bot for user=%s: %s", cfg.user_id, e)

    async def stop(self) -> None:
        bots = list(self._bots.values())
        self._bots.clear()
        await asyncio.gather(*(b.stop() for b in bots), return_exceptions=True)

    async def is_configured(self, user_id: str) -> bool:
        bot = self._bots.get(user_id)
        return bool(bot and bot.chat_id and bot.client.is_ready())

    async def send(self, user_id: str, payload: NotificationPayload) -> bool:
        bot = self._bots.get(user_id)
        if not bot or not bot.chat_id or not bot.client.is_ready():
            return False
        try:
            user = bot.client.get_user(bot.chat_id) or await bot.client.fetch_user(bot.chat_id)
        except Exception as e:
            logger.warning("Discord fetch_user failed: %s", e)
            return False
        if not user:
            return False

        text = f"**{payload.title}**\n{payload.body}" if payload.title else payload.body
        chunks = split_text(text, DISCORD_MSG_LIMIT)

        async def _sender(chunk: str) -> None:
            await user.send(chunk)

        ok = await send_with_retry("discord", user_id, chunks, _sender)
        if ok:
            await touch_last_message(user_id, "discord")
        return ok

    # ── Public API used by backend/api/messaging.py ──────────────────────────

    async def configure(self, user_id: str, bot_token: str) -> dict:
        if not _DISCORD_AVAILABLE:
            raise ValueError(
                f"discord.py not installed: {_DISCORD_IMPORT_ERROR}. "
                "Run: pip install -r requirements.txt"
            )

        token = bot_token.strip()
        if not token:
            raise ValueError("bot_token is required")

        # Validate via /users/@me — avoids spinning up the gateway just to check
        async with httpx.AsyncClient(timeout=10) as http_client:
            resp = await http_client.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {token}"},
            )
        if resp.status_code == 401:
            raise ValueError("Invalid Discord bot token.")
        if resp.status_code != 200:
            raise ValueError(f"Discord rejected token ({resp.status_code}): {resp.text[:200]}")

        data = resp.json()
        bot_id = int(data["id"])
        username = data.get("username", "bot")
        discrim = data.get("discriminator", "0")
        bot_username = username if discrim in ("0", None) else f"{username}#{discrim}"
        bot_invite_url = _build_invite_url(bot_id)

        async with self._lock:
            existing = self._bots.pop(user_id, None)
            if existing:
                await existing.stop()

            async with AsyncSessionLocal() as db:
                row = await db.execute(
                    select(MessagingConfig).where(
                        MessagingConfig.user_id == user_id,
                        MessagingConfig.channel == "discord",
                    )
                )
                cfg = row.scalar_one_or_none()
                if cfg:
                    # A different bot must be paired again — the owner binding
                    # belongs to the bot it was made with.
                    if decrypt_safe(cfg.bot_token_encrypted or "") != token:
                        cfg.chat_id = None
                    cfg.bot_token_encrypted = encrypt(token)
                    cfg.is_active = True
                    cfg.connected_at = datetime.utcnow()
                else:
                    cfg = MessagingConfig(
                        user_id=user_id,
                        channel="discord",
                        bot_token_encrypted=encrypt(token),
                        is_active=True,
                        connected_at=datetime.utcnow(),
                    )
                    db.add(cfg)
                await db.commit()
                chat_id = int(cfg.chat_id) if cfg.chat_id else None

            try:
                await self._spin_up_bot(user_id, token, chat_id=chat_id)
            except Exception as e:
                # The token was valid over REST, but the gateway did not come
                # up — a 400 the user can act on, not a raw 500.
                raise ValueError(f"The bot token works, but Discord's gateway didn't connect: {e}")
            st = self.status(user_id)
            st["bot_username"] = st.get("bot_username") or bot_username
            st["bot_invite_url"] = st.get("bot_invite_url") or bot_invite_url
            return st

    async def disconnect(self, user_id: str) -> bool:
        async with self._lock:
            bot = self._bots.pop(user_id, None)
            if bot:
                await bot.stop()
            await deactivate_config(user_id, "discord", clear_tokens=True)
        return True

    async def send_test(self, user_id: str) -> bool:
        return await self.send(
            user_id,
            NotificationPayload(
                event=NotificationEvent.JOB_FAILED,
                title="ViralMint test",
                body="If you can read this, your Discord bot is wired up. 🎉",
            ),
        )

    def status(self, user_id: str) -> dict:
        if not _DISCORD_AVAILABLE:
            return {
                "connected": False,
                "installed": False,
                "error": _DISCORD_IMPORT_ERROR,
            }
        bot = self._bots.get(user_id)
        if not bot:
            return {
                "connected": False,
                "installed": True,
                "awaiting_dm": False,
                "bot_username": None,
                "chat_id": None,
                "bot_invite_url": None,
                "pair_code": None,
            }
        ready = bot.client.is_ready()
        return {
            "connected": bool(bot.chat_id and ready),
            "installed": True,
            "awaiting_dm": ready and not bot.chat_id,
            "bot_username": bot.bot_username,
            "chat_id": str(bot.chat_id) if bot.chat_id else None,
            "bot_invite_url": bot.bot_invite_url,
            "pair_code": bot.pair_code if not bot.chat_id else None,
        }

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _spin_up_bot(
        self, user_id: str, token: str, chat_id: Optional[int]
    ) -> None:
        intents = discord.Intents.default()
        # DM message content is always available — no privileged intent needed
        intents.dm_messages = True

        client = discord.Client(intents=intents)
        user_bot = _UserBot(
            user_id=user_id,
            client=client,
            chat_id=chat_id,
            bot_username="",
            bot_invite_url="",
        )
        self._register_handlers(client, user_id)
        await user_bot.start(token)

        if client.user:
            user_bot.bot_username = str(client.user)
            user_bot.bot_invite_url = _build_invite_url(client.user.id)

        self._bots[user_id] = user_bot
        logger.info("Discord bot ready for user=%s as %s", user_id, user_bot.bot_username)

    def _register_handlers(self, client, user_id: str) -> None:
        @client.event
        async def on_ready():
            logger.info("Discord logged in as %s (user=%s)", client.user, user_id)

        @client.event
        async def on_message(message):
            if message.author.bot or message.author == client.user:
                return
            # Only DMs route to the planner
            if not isinstance(message.channel, discord.DMChannel):
                return

            bot = self._bots.get(user_id)
            if not bot:
                return
            if bot.chat_id != message.author.id:
                if bot.chat_id:
                    return          # someone other than the owner: silence
                if not matches_pair_code(message.content, bot.pair_code):
                    await message.channel.send(UNPAIRED_REPLY)
                    return
                bot.chat_id = message.author.id
                bot.pair_code = None
                await persist_chat_id(user_id, "discord", str(message.author.id))
                await ws_manager.send(
                    {
                        "type": "discord_connected",
                        "chat_id": str(message.author.id),
                        "user_id": user_id,
                    },
                    user_id,
                )
                await message.channel.send("🎬 ViralMint connected! DM me any time.")
                return

            # An attachment-only DM has no text for the agent to act on.
            if not (message.content or "").strip():
                return

            if not self._planner_callback:
                await message.channel.send("Planner not ready yet — try again in a moment.")
                return

            async with message.channel.typing():
                try:
                    reply = await self._planner_callback(message.content, user_id)
                except Exception as e:
                    logger.exception("Discord planner callback failed: %s", e)
                    await message.channel.send("Something went wrong. Check ViralMint for details.")
                    return

            clean = strip_action_blocks(reply) or "Done. ✅"
            chunks = split_text(clean, DISCORD_MSG_LIMIT)

            async def _reply_sender(chunk: str) -> None:
                await message.channel.send(chunk)

            await send_with_retry("discord", user_id, chunks, _reply_sender)
            await touch_last_message(user_id, "discord")


def _build_invite_url(bot_id: int) -> str:
    # permissions=117760 → View Channels, Send Messages, Read Message History
    return (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={bot_id}&scope=bot&permissions=117760"
    )


discord_channel = DiscordChannel()
