# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Shared helpers for all messaging channels.

Consolidates what used to be duplicated four ways across
telegram/whatsapp/discord/slack: the <action> stripper, the paragraph-aware
text splitter, the MessagingConfig load/persist/touch/deactivate helpers,
and a unified reliability wrapper around chunk sends.

The reliability wrapper is the enforcement point for the
"messaging must feel like the web Chat page" bar: when a chunk fails to
leave the bot even after one retry, we surface a constraint_warning to the
web UI so silent drops stop being silent.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from datetime import datetime
from typing import Awaitable, Callable, Optional

from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.messaging_config import MessagingConfig

logger = logging.getLogger(__name__)

# ── Text helpers ─────────────────────────────────────────────────────────────

ACTION_BLOCK = re.compile(r"<action>.*?</action>", re.DOTALL)


def strip_action_blocks(text: str) -> str:
    """Remove planner <action>...</action> blocks and trim."""
    if not text:
        return ""
    return ACTION_BLOCK.sub("", text).strip()


def split_text(text: str, limit: int) -> list[str]:
    """Paragraph-aware splitter that respects a platform's max-message size.

    Splits on double newlines first, hard-chops paragraphs that are themselves
    longer than `limit`. Short text passes through as a single-element list.
    """
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > limit:
            if buf:
                out.append(buf)
            if len(para) > limit:
                for i in range(0, len(para), limit):
                    out.append(para[i:i + limit])
                buf = ""
            else:
                buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        out.append(buf)
    return out


# ── Owner pairing ────────────────────────────────────────────────────────────
#
# A bot token lets ANYONE message the bot: Telegram bot usernames are public
# and searchable, a Discord bot can be DMed by anyone who shares (or invites it
# to) a server, and every Slack workspace member can DM an installed app. The
# old handshake bound whoever wrote first — and re-bound on every message from
# a new sender — then ran their text through the agent as the owner: a
# stranger could spend the owner's AI API credits, start downloads and clip cuts,
# read their Library, and take over where job notifications went
# (a P0 security fix).
#
# Now a freshly connected bot is bound only by a one-time code that the
# desktop app shows on the Messaging page. Until then it runs nothing; after
# that it answers its owner and ignores everyone else.

# No 0/O or 1/I/L — the code is read off a screen and typed on a phone.
_PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PAIR_CODE_LEN = 6

UNPAIRED_REPLY = (
    "This bot is private. To link it, send the pairing code shown in "
    "ViralMint → Messaging."
)


def new_pair_code() -> str:
    return "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(PAIR_CODE_LEN))


def matches_pair_code(text: str | None, code: str | None) -> bool:
    """True when `text` is the pairing code — spacing, dashes and case aside.

    Accepts the code on its own or as the last word ("/start ABC123",
    "code: abc-123"), so a user who pastes it with a little text around it
    still pairs. Constant-time compare on the normalised value.
    """
    if not text or not code:
        return False
    words = re.findall(r"[A-Za-z0-9-]+", text)
    if not words:
        return False
    candidate = words[-1].replace("-", "").upper()
    whole = re.sub(r"[\s-]", "", text).upper()
    return secrets.compare_digest(candidate, code) or secrets.compare_digest(whole, code)


# ── MessagingConfig helpers ──────────────────────────────────────────────────


async def load_config(user_id: str, channel: str) -> Optional[MessagingConfig]:
    """Read the MessagingConfig row for (user_id, channel) or None."""
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            select(MessagingConfig).where(
                MessagingConfig.user_id == user_id,
                MessagingConfig.channel == channel,
            )
        )
        return row.scalar_one_or_none()


async def persist_chat_id(user_id: str, channel: str, chat_id: str) -> None:
    """Save chat_id on an existing MessagingConfig row, stamping connected_at
    the first time and bumping last_message_at."""
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            select(MessagingConfig).where(
                MessagingConfig.user_id == user_id,
                MessagingConfig.channel == channel,
            )
        )
        cfg = row.scalar_one_or_none()
        if cfg:
            cfg.chat_id = str(chat_id)
            cfg.connected_at = cfg.connected_at or datetime.utcnow()
            cfg.last_message_at = datetime.utcnow()
            await db.commit()


async def touch_last_message(user_id: str, channel: str) -> None:
    """Bump last_message_at on the MessagingConfig row."""
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            select(MessagingConfig).where(
                MessagingConfig.user_id == user_id,
                MessagingConfig.channel == channel,
            )
        )
        cfg = row.scalar_one_or_none()
        if cfg:
            cfg.last_message_at = datetime.utcnow()
            await db.commit()


async def deactivate_config(
    user_id: str,
    channel: str,
    clear_tokens: bool = True,
) -> bool:
    """Mark the config inactive and optionally clear stored credentials."""
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            select(MessagingConfig).where(
                MessagingConfig.user_id == user_id,
                MessagingConfig.channel == channel,
            )
        )
        cfg = row.scalar_one_or_none()
        if not cfg:
            return False
        cfg.is_active = False
        cfg.chat_id = None
        if clear_tokens:
            cfg.bot_token_encrypted = None
            cfg.api_key_encrypted = None
            cfg.webhook_url_encrypted = None
        await db.commit()
        return True


# ── Reliability wrapper ──────────────────────────────────────────────────────


SendFn = Callable[[str], Awaitable[object]]


async def send_with_retry(
    channel_name: str,
    user_id: str,
    chunks: list[str],
    send_fn: SendFn,
    *,
    retry_once: bool = True,
    retry_delay: float = 0.5,
) -> bool:
    """Send chunks sequentially with a single retry on failure.

    Returns True only if every chunk was accepted. On final failure we log
    at ERROR and emit a `{channel}_delivery` constraint_warning so the web
    UI surfaces the drop instead of swallowing it.

    The caller owns chunk construction (e.g. split_text) and the send_fn
    closure (e.g. `lambda c: app.bot.send_message(...)`).
    """
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        try:
            await send_fn(chunk)
            continue
        except Exception as first_err:
            if not retry_once:
                await _notify_delivery_failed(channel_name, user_id, first_err, idx, total)
                return False
            # A rate limit says how long to wait (Telegram RetryAfter.retry_after,
            # Slack's Retry-After, discord.py's HTTPException.retry_after); a
            # flat half-second retry just trips it again. Capped so a
            # notification never holds its task for minutes.
            wait = _retry_after(first_err)
            delay = min(max(retry_delay, wait), MAX_RETRY_AFTER_S) if wait else retry_delay
            logger.warning(
                "messaging send failed ch=%s user=%s chunk=%d/%d — retrying in %.1fs: %s",
                channel_name, user_id, idx + 1, total, delay, first_err,
            )
            try:
                await asyncio.sleep(delay)
                await send_fn(chunk)
            except Exception as retry_err:
                await _notify_delivery_failed(channel_name, user_id, retry_err, idx, total)
                return False
    return True


MAX_RETRY_AFTER_S = 30.0


def _retry_after(err: BaseException) -> float:
    """Seconds a rate-limit error asks us to wait, or 0."""
    raw = getattr(err, "retry_after", None)
    if raw is None:
        resp = getattr(err, "response", None)
        headers = getattr(resp, "headers", None) or {}
        try:
            raw = headers.get("Retry-After") or headers.get("retry-after")
        except Exception:
            raw = None
    try:
        if hasattr(raw, "total_seconds"):
            return float(raw.total_seconds())
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


async def _notify_delivery_failed(
    channel_name: str,
    user_id: str,
    error: BaseException,
    chunk_index: int,
    total_chunks: int,
) -> None:
    """Log + surface a constraint_warning so the UI can toast the drop."""
    logger.error(
        "messaging delivery failed ch=%s user=%s chunk=%d/%d error=%s",
        channel_name, user_id, chunk_index + 1, total_chunks, error,
        exc_info=True,
    )
    # Local import to avoid import cycles at module load time
    try:
        from backend.core.ws_manager import ws_manager
        await ws_manager.send_constraint_warning(
            constraint=f"{channel_name}_delivery",
            message=(
                f"A {channel_name.title()} message didn't reach your device. "
                f"Check your connection or re-pair on the Messaging page."
            ),
            severity="warning",
            wizard_id=channel_name,
            user_id=user_id,
        )
    except Exception as ws_err:
        logger.warning("Failed to emit delivery-failure WS warning: %s", ws_err)
