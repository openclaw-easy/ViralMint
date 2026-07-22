# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend.core.chat_history — the single server-side writer for
rich-card and job-event chat rows.

Two contracts:
  1. _job_complete_row (pure mapping) — must mirror the live-view branching
     in useWebSocket.js so history == live view.
  2. on_ws_event persistence — writes into the ContextVar-scoped session,
     dedups job-lifecycle events, and no-ops outside a chat turn / for
     non-persistable types. DB-backed; rows live under a unique session id
     and are cleaned up.

Ported/adapted from the SaaS test_chat_history_events.py — SaaS-only branches
(motion, agent_tool, tool_suggestion, video_proposal, _summarize_rich_message)
dropped because this OSS fork's _job_complete_row / _PERSISTABLE don't have them.
"""
from sqlalchemy import delete, select

from backend.core.chat_history import (
    CHAT_SESSION, _first_time, _job_complete_row, on_ws_event,
)


# ── _job_complete_row (pure mapping) ───────────────────────────────────────

def test_scout_counts_row_new_split():
    kind, text = _job_complete_row("scout", {"total_results": 12, "new_results": 4})
    assert kind == "system" and "12" in text and "4 new" in text


def test_scout_counts_row_all_new():
    kind, text = _job_complete_row("scout", {"total_results": 7})
    assert kind == "system" and "7" in text and "trending" in text


def test_news_scout_found_row():
    kind, text = _job_complete_row("news_scout", {"total_results": 3})
    assert kind == "system" and "3" in text and "article" in text


def test_news_scout_empty_row():
    kind, text = _job_complete_row("news_scout", {"total_results": 0})
    assert kind == "system" and "no articles" in text.lower()


def test_video_row():
    kind, payload = _job_complete_row("generate", {"video": {"id": "v1"}})
    assert kind == "rich:video_preview" and payload["video"]["id"] == "v1"


def test_insights_row():
    kind, payload = _job_complete_row("analyze", {"insights": [1, 2]})
    assert kind == "rich:insights" and payload["videos"] == [1, 2]


def test_generic_fallback_row():
    kind, text = _job_complete_row("whatever", {})
    assert kind == "system" and text == "Job complete!"


# ── _first_time dedup helper ───────────────────────────────────────────────

def test_first_time_dedups_per_key():
    assert _first_time("dedup-job-xyz", "job_started") is True
    assert _first_time("dedup-job-xyz", "job_started") is False
    # Different type on the same job id is a distinct key.
    assert _first_time("dedup-job-xyz", "job_complete") is True


# ── on_ws_event persistence (real DB, cleaned up) ──────────────────────────

async def _init():
    from backend.database import init_db
    await init_db()


async def _rows_for(session_id):
    from backend.database import AsyncSessionLocal
    from backend.models.chat_session import ChatMessage
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(ChatMessage).where(ChatMessage.session_id == session_id)
        )).scalars().all()


async def _seed_session(session_id):
    from backend.database import AsyncSessionLocal
    from backend.models.chat_session import ChatSession
    async with AsyncSessionLocal() as db:
        db.add(ChatSession(id=session_id, title="New chat"))
        await db.commit()


async def _cleanup(session_id):
    from backend.database import AsyncSessionLocal
    from backend.models.chat_session import ChatMessage, ChatSession
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
        await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
        await db.commit()


async def test_no_session_context_is_a_noop():
    await _init()
    # No CHAT_SESSION set → nothing persists (must not raise either).
    await on_ws_event({"type": "job_started", "job_id": "outside-1"}, "local")


async def test_non_persistable_type_is_ignored():
    await _init()
    sid = "test-hist-nonpersist"
    await _seed_session(sid)
    token = CHAT_SESSION.set(sid)
    try:
        await on_ws_event({"type": "chat_token", "token": "hi"}, "local")
        assert await _rows_for(sid) == []
    finally:
        CHAT_SESSION.reset(token)
        await _cleanup(sid)


async def test_job_started_dedup_and_failed():
    await _init()
    sid = "test-hist-jobs"
    await _seed_session(sid)
    token = CHAT_SESSION.set(sid)
    try:
        await on_ws_event({"type": "job_started", "job_id": "j-a", "job_type": "download",
                           "message": "Downloading"}, "local")
        await on_ws_event({"type": "job_started", "job_id": "j-a", "job_type": "download",
                           "message": "dup"}, "local")            # deduped
        await on_ws_event({"type": "job_failed", "job_id": "j-a", "error": "boom"}, "local")
        rows = await _rows_for(sid)
        kinds = [(r.role, r.msg_type) for r in rows]
        assert kinds.count(("rich", "job_progress")) == 1        # dedup held
        failed = [r for r in rows if r.role == "system"]
        assert len(failed) == 1 and "boom" in failed[0].content
    finally:
        CHAT_SESSION.reset(token)
        await _cleanup(sid)


async def test_scout_results_and_news_results_rows():
    await _init()
    sid = "test-hist-cards"
    await _seed_session(sid)
    token = CHAT_SESSION.set(sid)
    try:
        await on_ws_event({"type": "scout_results", "platform": "youtube",
                           "results": [{"id": 1}], "job_id": "s1"}, "local")
        await on_ws_event({"type": "news_results", "query": "ai",
                           "results": [{"id": 2}], "job_id": "n1"}, "local")
        rows = await _rows_for(sid)
        assert sorted(r.msg_type for r in rows) == ["news_results", "scout_results"]
        assert all(r.role == "rich" for r in rows)
    finally:
        CHAT_SESSION.reset(token)
        await _cleanup(sid)


async def test_job_complete_scout_writes_system_row():
    await _init()
    sid = "test-hist-complete"
    await _seed_session(sid)
    token = CHAT_SESSION.set(sid)
    try:
        await on_ws_event({"type": "job_complete", "job_id": "jc-1", "job_type": "scout",
                           "result": {"total_results": 9}}, "local")
        rows = await _rows_for(sid)
        assert len(rows) == 1 and rows[0].role == "system" and "9" in rows[0].content
    finally:
        CHAT_SESSION.reset(token)
        await _cleanup(sid)


async def test_persist_message_bumps_count_and_titles_session():
    await _init()
    from backend.core.chat_history import persist_message
    from backend.database import AsyncSessionLocal
    from backend.models.chat_session import ChatSession
    sid = "test-hist-count"
    await _seed_session(sid)
    try:
        await persist_message(sid, "user", content="first user message here")
        async with AsyncSessionLocal() as db:
            s = (await db.execute(
                select(ChatSession).where(ChatSession.id == sid))).scalar_one()
            assert s.message_count == 1
            assert s.title.startswith("first user message")
    finally:
        await _cleanup(sid)
