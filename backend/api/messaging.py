# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""REST /api/messaging — connect/disconnect messaging channels and report status.

All four channels (Telegram, WhatsApp, Discord, Slack) implement the same
MessagingChannel ABC, so this module is deliberately thin:

  - `_get_channel()`   → uniform 500 if the channel isn't registered
  - `_connect_channel` → uniform configure() + error mapping
  - `_disconnect_channel` / `_test_channel` → one-line wrappers

Channels differ only in (1) their connect body schema and (2) the "not
connected yet" hint shown when send_test returns False. Everything else is
shared. Adding a fifth channel should take ~10 lines here, not 60.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.messaging.base import MessagingChannel
from backend.messaging.manager import messaging

logger = logging.getLogger(__name__)
router = APIRouter()

USER_ID = "local"

CHANNELS = ("telegram", "whatsapp", "discord", "slack")

# One-liner shown when send_test returns False for a channel — tells the user
# what they still need to do (pair the bot, scan the QR, etc).
NOT_CONNECTED_HINTS: dict[str, str] = {
    "telegram": "Could not send — link the bot to your account first (the Messaging page).",
    "whatsapp": "Could not send — WhatsApp is not paired or is offline. Scan the QR code first.",
    "discord":  "Could not send — DM your bot the pairing code shown on the Messaging page first.",
    "slack":    "Could not send — DM your bot the pairing code shown on the Messaging page first.",
}


class TelegramConnectBody(BaseModel):
    bot_token: str


class DiscordConnectBody(BaseModel):
    bot_token: str


class SlackConnectBody(BaseModel):
    bot_token: str
    app_token: str


# ── Shared helpers ────────────────────────────────────────────────────────────


def _get_channel(name: str) -> MessagingChannel:
    channel = messaging.get_channel(name)
    if channel is None:
        raise HTTPException(status_code=500, detail=f"{name.title()} channel not registered")
    return channel


async def _connect_channel(name: str, **credentials) -> dict:
    channel = _get_channel(name)
    try:
        result = await channel.configure(USER_ID, **credentials)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("%s connect failed: %s", name.title(), e)
        raise HTTPException(status_code=500, detail=f"Connect failed: {e}")
    return {"ok": True, **result}


async def _disconnect_channel(name: str) -> dict:
    channel = _get_channel(name)
    await channel.disconnect(USER_ID)
    return {"ok": True}


async def _test_channel(name: str) -> dict:
    channel = _get_channel(name)
    sent = await channel.send_test(USER_ID)
    if not sent:
        raise HTTPException(
            status_code=400,
            detail=NOT_CONNECTED_HINTS.get(name, "Could not send — channel not ready."),
        )
    return {"ok": True}


# ── Status (single endpoint, loops every registered channel) ─────────────────


@router.get("/messaging/status")
async def messaging_status():
    """Return connection status for every messaging channel."""
    out: dict[str, dict] = {}
    for name in CHANNELS:
        channel = messaging.get_channel(name)
        if channel is None:
            out[name] = {"connected": False, "installed": False}
        else:
            out[name] = channel.status(USER_ID)
    return out


# ── Telegram ─────────────────────────────────────────────────────────────────


@router.post("/messaging/telegram/connect")
async def telegram_connect(body: TelegramConnectBody):
    token = (body.bot_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="bot_token is required")
    return await _connect_channel("telegram", bot_token=token)


@router.post("/messaging/telegram/disconnect")
async def telegram_disconnect():
    return await _disconnect_channel("telegram")


@router.post("/messaging/telegram/test")
async def telegram_test():
    return await _test_channel("telegram")


# ── WhatsApp ─────────────────────────────────────────────────────────────────


@router.post("/messaging/whatsapp/connect")
async def whatsapp_connect():
    return await _connect_channel("whatsapp")


@router.post("/messaging/whatsapp/disconnect")
async def whatsapp_disconnect():
    return await _disconnect_channel("whatsapp")


@router.post("/messaging/whatsapp/test")
async def whatsapp_test():
    return await _test_channel("whatsapp")


# ── Discord ──────────────────────────────────────────────────────────────────


@router.post("/messaging/discord/connect")
async def discord_connect(body: DiscordConnectBody):
    token = (body.bot_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="bot_token is required")
    return await _connect_channel("discord", bot_token=token)


@router.post("/messaging/discord/disconnect")
async def discord_disconnect():
    return await _disconnect_channel("discord")


@router.post("/messaging/discord/test")
async def discord_test():
    return await _test_channel("discord")


# ── Slack ────────────────────────────────────────────────────────────────────


@router.post("/messaging/slack/connect")
async def slack_connect(body: SlackConnectBody):
    bot_token = (body.bot_token or "").strip()
    app_token = (body.app_token or "").strip()
    if not bot_token or not app_token:
        raise HTTPException(status_code=400, detail="bot_token and app_token are required")
    return await _connect_channel("slack", bot_token=bot_token, app_token=app_token)


@router.post("/messaging/slack/disconnect")
async def slack_disconnect():
    return await _disconnect_channel("slack")


@router.post("/messaging/slack/test")
async def slack_test():
    return await _test_channel("slack")
