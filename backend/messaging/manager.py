# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
MessagingManager — routes notifications to all configured messaging channels
and wires inbound messages from channels into the PlannerAgent.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from backend.messaging.base import (
    MessagingChannel,
    NotificationEvent,
    NotificationPayload,
)

logger = logging.getLogger(__name__)

# planner callback: async (text, user_id) -> full response string
PlannerCallback = Callable[[str, str], Awaitable[str]]


class MessagingManager:
    def __init__(self) -> None:
        self._channels: list[MessagingChannel] = []
        self._planner_callback: Optional[PlannerCallback] = None

    def register(self, channel: MessagingChannel) -> None:
        self._channels.append(channel)

    def set_planner_callback(self, callback: PlannerCallback) -> None:
        """Wire the planner so channels can route inbound text back to it."""
        self._planner_callback = callback
        for ch in self._channels:
            setter = getattr(ch, "set_planner_callback", None)
            if callable(setter):
                setter(callback)

    def get_planner_callback(self) -> Optional[PlannerCallback]:
        return self._planner_callback

    def get_channel(self, channel_name: str) -> Optional[MessagingChannel]:
        for ch in self._channels:
            if ch.channel_name == channel_name:
                return ch
        return None

    async def start_all(self) -> None:
        """Start every registered channel CONCURRENTLY.

        Each channel's start() does a network handshake (Telegram long-poll
        init, Slack socket-mode, Discord gateway; WhatsApp backgrounds its own
        connect). Running them serially made the slowest channel gate the
        rest — and since the lifespan used to await this, it also gated the
        browser-open "ready" signal. Gather connects them in parallel; each
        channel's failure is isolated + logged so one bad token can't stop
        the others (per-task try/except → gather never raises).
        """
        async def _start_one(ch: MessagingChannel) -> None:
            try:
                await ch.start()
                logger.info(f"Messaging channel started: {ch.channel_name}")
            except Exception as e:
                logger.warning(f"Failed to start channel {ch.channel_name}: {e}")

        await asyncio.gather(*(_start_one(ch) for ch in self._channels))

    async def stop_all(self) -> None:
        for ch in self._channels:
            try:
                await ch.stop()
            except Exception as e:
                logger.warning(f"Error stopping channel {ch.channel_name}: {e}")

    async def notify(self, event: NotificationEvent, user_id: str = "local", **kwargs) -> None:
        """Fan out a notification to every configured channel. Never raises."""
        payload = _build_payload(event, kwargs)
        logger.info(
            "notify | event=%s user=%s channels=%d",
            event.value, user_id, len(self._channels),
        )
        delivered = 0
        for ch in self._channels:
            try:
                configured = await ch.is_configured(user_id)
                logger.info(
                    "notify | event=%s ch=%s configured=%s",
                    event.value, ch.channel_name, configured,
                )
                if not configured:
                    continue
                ok = await ch.send(user_id, payload)
                logger.info(
                    "notify | event=%s ch=%s sent=%s",
                    event.value, ch.channel_name, ok,
                )
                if ok:
                    delivered += 1
            except Exception as e:
                logger.warning(f"Notify failed via {ch.channel_name}: {e}", exc_info=True)
        logger.info("notify | event=%s delivered=%d", event.value, delivered)


def _build_payload(event: NotificationEvent, data: dict) -> NotificationPayload:
    """Translate a typed event + kwargs into a conversational, chat-style payload.

    Tone: match the web Chat page — friendly, proactive, always offer a concrete
    next step the user can reply with.
    """
    if event == NotificationEvent.SCOUT_COMPLETE:
        total = data.get("total", 0)
        niche = data.get("niche", "your niche")
        platforms = ", ".join(data.get("platforms") or []) or "the platforms"
        top_title = data.get("top_title", "")
        top_score = data.get("top_score", 0.0)
        lines = [f"🔍 Scout done — pulled *{total}* trending videos for _{niche}_ on {platforms}."]
        if top_title:
            lines.append(f"Top pick: *{top_title[:80]}* (score {top_score:.1f}).")
        lines.append("Want me to download the top 5? Just reply *download top 5*.")
        return NotificationPayload(event=event, title="Scout complete",
                                   body="\n".join(lines), data=data)

    if event == NotificationEvent.DOWNLOAD_COMPLETE:
        count = data.get("downloaded", data.get("count", 0))
        total = data.get("total", count)
        title = data.get("title")
        lines = []
        if count and total and count == 1 and title:
            lines.append(f"✅ Done — *{title}* is downloaded and analyzed.")
        elif count and total:
            lines.append(f"✅ Downloaded and analyzed *{count}* of {total} video(s).")
        else:
            lines.append("✅ Download finished.")
        lines.append("I pulled the transcript and insights. Want me to generate a script from it? Just reply *generate*.")
        return NotificationPayload(event=event, title="Download ready",
                                   body="\n".join(lines), data=data)

    if event == NotificationEvent.VIDEO_GENERATED:
        title = data.get("title") or "Your video"
        duration = data.get("duration_seconds") or 0
        dur_str = f" ({duration}s)" if duration else ""
        return NotificationPayload(
            event=event,
            title="Video ready",
            body=(
                f"🎬 *{title}*{dur_str} is generated and ready.\n"
                "Open ViralMint to preview, export, or post it."
            ),
            data=data,
        )

    if event == NotificationEvent.VIDEO_UPLOADED:
        title = data.get("title") or "Your video"
        platform = data.get("platform", "the platform")
        url = (data.get("url") or "").strip()
        body = f"📤 *{title}* is live on {platform}."
        if url:
            body += f"\n{url}"
        body += "\n\nNice one — want me to scout a fresh batch for the next one?"
        return NotificationPayload(event=event, title="Published",
                                   body=body, data=data)

    if event == NotificationEvent.JOB_FAILED:
        job_type = data.get("job_type", "Background")
        err = data.get("error", "Unknown error")
        return NotificationPayload(
            event=event,
            title=f"{job_type.title()} hit a snag",
            body=(
                f"⚠️ The {job_type} job didn't finish.\n"
                f"_{err}_\n\n"
                "Want me to retry? Reply *retry* or open ViralMint for details."
            ),
            data=data,
        )

    # Fallback
    return NotificationPayload(
        event=event,
        title=event.value.replace("_", " ").title(),
        body=str(data),
        data=data,
    )


# Singleton — import this everywhere
messaging = MessagingManager()


def _register_default_channels() -> None:
    """Register built-in channels the first time the manager is used."""
    if messaging._channels:
        return
    # Import here to avoid circulars on module import
    from backend.messaging.telegram_channel import telegram_channel
    from backend.messaging.whatsapp_channel import whatsapp_channel
    from backend.messaging.discord_channel import discord_channel
    from backend.messaging.slack_channel import slack_channel
    messaging.register(telegram_channel)
    messaging.register(whatsapp_channel)
    messaging.register(discord_channel)
    messaging.register(slack_channel)


_register_default_channels()
