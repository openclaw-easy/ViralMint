# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Server-side chat-history recorder — the single writer for rich cards and
job events in chat sessions.

Previously, rich WS events (scout results, job cards, "Job complete!" system
rows) were NEVER persisted in this fork: the frontend only added them to an
in-memory store, so they vanished on reload and results that finished with no
chat tab open were lost. Now the backend persists them at emit time and the
frontend only renders.

How session attribution works — a ContextVar that rides asyncio's natural
context propagation instead of hand-kept registries:

  1. The WS chat handler sets CHAT_SESSION around the planner turn
     (asyncio.create_task snapshots the context).
  2. Anything the turn does directly (planner action dispatch,
     task_runner.dispatch -> create_task) inherits it, so a job that finishes
     long after the turn returns still emits into the right session.
  3. ws_manager.send() calls on_ws_event() before delivery: if the event type
     is persistable AND a session is in context, the row is written — even
     when no client is connected.

Jobs started OUTSIDE a chat turn (tool pages, Channels page) carry no context
-> nothing is persisted. That is deliberate: chat history records
chat-initiated work only.

Note: this fork has no MCP loopback self-client, so there is no X-Chat-Session
header hop to replicate (unlike the hosted variant).
"""
from __future__ import annotations

import json
import logging
from collections import OrderedDict
from contextvars import ContextVar
from datetime import datetime

from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.chat_session import ChatSession, ChatMessage

logger = logging.getLogger(__name__)

# The chat session the current execution context is working for. None outside
# a chat turn. See module docstring for how it propagates.
CHAT_SESSION: ContextVar[str | None] = ContextVar("vm_chat_session", default=None)

# WS event types this module persists. Everything else (chat_token, progress
# ticks, constraint warnings, smart suggestions, session bookkeeping …) stays
# ephemeral. The set is kept in lock-step with the rich cards the frontend
# renders on reload (RichMessage switch in Chat.jsx) so history == live view.
_PERSISTABLE = {
    "job_started", "job_complete", "job_failed",
    "scout_results", "news_results", "channel_analysis",
    "downloaded_list", "content_calendar",
}

# Job-lifecycle events are one-shot per job but can be EMITTED twice (an
# endpoint and the agent may both emit for the same job id). Dedup on
# (job_id, type) so history gets exactly one row. Bounded FIFO.
_once: OrderedDict[tuple[str, str], bool] = OrderedDict()
_ONCE_CAP = 1000


def _first_time(job_id: str, msg_type: str) -> bool:
    key = (job_id, msg_type)
    if key in _once:
        return False
    _once[key] = True
    while len(_once) > _ONCE_CAP:
        _once.popitem(last=False)
    return True


async def persist_message(
    session_id: str,
    role: str,
    content: str | None = None,
    msg_type: str | None = None,
    data_json: str | None = None,
    user_id: str = "local",
) -> None:
    """Save a message row + session bookkeeping (count, updated_at, title).

    Mirrors the write path in backend/api/chat.py `_persist_message` so the WS
    handler (user/assistant turns) and on_ws_event (rich cards, job events)
    stay consistent. on_ws_event only ever writes role in {rich, system}, so
    the user-message title branch never fires from here — it is kept only for
    parity/robustness.
    """
    async with AsyncSessionLocal() as db:
        msg = ChatMessage(
            session_id=session_id,
            user_id=user_id,
            role=role,
            content=content,
            msg_type=msg_type,
            data_json=data_json,
        )
        db.add(msg)

        result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
        session = result.scalar_one_or_none()
        if session:
            session.message_count = (session.message_count or 0) + 1
            session.updated_at = datetime.utcnow()
            if session.title == "New chat" and role == "user" and content:
                session.title = content[:80] + ("..." if len(content) > 80 else "")

        await db.commit()


async def _record_rich(session_id: str, msg_type: str, data: dict, user_id: str) -> None:
    await persist_message(
        session_id, "rich", msg_type=msg_type,
        data_json=json.dumps(data, ensure_ascii=False, default=str), user_id=user_id,
    )


async def _record_system(session_id: str, content: str, user_id: str) -> None:
    await persist_message(session_id, "system", content=content, user_id=user_id)


async def _job_type_from_db(job_id: str) -> str:
    """Authoritative job_type for job_complete branching — most runners don't
    include it in the WS payload. One indexed PK read; rare event."""
    if not job_id:
        return ""
    try:
        from backend.models.job import Job
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(Job.job_type).where(Job.id == job_id))).scalar_one_or_none()
        return row or ""
    except Exception:  # noqa: BLE001
        return ""


def _job_complete_row(job_type: str, result: dict) -> tuple[str, str | dict] | None:
    """Map a job_complete payload to the row to persist.

    Returns ("system", text) or ("rich:<msg_type>", data) or None (skip).
    MUST mirror the live-view branching in useWebSocket.js `ws.on("job_complete")`
    — the persisted history and the live chat must describe the same event the
    same way. Change them together.
    """
    if "total_results" in result:
        total = result.get("total_results") or 0
        new = result.get("new_results")
        if job_type == "news_scout":
            text = (
                f"News research complete — found **{total}** article{'s' if total != 1 else ''}."
                if total > 0 else
                "News research complete — no articles scored high enough. Try a different or more specific query."
            )
        elif new is not None and new < total:
            text = f"Scout complete — found **{total}** videos ({new} new, {total - new} previously scouted)."
        else:
            text = f"Scout complete — found **{total}** trending videos."
        return ("system", text)

    if result.get("video"):
        return ("rich:video_preview", {"video": result["video"]})

    if result.get("insights"):
        return ("rich:insights", {"videos": result["insights"]})

    return ("system", "Job complete!")


async def on_ws_event(message: dict, user_id: str = "local") -> None:
    """Persist a WS event into the originating chat session, if any.

    Called by ws_manager.send() for EVERY outbound event, before delivery —
    so results are recorded even with zero connected clients. Must never
    raise (a history-write failure must not break the send), and must be
    near-free for non-persistable types (one set lookup).
    """
    try:
        msg_type = message.get("type")
        if msg_type not in _PERSISTABLE:
            return
        session_id = CHAT_SESSION.get()
        if not session_id:
            return

        job_id = str(message.get("job_id") or "")

        if msg_type == "job_started":
            if not job_id or not _first_time(job_id, msg_type):
                return
            await _record_rich(session_id, "job_progress", {
                "jobId": job_id,
                "jobType": message.get("job_type") or "",
                "message": message.get("message") or "",
            }, user_id)

        elif msg_type == "job_failed":
            if job_id and not _first_time(job_id, msg_type):
                return
            await _record_system(
                session_id, f"Job failed: {message.get('error') or 'Unknown error'}", user_id)

        elif msg_type == "job_complete":
            if job_id and not _first_time(job_id, msg_type):
                return
            job_type = message.get("job_type") or await _job_type_from_db(job_id)
            row = _job_complete_row(job_type, message.get("result") or {})
            if row is None:
                return
            kind, payload = row
            if kind == "system":
                await _record_system(session_id, payload, user_id)
            else:
                await _record_rich(session_id, kind.split(":", 1)[1], payload, user_id)

        elif msg_type == "scout_results":
            # One card per platform per scout — no dedup (distinct payloads).
            await _record_rich(session_id, "scout_results", {
                "results": message.get("results") or [],
                "platform": message.get("platform"),
                "jobId": job_id,
            }, user_id)

        elif msg_type == "news_results":
            await _record_rich(session_id, "news_results", {
                "results": message.get("results") or [],
                "query": message.get("query"),
                "jobId": job_id,
            }, user_id)

        elif msg_type == "channel_analysis":
            await _record_rich(session_id, "channel_summary",
                               {"summary": message.get("summary") or {}}, user_id)

        elif msg_type == "downloaded_list":
            await _record_rich(session_id, "downloaded_list",
                               {"videos": message.get("videos") or []}, user_id)

        elif msg_type == "content_calendar":
            await _record_rich(session_id, "content_calendar",
                               {"calendar": message.get("calendar") or []}, user_id)

    except Exception:  # noqa: BLE001 — history is a record, not a dependency
        logger.exception("chat-history record failed (non-fatal) for %s", message.get("type"))
