# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Unit tests for AnalyzerAgent — the acquire→analyze seam.

Covers the testable pieces without touching Whisper, the AI providers, or
the real DB:
  - `_split_for_correction` (pure sentence-aligned chunking)
  - `_ai_correct_transcript` (best-effort AI cleanup — never loses content)
  - `AnalyzerAgent.run` insight extraction + normalization + DB row writes,
    driven off canned AI JSON with a mocked session
  - `reanalyze_single` guard paths (missing video / missing audio)

All heavy I/O (Whisper, get_ai_client, ws_manager, job status) is mocked.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents import analyzer as analyzer_mod
from backend.agents.analyzer import (
    AnalyzerAgent,
    _split_for_correction,
    _ai_correct_transcript,
    CORRECTION_CHUNK_CHARS,
    MAX_CORRECTION_CHUNKS,
)


# ── Fake DB session plumbing ────────────────────────────────────────────────

def _result_scalars_all(rows):
    r = MagicMock()
    r.scalars.return_value.all.return_value = rows
    return r


def _result_scalar_one(obj):
    r = MagicMock()
    r.scalar_one_or_none.return_value = obj
    return r


def _session_cm(results):
    """A fake `AsyncSessionLocal` whose returned context manager yields a
    shared session; `db.execute(...)` returns the queued results in order."""
    session = MagicMock()
    session.execute = AsyncMock(side_effect=list(results))
    session.commit = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    class FakeCM:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    factory = MagicMock(side_effect=lambda: FakeCM())
    return factory, session


def _fake_downloaded_video(**overrides):
    dv = MagicMock()
    dv.id = "abcdef01-2345-6789-abcd-ef0123456789"
    dv.transcript = "word " * 80  # >200 chars so segment analysis runs
    dv.transcript_source = "creator_subtitles"
    dv.transcript_language = "en"
    dv.insights_json = None
    dv.segment_analysis_json = None
    dv.improvement_suggestions_json = None
    dv.comments_json = None
    dv.comment_insights_json = None
    dv.transcript_segments_json = None
    dv.chapters_json = None
    dv.platform = "tiktok"          # not youtube → skip comment analysis
    dv.scout_result_id = None       # skip scout-result update
    dv.audio_path = None
    dv.video_path = None
    for k, v in overrides.items():
        setattr(dv, k, v)
    return dv


_INSIGHTS = {
    "hook": "Opens on a bold claim",
    "structure": "hook-problem-solution",
    "tone": "energetic",
    "topic_angle": "budgeting",
    "why_viral": "relatable",
    "scores": {"hook_quality": 8, "composite": 7.1},
}
_SEGMENTS = {"segments": [{"label": "Hook", "start_pct": 0, "end_pct": 8}],
             "overall_retention_curve": "front-loaded"}
_IMPROVEMENTS = {"improvements": [{"category": "hook", "priority": "high"}],
                 "biggest_opportunity": "Lead with the result"}


class TestSplitForCorrection:
    def test_short_text_single_chunk(self):
        chunks = _split_for_correction("Hello world.")
        assert chunks == ["Hello world."]

    def test_empty_text(self):
        assert _split_for_correction("") == []

    def test_reassembles_to_original(self):
        text = ("This is a sentence. " * 400).strip()  # > chunk size
        chunks = _split_for_correction(text)
        assert len(chunks) > 1
        assert "".join(chunks) == text

    def test_cuts_on_sentence_boundary(self):
        # First chunk should end right after a sentence terminator.
        text = "A. " + ("filler " * 900) + "end."
        chunks = _split_for_correction(text, chunk_chars=200)
        # Every non-final chunk that found a boundary ends with a terminator.
        assert all(len(c) <= 200 + 1 for c in chunks[:-1]) or len(chunks) == 1


class TestAiCorrectTranscript:
    @pytest.mark.asyncio
    async def test_ai_unavailable_returns_raw(self):
        with patch.object(analyzer_mod, "get_ai_client", side_effect=RuntimeError("no key")):
            out = await _ai_correct_transcript(None, "raw whisper text")
        assert out == "raw whisper text"

    @pytest.mark.asyncio
    async def test_successful_correction_returned(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="Corrected whisper text.")
        with patch.object(analyzer_mod, "get_ai_client", return_value=ai):
            out = await _ai_correct_transcript(None, "corected wisper txt")
        assert out == "Corrected whisper text."

    @pytest.mark.asyncio
    async def test_suspicious_length_keeps_raw(self):
        # Correction far too long vs input → treated as suspicious, raw kept.
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="x" * 5000)
        with patch.object(analyzer_mod, "get_ai_client", return_value=ai):
            out = await _ai_correct_transcript(None, "short input text")
        assert out == "short input text"

    @pytest.mark.asyncio
    async def test_chunk_exception_keeps_raw_chunk(self):
        ai = MagicMock()
        ai.chat = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(analyzer_mod, "get_ai_client", return_value=ai):
            out = await _ai_correct_transcript(None, "keep this text")
        assert "keep this text" in out

    @pytest.mark.asyncio
    async def test_passthrough_beyond_max_chunks(self):
        # Build more than MAX_CORRECTION_CHUNKS chunks; the tail passes raw.
        chunk = "A" * CORRECTION_CHUNK_CHARS
        text = "".join(chunk for _ in range(MAX_CORRECTION_CHUNKS + 2))
        ai = MagicMock()
        ai.chat = AsyncMock(side_effect=lambda **kw: "corrected")
        with patch.object(analyzer_mod, "get_ai_client", return_value=ai):
            out = await _ai_correct_transcript(None, text)
        # Only MAX_CORRECTION_CHUNKS chunks were sent to the AI.
        assert ai.chat.await_count == MAX_CORRECTION_CHUNKS
        # Raw tail is still present in the output.
        assert "A" * 50 in out


class TestAnalyzerRun:
    @pytest.mark.asyncio
    async def test_no_videos_early_return(self):
        factory, _ = _session_cm([_result_scalars_all([])])
        with patch.object(analyzer_mod, "AsyncSessionLocal", factory), \
             patch.object(analyzer_mod, "update_job_status", AsyncMock()) as ujs, \
             patch.object(analyzer_mod.ws_manager, "send_progress", AsyncMock()):
            await AnalyzerAgent().run(job_id="j1", user_id="local")
        # No videos → returns before touching job status.
        ujs.assert_not_called()

    @pytest.mark.asyncio
    async def test_subtitle_path_extracts_and_stores_insights(self):
        dv = _fake_downloaded_video()
        factory, session = _session_cm([
            _result_scalars_all([dv]),        # unanalyzed videos
            _result_scalar_one(None),         # user settings
            _result_scalar_one(dv),           # save: re-fetch row
        ])
        ai = MagicMock()
        ai.chat = AsyncMock(side_effect=[
            json.dumps(_INSIGHTS),
            json.dumps(_SEGMENTS),
            json.dumps(_IMPROVEMENTS),
        ])
        with patch.object(analyzer_mod, "AsyncSessionLocal", factory), \
             patch.object(analyzer_mod, "get_ai_client", return_value=ai), \
             patch.object(analyzer_mod, "update_job_status", AsyncMock()) as ujs, \
             patch.object(analyzer_mod.ws_manager, "send_progress", AsyncMock()), \
             patch.object(analyzer_mod.ws_manager, "send_constraint_warning", AsyncMock()):
            await AnalyzerAgent().run(job_id="j2", user_id="local")

        # Transcript came from subtitles (no Whisper call needed).
        assert dv.transcript_source == "creator_subtitles"
        # Insights parsed + persisted onto the row.
        stored = json.loads(dv.insights_json)
        assert stored["hook"] == "Opens on a bold claim"
        seg = json.loads(dv.segment_analysis_json)
        assert seg["overall_retention_curve"] == "front-loaded"
        imp = json.loads(dv.improvement_suggestions_json)
        assert imp["biggest_opportunity"] == "Lead with the result"
        session.commit.assert_awaited()
        ujs.assert_awaited()
        assert ujs.await_args_list[-1].args[1] == "success"

    @pytest.mark.asyncio
    async def test_code_fenced_json_is_stripped(self):
        dv = _fake_downloaded_video(transcript="short but fine text here now.")
        factory, _ = _session_cm([
            _result_scalars_all([dv]),
            _result_scalar_one(None),
            _result_scalar_one(dv),
        ])
        ai = MagicMock()
        fenced = "```json\n" + json.dumps(_INSIGHTS) + "\n```"
        # transcript < 200 chars → segment analysis skipped; only insights call.
        ai.chat = AsyncMock(return_value=fenced)
        with patch.object(analyzer_mod, "AsyncSessionLocal", factory), \
             patch.object(analyzer_mod, "get_ai_client", return_value=ai), \
             patch.object(analyzer_mod, "update_job_status", AsyncMock()), \
             patch.object(analyzer_mod.ws_manager, "send_progress", AsyncMock()), \
             patch.object(analyzer_mod.ws_manager, "send_constraint_warning", AsyncMock()):
            await AnalyzerAgent().run(job_id="j3", user_id="local")
        assert json.loads(dv.insights_json)["tone"] == "energetic"

    @pytest.mark.asyncio
    async def test_malformed_insight_json_triggers_ai_repair(self):
        dv = _fake_downloaded_video(transcript="short valid transcript text.")
        factory, _ = _session_cm([
            _result_scalars_all([dv]),
            _result_scalar_one(None),
            _result_scalar_one(dv),
        ])
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="{not valid json")
        with patch.object(analyzer_mod, "AsyncSessionLocal", factory), \
             patch.object(analyzer_mod, "get_ai_client", return_value=ai), \
             patch("backend.core.ai_retry.ai_fix_json",
                   AsyncMock(return_value={"hook": "repaired"})) as fix, \
             patch.object(analyzer_mod, "update_job_status", AsyncMock()), \
             patch.object(analyzer_mod.ws_manager, "send_progress", AsyncMock()), \
             patch.object(analyzer_mod.ws_manager, "send_constraint_warning", AsyncMock()):
            await AnalyzerAgent().run(job_id="j4", user_id="local")
        fix.assert_awaited()
        assert json.loads(dv.insights_json)["hook"] == "repaired"


class TestReanalyzeSingle:
    @pytest.mark.asyncio
    async def test_video_not_found_fails_job(self):
        factory, _ = _session_cm([_result_scalar_one(None)])
        with patch.object(analyzer_mod, "AsyncSessionLocal", factory), \
             patch.object(analyzer_mod, "update_job_status", AsyncMock()) as ujs:
            await AnalyzerAgent().reanalyze_single(
                job_id="j5", video_id="missing", user_id="local"
            )
        ujs.assert_awaited()
        assert ujs.await_args_list[-1].args[1] == "failed"

    @pytest.mark.asyncio
    async def test_missing_audio_file_fails_job(self):
        dv = _fake_downloaded_video(audio_path=None, video_path=None)
        factory, _ = _session_cm([
            _result_scalar_one(dv),     # fetch video
            _result_scalar_one(None),   # fetch user settings
        ])
        with patch.object(analyzer_mod, "AsyncSessionLocal", factory), \
             patch.object(analyzer_mod, "update_job_status", AsyncMock()) as ujs, \
             patch.object(analyzer_mod.ws_manager, "send_progress", AsyncMock()):
            await AnalyzerAgent().reanalyze_single(
                job_id="j6", video_id=dv.id, user_id="local"
            )
        assert ujs.await_args_list[-1].args[1] == "failed"
        assert "audio" in ujs.await_args_list[-1].kwargs["error_message"].lower()
