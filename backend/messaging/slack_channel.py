# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Slack messaging channel — Socket Mode (no webhooks, no public URL).

Uses slack-sdk's AsyncSocketModeClient to receive events and AsyncWebClient
to post messages. Requires two tokens:
  - xoxb- bot token  (scopes: chat:write, im:history, im:read, im:write, users:read)
  - xapp- app-level token (scope: connections:write)

Both are stored per-user on MessagingConfig:
  - bot_token_encrypted  → xoxb-
  - api_key_encrypted    → xapp-

Inbound direct messages (message.im) are routed to the PlannerAgent via the
callback wired by MessagingManager.set_planner_callback().

Ownership: every workspace member can DM an installed app,
and the old handshake re-bound to whoever DMed last and ran their text as the
owner. Now the DM that sends the pairing code shown on the Messaging page becomes the
owner's channel; every other DM is ignored.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional

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
    from slack_sdk.socket_mode.aiohttp import SocketModeClient  # type: ignore
    from slack_sdk.socket_mode.request import SocketModeRequest  # type: ignore
    from slack_sdk.socket_mode.response import SocketModeResponse  # type: ignore
    from slack_sdk.web.async_client import AsyncWebClient  # type: ignore
    _SLACK_AVAILABLE = True
    _SLACK_IMPORT_ERROR = ""
except ImportError as _e:  # pragma: no cover
    SocketModeClient = None  # type: ignore
    SocketModeRequest = None  # type: ignore
    SocketModeResponse = None  # type: ignore
    AsyncWebClient = None  # type: ignore
    _SLACK_AVAILABLE = False
    _SLACK_IMPORT_ERROR = str(_e)

PlannerCallback = Callable[[str, str], Awaitable[str]]

SLACK_MSG_LIMIT = 3500

# slack-sdk's aiohttp SocketModeClient.connect() retries every failure forever
# (log, sleep, try again). With a revoked app token that made the connect
# request never return. The token is now checked first; this bounds the rest.
SOCKET_CONNECT_TIMEOUT_S = 20.0


class _UserBot:
    def __init__(
        self,
        user_id: str,
        socket_client,
        web_client,
        chat_id: Optional[str],
        bot_user_id: str,
        team_name: str,
    ) -> None:
        self.user_id = user_id
        self.socket_client = socket_client
        self.web_client = web_client
        self.chat_id = chat_id          # DM channel id (starts with "D")
        self.bot_user_id = bot_user_id  # Slack user id of the bot (starts with "U"/"B")
        self.team_name = team_name
        self.pair_code: Optional[str] = None if chat_id else new_pair_code()

    async def start(self) -> None:
        try:
            await asyncio.wait_for(self.socket_client.connect(), timeout=SOCKET_CONNECT_TIMEOUT_S)
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        try:
            await self.socket_client.disconnect()
        except Exception as e:
            logger.warning("Slack disconnect error: %s", e)
        try:
            await self.socket_client.close()
        except Exception:
            pass


class SlackChannel(MessagingChannel):
    channel_name = "slack"

    def __init__(self) -> None:
        self._bots: dict[str, _UserBot] = {}
        self._planner_callback: Optional[PlannerCallback] = None
        # One connect / disconnect / boot start at a time — overlapping ones
        # each opened a socket and every DM got two replies.
        self._lock = asyncio.Lock()

    def set_planner_callback(self, callback: PlannerCallback) -> None:
        self._planner_callback = callback

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not _SLACK_AVAILABLE:
            return
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(MessagingConfig).where(
                    MessagingConfig.channel == "slack",
                    MessagingConfig.is_active == True,  # noqa: E712
                )
            )
            configs = result.scalars().all()

        for cfg in configs:
            bot_token = decrypt_safe(cfg.bot_token_encrypted or "")
            app_token = decrypt_safe(cfg.api_key_encrypted or "")
            if not bot_token or not app_token:
                logger.warning("Slack config %s missing token(s)", cfg.id)
                continue
            try:
                async with self._lock:
                    await self._spin_up_bot(
                        cfg.user_id,
                        bot_token=bot_token,
                        app_token=app_token,
                        chat_id=cfg.chat_id or None,
                    )
            except Exception as e:
                logger.warning("Failed to start Slack bot for user=%s: %s", cfg.user_id, e)

    async def stop(self) -> None:
        bots = list(self._bots.values())
        self._bots.clear()
        await asyncio.gather(*(b.stop() for b in bots), return_exceptions=True)

    async def is_configured(self, user_id: str) -> bool:
        bot = self._bots.get(user_id)
        return bool(bot and bot.chat_id)

    async def send(self, user_id: str, payload: NotificationPayload) -> bool:
        bot = self._bots.get(user_id)
        if not bot or not bot.chat_id:
            return False

        text = f"*{payload.title}*\n{payload.body}" if payload.title else payload.body
        chunks = split_text(text, SLACK_MSG_LIMIT)
        channel_id = bot.chat_id

        async def _sender(chunk: str) -> None:
            await bot.web_client.chat_postMessage(channel=channel_id, text=chunk)

        ok = await send_with_retry("slack", user_id, chunks, _sender)
        if ok:
            await touch_last_message(user_id, "slack")
        return ok

    # ── Public API used by backend/api/messaging.py ──────────────────────────

    async def configure(self, user_id: str, bot_token: str, app_token: str) -> dict:
        if not _SLACK_AVAILABLE:
            raise ValueError(
                f"slack-sdk not installed: {_SLACK_IMPORT_ERROR}. "
                "Run: pip install -r requirements.txt"
            )

        bot_token = (bot_token or "").strip()
        app_token = (app_token or "").strip()
        if not bot_token.startswith("xoxb-"):
            raise ValueError("Bot token must start with 'xoxb-'")
        if not app_token.startswith("xapp-"):
            raise ValueError("App-level token must start with 'xapp-'")

        # Validate bot token via auth.test
        web_client = AsyncWebClient(token=bot_token)
        try:
            auth = await web_client.auth_test()
        except Exception as e:
            raise ValueError(f"Slack rejected bot token: {e}")

        # Validate the APP-LEVEL token too, before anything is torn down.
        # auth.test only checks the bot token, and a bad app token made the
        # socket connect retry forever — the request never returned, after
        # the working bot had already been stopped.
        try:
            opened = await AsyncWebClient(token=app_token).apps_connections_open()
            if not opened.get("ok"):
                raise ValueError(opened.get("error") or "rejected")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Slack rejected the app-level token: {e}")

        async with self._lock:
            existing = self._bots.pop(user_id, None)
            if existing:
                await existing.stop()

            async with AsyncSessionLocal() as db:
                row = await db.execute(
                    select(MessagingConfig).where(
                        MessagingConfig.user_id == user_id,
                        MessagingConfig.channel == "slack",
                    )
                )
                cfg = row.scalar_one_or_none()
                if cfg:
                    # A different app must be paired again — the DM channel
                    # belongs to the bot it was made with.
                    if decrypt_safe(cfg.bot_token_encrypted or "") != bot_token:
                        cfg.chat_id = None
                    cfg.bot_token_encrypted = encrypt(bot_token)
                    cfg.api_key_encrypted = encrypt(app_token)
                    cfg.is_active = True
                    cfg.connected_at = datetime.utcnow()
                else:
                    cfg = MessagingConfig(
                        user_id=user_id,
                        channel="slack",
                        bot_token_encrypted=encrypt(bot_token),
                        api_key_encrypted=encrypt(app_token),
                        is_active=True,
                        connected_at=datetime.utcnow(),
                    )
                    db.add(cfg)
                await db.commit()
                chat_id = cfg.chat_id or None

            try:
                await self._spin_up_bot(
                    user_id,
                    bot_token=bot_token,
                    app_token=app_token,
                    chat_id=chat_id,
                )
            except Exception as e:
                raise ValueError(f"Slack's socket didn't connect: {str(e) or type(e).__name__}")
            return self.status(user_id)

    async def disconnect(self, user_id: str) -> bool:
        async with self._lock:
            bot = self._bots.pop(user_id, None)
            if bot:
                await bot.stop()
            await deactivate_config(user_id, "slack", clear_tokens=True)
        return True

    async def send_test(self, user_id: str) -> bool:
        return await self.send(
            user_id,
            NotificationPayload(
                event=NotificationEvent.JOB_FAILED,
                title="ViralMint test",
                body="If you can read this, your Slack bot is wired up. :tada:",
            ),
        )

    def status(self, user_id: str) -> dict:
        if not _SLACK_AVAILABLE:
            return {
                "connected": False,
                "installed": False,
                "error": _SLACK_IMPORT_ERROR,
            }
        bot = self._bots.get(user_id)
        if not bot:
            return {
                "connected": False,
                "installed": True,
                "awaiting_dm": False,
                "bot_user_id": None,
                "team_name": None,
                "chat_id": None,
                "pair_code": None,
            }
        return {
            "connected": bool(bot.chat_id),
            "installed": True,
            "awaiting_dm": not bot.chat_id,
            "bot_user_id": bot.bot_user_id,
            "team_name": bot.team_name,
            "chat_id": bot.chat_id,
            "pair_code": bot.pair_code if not bot.chat_id else None,
        }

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _spin_up_bot(
        self,
        user_id: str,
        bot_token: str,
        app_token: str,
        chat_id: Optional[str],
    ) -> None:
        web_client = AsyncWebClient(token=bot_token)
        auth = await web_client.auth_test()
        bot_user_id = auth.get("user_id") or ""
        team_name = auth.get("team") or ""

        socket_client = SocketModeClient(
            app_token=app_token,
            web_client=web_client,
        )

        user_bot = _UserBot(
            user_id=user_id,
            socket_client=socket_client,
            web_client=web_client,
            chat_id=chat_id,
            bot_user_id=bot_user_id,
            team_name=team_name,
        )
        self._register_handlers(socket_client, user_id, bot_user_id)
        await user_bot.start()

        self._bots[user_id] = user_bot
        logger.info("Slack bot ready for user=%s team=%s", user_id, team_name)

    def _register_handlers(self, socket_client, user_id: str, bot_user_id: str) -> None:
        async def _on_request(client, req: "SocketModeRequest"):
            # ACK first so Slack doesn't retry
            try:
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id)
                )
            except Exception as e:
                logger.warning("Slack ACK failed: %s", e)
                return

            if req.type != "events_api":
                return
            event = (req.payload or {}).get("event") or {}
            if event.get("type") != "message":
                return
            # DMs only
            if event.get("channel_type") != "im":
                return
            # Skip bot messages, edits, and our own echoes
            if event.get("bot_id") or event.get("subtype"):
                return
            if event.get("user") == bot_user_id:
                return

            channel_id = event.get("channel")
            text = event.get("text") or ""
            if not channel_id or not text:
                return

            bot = self._bots.get(user_id)
            if not bot:
                return

            # Only the owner's DM channel drives the app. Each person's DM
            # with the bot is its own channel, so the channel id identifies them.
            if bot.chat_id != channel_id:
                if bot.chat_id:
                    return          # another workspace member: silence
                if not matches_pair_code(text, bot.pair_code):
                    await bot.web_client.chat_postMessage(channel=channel_id, text=UNPAIRED_REPLY)
                    return
                bot.chat_id = channel_id
                bot.pair_code = None
                await persist_chat_id(user_id, "slack", channel_id)
                await ws_manager.send(
                    {
                        "type": "slack_connected",
                        "chat_id": channel_id,
                        "user_id": user_id,
                    },
                    user_id,
                )
                await bot.web_client.chat_postMessage(
                    channel=channel_id, text=":clapper: ViralMint connected! DM me any time.")
                return

            if not self._planner_callback:
                await bot.web_client.chat_postMessage(
                    channel=channel_id,
                    text="Planner not ready yet — try again in a moment.",
                )
                return

            try:
                reply = await self._planner_callback(text, user_id)
            except Exception as e:
                logger.exception("Slack planner callback failed: %s", e)
                await bot.web_client.chat_postMessage(
                    channel=channel_id,
                    text="Something went wrong. Check ViralMint for details.",
                )
                return

            clean = strip_action_blocks(reply) or "Done. :white_check_mark:"
            chunks = split_text(clean, SLACK_MSG_LIMIT)

            async def _reply_sender(chunk: str) -> None:
                await bot.web_client.chat_postMessage(channel=channel_id, text=chunk)

            await send_with_retry("slack", user_id, chunks, _reply_sender)
            await touch_last_message(user_id, "slack")

        socket_client.socket_mode_request_listeners.append(_on_request)


slack_channel = SlackChannel()
