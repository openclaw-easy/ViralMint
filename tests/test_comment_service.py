# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend.services.comment_service — the comment fetch/parse seams and
AI sentiment analysis.

All network + AI clients are mocked: the YouTube Data API `build`, httpx (TikHub),
and the AI client. No real quota is spent and no external call is made.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services import comment_service


# ── fetch_youtube_comments ───────────────────────────────────────────────────

def _yt_response(items):
    return {"items": items}


def _yt_item(text, author="Alice", likes=5, published="2026-01-01T00:00:00Z"):
    return {
        "snippet": {
            "topLevelComment": {
                "snippet": {
                    "textDisplay": text,
                    "authorDisplayName": author,
                    "likeCount": likes,
                    "publishedAt": published,
                }
            }
        }
    }


class TestFetchYouTubeComments:
    async def test_parses_comments(self):
        fake_youtube = MagicMock()
        (fake_youtube.commentThreads.return_value
            .list.return_value.execute.return_value) = _yt_response([
                _yt_item("Great video!", author="Bob", likes=42),
                _yt_item("Second"),
            ])
        with patch("googleapiclient.discovery.build", return_value=fake_youtube):
            out = await comment_service.fetch_youtube_comments("vid123", "KEY")
        assert len(out) == 2
        assert out[0] == {
            "author": "Bob", "text": "Great video!",
            "likes": 42, "published_at": "2026-01-01T00:00:00Z",
        }

    async def test_skips_empty_text(self):
        fake_youtube = MagicMock()
        (fake_youtube.commentThreads.return_value
            .list.return_value.execute.return_value) = _yt_response([
                _yt_item("   "),          # whitespace only → skipped
                _yt_item("real one"),
            ])
        with patch("googleapiclient.discovery.build", return_value=fake_youtube):
            out = await comment_service.fetch_youtube_comments("vid123", "KEY")
        assert len(out) == 1
        assert out[0]["text"] == "real one"

    async def test_http_error_returns_empty(self):
        from googleapiclient.errors import HttpError

        resp = MagicMock()
        resp.status = 403                      # comments disabled
        fake_youtube = MagicMock()
        (fake_youtube.commentThreads.return_value
            .list.return_value.execute.side_effect) = HttpError(resp, b"disabled")
        with patch("googleapiclient.discovery.build", return_value=fake_youtube):
            out = await comment_service.fetch_youtube_comments("vid123", "KEY")
        assert out == []

    async def test_generic_exception_returns_empty(self):
        with patch("googleapiclient.discovery.build",
                   side_effect=RuntimeError("boom")):
            out = await comment_service.fetch_youtube_comments("vid123", "KEY")
        assert out == []


# ── fetch_tiktok_comments ────────────────────────────────────────────────────

def _httpx_resp(status=200, payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload or {}
    return resp


class TestFetchTikTokComments:
    async def test_no_key_returns_empty_without_call(self):
        out = await comment_service.fetch_tiktok_comments("aweme1", "")
        assert out == []

    async def test_parses_comments(self):
        payload = {"data": {"comments": [
            {"text": "love it", "user": {"nickname": "Carol"}, "digg_count": 9},
            {"text": "", "user": {"nickname": "X"}},        # empty → skipped
            "not-a-dict",                                    # non-dict → skipped
        ]}}
        with patch("httpx.get", return_value=_httpx_resp(200, payload)):
            out = await comment_service.fetch_tiktok_comments("aweme1", "TIKKEY")
        assert out == [{
            "author": "Carol", "text": "love it",
            "likes": 9, "published_at": "",
        }]

    async def test_404_returns_empty(self):
        with patch("httpx.get", return_value=_httpx_resp(404)):
            out = await comment_service.fetch_tiktok_comments("aweme1", "TIKKEY")
        assert out == []

    async def test_429_returns_empty(self):
        with patch("httpx.get", return_value=_httpx_resp(429)):
            out = await comment_service.fetch_tiktok_comments("aweme1", "TIKKEY")
        assert out == []

    async def test_non_list_data_returns_empty(self):
        payload = {"data": {"comments": "unexpected"}}
        with patch("httpx.get", return_value=_httpx_resp(200, payload)):
            out = await comment_service.fetch_tiktok_comments("aweme1", "TIKKEY")
        assert out == []

    async def test_timeout_returns_empty(self):
        import httpx
        with patch("httpx.get", side_effect=httpx.TimeoutException("slow")):
            out = await comment_service.fetch_tiktok_comments("aweme1", "TIKKEY")
        assert out == []


# ── analyze_comments ─────────────────────────────────────────────────────────

def _ai_client(response_text):
    client = MagicMock()
    client.chat = AsyncMock(return_value=response_text)
    return client


class TestAnalyzeComments:
    async def test_empty_comments_returns_none(self):
        out = await comment_service.analyze_comments([], "transcript", _ai_client("{}"))
        assert out is None

    async def test_parses_plain_json(self):
        comments = [{"author": "A", "text": "hi", "likes": 3}]
        result = {"audience_sentiment": "positive", "sentiment_score": 0.8}
        client = _ai_client(json.dumps(result))
        out = await comment_service.analyze_comments(comments, "the transcript", client)
        assert out == result
        # transcript + formatted comments made it into the prompt
        sent_prompt = client.chat.call_args.kwargs["messages"][0]["content"]
        assert "the transcript" in sent_prompt
        assert "[3 likes] A: hi" in sent_prompt

    async def test_strips_markdown_fence(self):
        comments = [{"author": "A", "text": "hi"}]
        fenced = "```json\n{\"sentiment_score\": 0.5}\n```"
        out = await comment_service.analyze_comments(comments, "", _ai_client(fenced))
        assert out == {"sentiment_score": 0.5}

    async def test_bad_json_falls_back_to_ai_fix(self):
        comments = [{"author": "A", "text": "hi"}]
        repaired = {"sentiment_score": 0.9}
        with patch("backend.core.ai_retry.ai_fix_json",
                   AsyncMock(return_value=repaired)) as fixer:
            out = await comment_service.analyze_comments(
                comments, "", _ai_client("{not valid json"))
        assert out == repaired
        fixer.assert_awaited_once()

    async def test_ai_client_exception_returns_none(self):
        comments = [{"author": "A", "text": "hi"}]
        client = MagicMock()
        client.chat = AsyncMock(side_effect=RuntimeError("api down"))
        out = await comment_service.analyze_comments(comments, "", client)
        assert out is None
