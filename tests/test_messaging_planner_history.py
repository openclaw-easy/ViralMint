# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Chat from a phone carries the recent conversation into the planner.

Every inbound message used to reach the model as a lone user turn, so a bare
"yes" or "generate" in reply to the bot's own question (or to a notification it
pushed) arrived with no context at all.
"""
from __future__ import annotations

import pytest

from backend.agents import planner as planner_mod
from backend.agents.planner import PlannerAgent, _messaging_turns


class TestMessagingTurns:
    def test_no_history_is_just_the_message(self):
        assert _messaging_turns(None, "hi") == [{"role": "user", "content": "hi"}]

    def test_a_leading_notification_gets_a_user_turn_in_front(self):
        # Anthropic rejects a conversation that opens with the assistant.
        turns = _messaging_turns([{"role": "assistant", "content": "Download ready"}], "generate")
        assert [t["role"] for t in turns] == ["user", "assistant", "user"]
        assert turns[-1]["content"] == "generate"

    def test_consecutive_same_role_turns_merge(self):
        hist = [
            {"role": "user", "content": "scout cooking"},
            {"role": "assistant", "content": "On it."},
            {"role": "assistant", "content": "Scout finished: 12 videos"},
        ]
        turns = _messaging_turns(hist, "download top 5")
        assert [t["role"] for t in turns] == ["user", "assistant", "user"]
        assert "On it." in turns[1]["content"] and "Scout finished" in turns[1]["content"]

    def test_blank_and_unknown_roles_are_dropped(self):
        hist = [{"role": "system", "content": "x"}, {"role": "user", "content": "  "}]
        assert _messaging_turns(hist, "yes") == [{"role": "user", "content": "yes"}]


@pytest.mark.asyncio
async def test_handle_message_text_sends_the_history(monkeypatch):
    seen = {}

    class FakeAI:
        async def chat(self, messages, system, max_tokens):
            seen["messages"] = messages
            return "Sure."

    async def fake_common(self, user_settings, user_id):
        return {"ctx": {}, "suggestions": [], "cred_status": {}, "user_profile": None,
                "failures_text": "", "perf_insights": None, "news_ctx": None, "pipeline": None}

    monkeypatch.setattr(PlannerAgent, "_gather_common_context", fake_common)
    monkeypatch.setattr(planner_mod, "get_ai_client", lambda _s: FakeAI())

    agent = PlannerAgent()

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(agent.intelligence, "record_event", _noop)
    reply = await agent.handle_message_text(
        "yes", user_settings=None, user_id="local",
        history=[{"role": "assistant", "content": "Want me to write a script?"}],
    )
    assert reply.startswith("Sure.")
    assert [m["content"] for m in seen["messages"]][-2:] == ["Want me to write a script?", "yes"]
