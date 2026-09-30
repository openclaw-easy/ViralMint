# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Telegram messaging channel.

Uses python-telegram-bot v21 (fully async, long-polling). No webhook, no
HTTPS — a plain asyncio.Task inside the main event loop receives updates.

Lifecycle:
  - start():   load every active MessagingConfig row and spin up one Application per user
  - send():    forward a NotificationPayload to the user's chat
  - stop():    cleanly shut down every running Application

Inbound messages are routed to the PlannerAgent via the callback wired by
MessagingManager.set_planner_callback(). The callback signature is
async (text, user_id) -> str — Telegram replies with the non-streamed response.

Ownership: the bot answers exactly ONE private chat — the
one that sent the pairing code (normally via the t.me/<bot>?start=<code> link
the Messaging page shows, which Telegram delivers as "/start <code>"). Every other chat
is ignored, commands included. A saved chat_id from an older build keeps
working as the owner; switching to a different bot token clears it.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, InvalidToken, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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

PlannerCallback = Callable[[str, str], Awaitable[str]]

# Telegram messages max 4096 chars. Leave some headroom for markdown.
TG_MSG_LIMIT = 3800

# Stopping a bot waits for in-flight updates (PTB's app.stop joins the update
# queue), and an agent turn can run for a minute. Shutdown must not wait that
# long — the launcher kills the process at 8s and skips everyone else's cleanup.
STOP_TIMEOUT_S = 5.0

# A bot that can't reach Telegram at launch (offline, DNS) used to stay off for
# the whole session with nothing but a log line. Retry with backoff instead.
START_RETRY_DELAYS_S = (15, 30, 60, 120, 300, 600)


class _UserBot:
    """Per-user Telegram bot state."""

    def __init__(self, user_id: str, app: Application, chat_id: Optional[int], bot_username: str):
        self.user_id = user_id
        self.app = app
        self.chat_id = chat_id
        self.bot_username = bot_username
        # Shown on the Messaging page while nobody owns the bot yet; consumed on pairing.
        self.pair_code: Optional[str] = None if chat_id else new_pair_code()
        self._polling_task: Optional[asyncio.Task] = None

    async def start_polling(self) -> None:
        """Start long-polling in the current event loop."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram polling started for user=%s bot=@%s", self.user_id, self.bot_username)

    async def stop_polling(self) -> None:
        async def _stop():
            if self.app.updater and self.app.updater.running:
                await self.app.updater.stop()
            if self.app.running:
                await self.app.stop()
            await self.app.shutdown()
        try:
            await asyncio.wait_for(_stop(), timeout=STOP_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("Telegram shutdown for user=%s took over %.0fs — abandoned",
                           self.user_id, STOP_TIMEOUT_S)
        except Exception as e:
            logger.warning("Telegram shutdown error for user=%s: %s", self.user_id, e)


class TelegramChannel(MessagingChannel):
    channel_name = "telegram"

    def __init__(self) -> None:
        self._bots: dict[str, _UserBot] = {}
        self._planner_callback: Optional[PlannerCallback] = None
        # One connect / disconnect / start at a time. Two overlapping connects
        # each passed "pop the old bot" before either stored its new one, so
        # two pollers ran on one token (Telegram 409s forever) and one of them
        # was never stopped.
        self._lock = asyncio.Lock()
        self._retry_tasks: dict[str, asyncio.Task] = {}

    # ── MessagingManager wiring ───────────────────────────────────────────────

    def set_planner_callback(self, callback: PlannerCallback) -> None:
        self._planner_callback = callback

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Load every active Telegram config and spin up bots."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(MessagingConfig).where(
                    MessagingConfig.channel == "telegram",
                    MessagingConfig.is_active == True,  # noqa: E712
                )
            )
            configs = result.scalars().all()

        for cfg in configs:
            token = decrypt_safe(cfg.bot_token_encrypted or "")
            if not token:
                logger.warning("Telegram config %s has no valid token (decryption failed?)", cfg.id)
                continue
            chat_id = int(cfg.chat_id) if cfg.chat_id else None
            try:
                async with self._lock:
                    await self._spin_up_bot(cfg.user_id, token, chat_id=chat_id)
            except InvalidToken:
                logger.warning("Telegram token for user=%s was revoked — reconnect it on the Messaging page",
                               cfg.user_id)
            except Exception as e:
                logger.warning("Failed to start Telegram bot for user=%s: %s — retrying",
                               cfg.user_id, e)
                self._schedule_retry(cfg.user_id, token, chat_id)

    async def stop(self) -> None:
        for task in self._retry_tasks.values():
            task.cancel()
        self._retry_tasks.clear()
        bots = list(self._bots.values())
        self._bots.clear()
        await asyncio.gather(*(b.stop_polling() for b in bots), return_exceptions=True)

    def _schedule_retry(self, user_id: str, token: str, chat_id: Optional[int]) -> None:
        old = self._retry_tasks.pop(user_id, None)
        if old:
            old.cancel()

        async def _retry():
            for delay in START_RETRY_DELAYS_S:
                await asyncio.sleep(delay)
                try:
                    async with self._lock:
                        if user_id in self._bots:
                            return          # a manual connect beat us to it
                        await self._spin_up_bot(user_id, token, chat_id=chat_id)
                    logger.info("Telegram bot for user=%s started on retry", user_id)
                    return
                except InvalidToken:
                    logger.warning("Telegram token for user=%s is invalid — giving up", user_id)
                    return
                except Exception as e:
                    logger.warning("Telegram retry for user=%s failed: %s", user_id, e)
            logger.warning("Telegram bot for user=%s could not start — reconnect it on the Messaging page", user_id)

        task = asyncio.create_task(_retry(), name=f"telegram-retry-{user_id}")
        task.add_done_callback(lambda t, uid=user_id: self._retry_tasks.pop(uid, None)
                               if self._retry_tasks.get(uid) is t else None)
        self._retry_tasks[user_id] = task

    def _cancel_retry(self, user_id: str) -> None:
        task = self._retry_tasks.pop(user_id, None)
        if task:
            task.cancel()

    # ── Send ──────────────────────────────────────────────────────────────────

    async def is_configured(self, user_id: str) -> bool:
        bot = self._bots.get(user_id)
        return bool(bot and bot.chat_id)

    async def send(self, user_id: str, payload: NotificationPayload) -> bool:
        bot = self._bots.get(user_id)
        if not bot or not bot.chat_id:
            return False

        text = f"*{_md(payload.title)}*\n{payload.body}" if payload.title else payload.body
        chunks = split_text(text, TG_MSG_LIMIT)
        keyboard = self._build_keyboard(payload.action_buttons)
        last_idx = len(chunks) - 1

        # Counter lets us attach action buttons only to the last chunk without
        # fighting the (chunk,) send_fn signature of send_with_retry.
        sent_count = 0

        async def _sender(chunk: str) -> None:
            nonlocal sent_count
            reply_markup = keyboard if sent_count == last_idx else None
            try:
                await bot.app.bot.send_message(
                    chat_id=bot.chat_id,
                    text=chunk,
                    parse_mode="Markdown",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            except BadRequest as e:
                # Only a REJECTED message (unparseable Markdown in a title) is
                # resent as plain text. A timeout may have been delivered, and
                # resending on every TelegramError sent it twice.
                logger.warning("Telegram markdown send failed, retrying plain: %s", e)
                await bot.app.bot.send_message(
                    chat_id=bot.chat_id,
                    text=chunk,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            sent_count += 1

        ok = await send_with_retry("telegram", user_id, chunks, _sender)
        if ok:
            await touch_last_message(user_id, "telegram")
        return ok

    # ── Public API used by backend/api/messaging.py ──────────────────────────

    async def configure(self, user_id: str, bot_token: str) -> dict:
        """Validate bot token, persist encrypted, start polling.
        Returns {bot_username, bot_url}. Raises ValueError on invalid token.
        """
        async with self._lock:
            # Validate via getMe before touching anything. `async with` closes
            # the probe's HTTP client — the old throwaway Application leaked one
            # per connect.
            try:
                async with Bot(bot_token) as probe:
                    await probe.get_me()
            except InvalidToken:
                raise ValueError("Invalid Telegram bot token.")
            except TelegramError as e:
                raise ValueError(f"Telegram rejected token: {e}")

            self._cancel_retry(user_id)
            existing = self._bots.pop(user_id, None)
            if existing:
                await existing.stop_polling()

            async with AsyncSessionLocal() as db:
                row = await db.execute(
                    select(MessagingConfig).where(
                        MessagingConfig.user_id == user_id,
                        MessagingConfig.channel == "telegram",
                    )
                )
                cfg = row.scalar_one_or_none()
                if cfg:
                    # A DIFFERENT bot (or a regenerated token) must be paired
                    # again: the saved chat belongs to the old bot, which the
                    # new one cannot message, so keeping it reported
                    # "connected" while every send failed.
                    if decrypt_safe(cfg.bot_token_encrypted or "") != bot_token:
                        cfg.chat_id = None
                    cfg.bot_token_encrypted = encrypt(bot_token)
                    cfg.is_active = True
                    cfg.connected_at = datetime.utcnow()
                else:
                    cfg = MessagingConfig(
                        user_id=user_id,
                        channel="telegram",
                        bot_token_encrypted=encrypt(bot_token),
                        is_active=True,
                        connected_at=datetime.utcnow(),
                    )
                    db.add(cfg)
                await db.commit()
                chat_id = int(cfg.chat_id) if cfg.chat_id else None

            await self._spin_up_bot(user_id, bot_token, chat_id=chat_id)
            return self.status(user_id)

    async def disconnect(self, user_id: str) -> bool:
        async with self._lock:
            self._cancel_retry(user_id)
            bot = self._bots.pop(user_id, None)
            if bot:
                await bot.stop_polling()
            await deactivate_config(user_id, "telegram", clear_tokens=True)
        return True

    async def send_test(self, user_id: str) -> bool:
        return await self.send(
            user_id,
            NotificationPayload(
                event=NotificationEvent.JOB_FAILED,  # any event works — title/body drive render
                title="ViralMint test",
                body="If you can read this, your Telegram bot is wired up. 🎉",
            ),
        )

    def status(self, user_id: str) -> dict:
        bot = self._bots.get(user_id)
        if not bot:
            return {"connected": False, "awaiting_start": False, "bot_username": None,
                    "bot_url": None, "chat_id": None, "pair_code": None,
                    "retrying": user_id in self._retry_tasks}
        awaiting = not bot.chat_id
        base = f"https://t.me/{bot.bot_username}" if bot.bot_username else ""
        return {
            "connected": bool(bot.chat_id),
            "awaiting_start": awaiting,
            "bot_username": bot.bot_username,
            # While unpaired the link carries the code: Telegram opens the bot
            # with a Start button that sends "/start <code>" — one tap pairs.
            "bot_url": (f"{base}?start={bot.pair_code}" if awaiting and base and bot.pair_code else base) or None,
            "chat_id": bot.chat_id,
            "pair_code": bot.pair_code if awaiting else None,
            "retrying": False,
        }

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _spin_up_bot(self, user_id: str, bot_token: str, chat_id: Optional[int]) -> None:
        # Older builds bound /start from ANY chat, groups included (negative
        # ids). _is_owner accepts only a private chat and _try_pair refuses
        # while an owner is set, so a saved group id would lock the owner out
        # silently — ask for the pairing code again instead.
        if chat_id is not None and chat_id < 0:
            logger.warning("Telegram user=%s was bound to a group chat — pairing again", user_id)
            chat_id = None
        app = ApplicationBuilder().token(bot_token).build()
        me = await app.bot.get_me()

        user_bot = _UserBot(user_id=user_id, app=app, chat_id=chat_id, bot_username=me.username or "")
        self._register_handlers(app, user_id)
        await user_bot.start_polling()
        self._bots[user_id] = user_bot

    def _is_owner(self, update: Update, user_id: str) -> bool:
        """The paired owner's private chat, and nothing else."""
        bot = self._bots.get(user_id)
        chat = update.effective_chat
        return bool(bot and bot.chat_id and chat and chat.type == "private"
                    and chat.id == bot.chat_id)

    async def _try_pair(self, update: Update, user_id: str, text: str) -> str:
        """Returns "paired", "owner" (already ours), or "refused"."""
        bot = self._bots.get(user_id)
        chat = update.effective_chat
        if not bot or not chat:
            return "refused"
        if self._is_owner(update, user_id):
            return "owner"
        if bot.chat_id or chat.type != "private" or not matches_pair_code(text, bot.pair_code):
            return "refused"
        bot.chat_id = chat.id
        bot.pair_code = None
        await persist_chat_id(user_id, "telegram", str(chat.id))
        await ws_manager.send(
            {"type": "telegram_connected", "chat_id": chat.id, "user_id": user_id},
            user_id,
        )
        logger.info("Telegram bot for user=%s paired to chat %s", user_id, chat.id)
        return "paired"

    async def _refuse(self, update: Update, user_id: str) -> None:
        """Strangers get one short line while the bot is unpaired, nothing after.

        Once an owner exists, silence: a reply would confirm the bot is live
        and give anyone a way to make it send messages."""
        bot = self._bots.get(user_id)
        msg = update.effective_message
        chat = update.effective_chat
        if bot and not bot.chat_id and msg and chat and chat.type == "private":
            try:
                await msg.reply_text(UNPAIRED_REPLY)
            except TelegramError:
                pass

    def _register_handlers(self, app: Application, user_id: str) -> None:
        app.add_handler(CommandHandler("start", self._make_start_handler(user_id)))
        app.add_handler(CommandHandler("help", self._make_help_handler(user_id)))
        app.add_handler(CommandHandler("status", self._make_status_handler(user_id)))
        app.add_handler(CallbackQueryHandler(self._make_callback_handler(user_id)))
        app.add_handler(
            # UpdateType.MESSAGE: an EDITED message would otherwise re-run a
            # agent turn (real AI calls) — PTB's TEXT filter matches edits too.
            MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND,
                           self._make_text_handler(user_id))
        )

    def _make_start_handler(self, user_id: str):
        async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not update.effective_chat or not update.effective_message:
                return
            # The deep link delivers "/start <code>"; a code typed after a bare
            # /start arrives through the text handler instead.
            outcome = await self._try_pair(update, user_id, " ".join(ctx.args or []))
            if outcome == "refused":
                await self._refuse(update, user_id)
                return
            await update.effective_message.reply_text(
                "🎬 *ViralMint connected!*\n\n"
                "You'll get a message here when scouts, downloads and videos finish.\n"
                "You can also just talk to me — say `download https://...` or `scout cooking videos`.",
                parse_mode="Markdown",
            )

        return handler

    def _make_help_handler(self, user_id: str):
        async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not self._is_owner(update, user_id):
                await self._refuse(update, user_id)
                return
            await update.effective_message.reply_text(
                "*ViralMint bot commands*\n\n"
                "`/start`  — Link this chat\n"
                "`/status` — Recent jobs\n"
                "`/help`   — This message\n\n"
                "Or just ask naturally: _download this: https://..._",
                parse_mode="Markdown",
            )

        return handler

    def _make_status_handler(self, user_id: str):
        async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            # Job titles are the owner's private data.
            if not self._is_owner(update, user_id):
                await self._refuse(update, user_id)
                return
            text = await self._recent_jobs_summary(user_id)
            # Plain text: job titles are user content, and one "_" or "`" in a
            # title made Markdown parsing fail with no reply at all.
            await update.effective_message.reply_text(text)

        return handler

    def _make_callback_handler(self, user_id: str):
        async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            query = update.callback_query
            if not query:
                return
            await query.answer()
            if not self._is_owner(update, user_id):
                return
            data = query.data or ""
            if not self._planner_callback:
                await query.edit_message_reply_markup(reply_markup=None)
                return

            # Encode button callbacks as synthetic planner messages
            if data.startswith("planner:"):
                synthetic = data[len("planner:"):]
                try:
                    reply = await self._planner_callback(synthetic, user_id)
                except Exception as e:
                    logger.exception("Planner callback (button) failed: %s", e)
                    reply = "Something went wrong handling that action."
                await query.edit_message_reply_markup(reply_markup=None)
                clean = strip_action_blocks(reply) or "Done. ✅"
                chunks = split_text(clean, TG_MSG_LIMIT)

                async def _btn_sender(chunk: str) -> None:
                    try:
                        await query.message.reply_text(chunk, parse_mode="Markdown", disable_web_page_preview=True)
                    except TelegramError:
                        await query.message.reply_text(chunk, disable_web_page_preview=True)

                ok = await send_with_retry("telegram", user_id, chunks, _btn_sender)
                if ok:
                    await touch_last_message(user_id, "telegram")

        return handler

    def _make_text_handler(self, user_id: str):
        async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            msg = update.effective_message
            if not msg or not msg.text:
                return
            if not self._is_owner(update, user_id):
                # An unpaired bot accepts the code typed as a plain message too.
                if await self._try_pair(update, user_id, msg.text) == "paired":
                    await msg.reply_text("🎬 ViralMint connected! Message me any time.")
                else:
                    await self._refuse(update, user_id)
                return

            if not self._planner_callback:
                await msg.reply_text("Planner not ready yet — try again in a moment.")
                return

            # Typing indicator — non-blocking
            try:
                await ctx.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
            except Exception:
                pass

            try:
                reply = await self._planner_callback(msg.text, user_id)
            except Exception as e:
                logger.exception("Planner callback failed for user=%s: %s", user_id, e)
                await msg.reply_text("Something went wrong. Check ViralMint for details.")
                return

            clean = strip_action_blocks(reply) or "Done. ✅"
            chunks = split_text(clean, TG_MSG_LIMIT)

            async def _reply_sender(chunk: str) -> None:
                try:
                    await msg.reply_text(chunk, parse_mode="Markdown", disable_web_page_preview=True)
                except TelegramError:
                    await msg.reply_text(chunk, disable_web_page_preview=True)

            ok = await send_with_retry("telegram", user_id, chunks, _reply_sender)
            if ok:
                await touch_last_message(user_id, "telegram")

        return handler

    async def _recent_jobs_summary(self, user_id: str) -> str:
        """Read the last 5 jobs straight from the DB — no cross-agent imports."""
        from backend.models.job import Job  # local import avoids circulars at startup

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Job)
                .where(Job.user_id == user_id)
                .order_by(Job.created_at.desc())
                .limit(5)
            )
            jobs = result.scalars().all()

        if not jobs:
            return "No recent jobs."

        lines = ["Recent jobs:"]
        for j in jobs:
            icon = {
                "success": "✅",
                "failed": "❌",
                "running": "⏳",
                "pending": "…",
                "cancelled": "🚫",
            }.get(j.status, "•")
            title = j.title or j.job_type
            lines.append(f"{icon} {j.job_type} — {title[:60]} ({j.status})")
        return "\n".join(lines)

    def _build_keyboard(self, buttons: list[dict]) -> Optional[InlineKeyboardMarkup]:
        if not buttons:
            return None
        rows = []
        for btn in buttons:
            label = btn.get("label")
            callback = btn.get("callback")
            if not label or not callback:
                continue
            # Telegram callback_data max 64 bytes
            data = f"planner:{callback}"[:64]
            rows.append([InlineKeyboardButton(label, callback_data=data)])
        return InlineKeyboardMarkup(rows) if rows else None


def _md(s: str) -> str:
    """Escape characters that would break Markdown parsing inside titles."""
    return (s or "").replace("*", "").replace("_", "").replace("`", "")


# Singleton
telegram_channel = TelegramChannel()
