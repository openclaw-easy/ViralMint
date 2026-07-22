# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Unit tests for GeneratorAgent — the script→video generation seam.

Covers the testable pieces without touching TTS, Whisper, FFmpeg, Pexels,
or the real DB:
  - `_resolve_options` (dialog overrides > settings > defaults)
  - `_verify_script` (quality gate: too-short / refusal / echo / fence-strip)
  - `_get_words_per_group` (caption-style lookup)
  - `_generate_script` (prompt build + strip)
  - `_get_search_demand_section` (keyword injection formatting)
  - `_generate_metadata` (YouTube + TikTok JSON parse + fence-strip + repair)
  - `_generate_voice` (paid→free Edge fallback)
  - `_generate_video` (kenburns→stock→text fallback chain)
  - `_mix_music` (no-track passthrough)
  - `run` happy path + failure guards, driven off mocked collaborators.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents import generator as gen_mod
from backend.agents.generator import GeneratorAgent
from backend.services.tts_service import TTSProvider


# ── Fake DB session plumbing ────────────────────────────────────────────────

def _result_scalar_one(obj):
    r = MagicMock()
    r.scalar_one_or_none.return_value = obj
    return r


def _session_cm(results):
    session = MagicMock()
    session.execute = AsyncMock(side_effect=list(results))
    session.commit = AsyncMock()
    session.add = MagicMock()
    session.refresh = AsyncMock()

    class FakeCM:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    factory = MagicMock(side_effect=lambda: FakeCM())
    return factory, session


def _fake_source(**overrides):
    src = MagicMock()
    src.insights_json = json.dumps({
        "tone": "witty and sharp",
        "estimated_duration": 60,
        "topic_angle": "personal finance",
    })
    src.transcript = "Original competitor transcript text."
    src.niche = "personal finance"
    src.scout_result_id = None
    src.scout_result_id = None
    for k, v in overrides.items():
        setattr(src, k, v)
    return src


# ── _resolve_options ────────────────────────────────────────────────────────

class TestResolveOptions:
    def test_defaults_with_no_settings(self):
        opts = GeneratorAgent()._resolve_options(
            None, None, None, None, None, None, None
        )
        assert opts["tts_provider"] == TTSProvider.EDGE_TTS
        assert opts["tts_label"] == "Edge TTS"
        assert opts["caption_style"] == "viral"
        assert opts["caption_enabled"] is True
        assert opts["music_enabled"] is True
        assert opts["music_genre"] == "lofi"
        # viral style → 6 words per group (from CAPTION_STYLES)
        assert opts["words_per_group"] == 6

    def test_caption_none_disables_captions(self):
        opts = GeneratorAgent()._resolve_options(
            None, "edge_tts", None, "none", True, None, None
        )
        # style "none" forces caption_enabled False regardless of the flag.
        assert opts["caption_enabled"] is False

    def test_dialog_overrides_win(self):
        settings_obj = MagicMock()
        settings_obj.tts_provider = "edge_tts"
        settings_obj.caption_style = "classic"
        settings_obj.music_genre = "epic"
        opts = GeneratorAgent()._resolve_options(
            settings_obj, "edge_tts", "en-US-AriaNeural",
            "bold", True, False, "lofi",
        )
        assert opts["tts_voice"] == "en-US-AriaNeural"
        assert opts["caption_style"] == "bold"
        assert opts["music_enabled"] is False
        assert opts["music_genre"] == "lofi"  # override beat the setting


# ── _verify_script ──────────────────────────────────────────────────────────

class TestVerifyScript:
    @pytest.mark.asyncio
    async def test_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            await GeneratorAgent()._verify_script("hi")

    @pytest.mark.asyncio
    async def test_refusal_raises(self):
        with pytest.raises(ValueError, match="refused"):
            await GeneratorAgent()._verify_script(
                "I'm sorry, but I cannot help write that script for you here."
            )

    @pytest.mark.asyncio
    async def test_echoed_prompt_raises(self):
        with pytest.raises(ValueError, match="echoed"):
            await GeneratorAgent()._verify_script(
                "Generate a script about saving money for beginners now please."
            )

    @pytest.mark.asyncio
    async def test_code_fence_stripped(self):
        script = "```\nThis is the actual spoken narration for the short.\n```"
        out = await GeneratorAgent()._verify_script(script)
        assert "```" not in out
        assert out.startswith("This is the actual spoken narration")

    @pytest.mark.asyncio
    async def test_clean_script_passes_through(self):
        script = "Here is a perfectly good script about saving money fast."
        assert await GeneratorAgent()._verify_script(script) == script


# ── _get_words_per_group ────────────────────────────────────────────────────

class TestWordsPerGroup:
    def test_known_style(self):
        assert GeneratorAgent._get_words_per_group("viral") == 6

    def test_unknown_style_defaults_to_three(self):
        assert GeneratorAgent._get_words_per_group("does-not-exist") == 3


# ── _generate_script ────────────────────────────────────────────────────────

class TestGenerateScript:
    @pytest.mark.asyncio
    async def test_builds_prompt_and_strips_output(self):
        src = _fake_source()
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="   Final spoken script here.   ")
        with patch.object(gen_mod, "get_ai_client", return_value=ai), \
             patch.object(GeneratorAgent, "_get_search_demand_section",
                          AsyncMock(return_value="")), \
             patch("backend.core.user_intelligence.UserIntelligence.get_performance_insights",
                   AsyncMock(return_value=None)):
            out = await GeneratorAgent()._generate_script(
                src, "9:16", MagicMock(user_id="local")
            )
        assert out == "Final spoken script here."
        prompt = ai.chat.await_args.kwargs["messages"][0]["content"]
        # Tone from insights + TikTok platform guideline (9:16) both injected.
        assert "witty and sharp" in prompt
        assert "TikTok" in prompt

    @pytest.mark.asyncio
    async def test_user_instructions_appended(self):
        src = _fake_source()
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="script body")
        with patch.object(gen_mod, "get_ai_client", return_value=ai), \
             patch.object(GeneratorAgent, "_get_search_demand_section",
                          AsyncMock(return_value="")), \
             patch("backend.core.user_intelligence.UserIntelligence.get_performance_insights",
                   AsyncMock(return_value=None)):
            await GeneratorAgent()._generate_script(
                src, "16:9", MagicMock(user_id="local"),
                user_instructions="Make it funnier",
            )
        prompt = ai.chat.await_args.kwargs["messages"][0]["content"]
        assert "Make it funnier" in prompt


# ── _get_search_demand_section ──────────────────────────────────────────────

class TestSearchDemandSection:
    @pytest.mark.asyncio
    async def test_formats_demand_keywords(self):
        src = _fake_source(niche="ai tools")
        demand = {"demand_summary": "high demand", "top_keywords": ["a", "b", "c"]}
        with patch("backend.services.youtube_suggest_service.get_search_demand",
                   AsyncMock(return_value=demand)):
            out = await GeneratorAgent()._get_search_demand_section(src, {})
        assert "high demand" in out
        assert "a, b, c" in out

    @pytest.mark.asyncio
    async def test_no_niche_returns_empty(self):
        src = _fake_source(niche=None, scout_result_id=None)
        out = await GeneratorAgent()._get_search_demand_section(src, {})
        assert out == ""


# ── _generate_metadata ──────────────────────────────────────────────────────

class TestGenerateMetadata:
    @pytest.mark.asyncio
    async def test_parses_youtube_and_tiktok(self):
        ai = MagicMock()
        yt = {"title": "Big Title", "description": "desc", "tags": ["x"]}
        tt = {"title": "tiktok caption", "description": "d"}
        ai.chat = AsyncMock(side_effect=[json.dumps(yt), json.dumps(tt)])
        with patch.object(gen_mod, "get_ai_client", return_value=ai):
            meta = await GeneratorAgent()._generate_metadata(
                "some script", niche="", user_settings=None
            )
        assert meta["youtube"]["title"] == "Big Title"
        assert meta["tiktok"]["title"] == "tiktok caption"

    @pytest.mark.asyncio
    async def test_fenced_json_stripped(self):
        ai = MagicMock()
        yt = "```json\n" + json.dumps({"title": "Fenced"}) + "\n```"
        tt = "```\n" + json.dumps({"title": "tt"}) + "\n```"
        ai.chat = AsyncMock(side_effect=[yt, tt])
        with patch.object(gen_mod, "get_ai_client", return_value=ai):
            meta = await GeneratorAgent()._generate_metadata(
                "script", niche="", user_settings=None
            )
        assert meta["youtube"]["title"] == "Fenced"
        assert meta["tiktok"]["title"] == "tt"

    @pytest.mark.asyncio
    async def test_malformed_youtube_falls_back(self):
        ai = MagicMock()
        # YT malformed + repair returns None → fallback dict used.
        ai.chat = AsyncMock(side_effect=["{bad", json.dumps({"title": "ok"})])
        with patch.object(gen_mod, "get_ai_client", return_value=ai), \
             patch("backend.core.ai_retry.ai_fix_json", AsyncMock(return_value=None)):
            meta = await GeneratorAgent()._generate_metadata(
                "script preview text", niche="", user_settings=None
            )
        assert meta["youtube"]["title"] == "Untitled Video"


# ── _generate_voice ─────────────────────────────────────────────────────────

class TestGenerateVoice:
    @pytest.mark.asyncio
    async def test_edge_provider_calls_generate_tts(self):
        opts = {"tts_provider": TTSProvider.EDGE_TTS, "tts_voice": None,
                "tts_label": "Edge TTS"}
        with patch("backend.services.tts_service.generate_tts",
                   AsyncMock(return_value=Path("/tmp/v.mp3"))) as gt:
            out = await GeneratorAgent()._generate_voice("hi", opts, None)
        assert out == Path("/tmp/v.mp3")
        assert gt.await_args.kwargs["provider"] == TTSProvider.EDGE_TTS

    @pytest.mark.asyncio
    async def test_paid_provider_without_key_falls_back_to_edge(self):
        opts = {"tts_provider": TTSProvider.OPENAI_TTS, "tts_voice": None,
                "tts_label": "OpenAI TTS"}
        with patch.object(gen_mod.settings, "OPENAI_API_KEY", ""), \
             patch("backend.services.tts_service.generate_tts",
                   AsyncMock(return_value=Path("/tmp/edge.mp3"))) as gt:
            out = await GeneratorAgent()._generate_voice("hi", opts, None)
        assert out == Path("/tmp/edge.mp3")
        # Fell back to the free Edge provider.
        assert gt.await_args.kwargs["provider"] == TTSProvider.EDGE_TTS

    @pytest.mark.asyncio
    async def test_paid_provider_with_key_stays_paid(self):
        opts = {"tts_provider": TTSProvider.OPENAI_TTS, "tts_voice": None,
                "tts_label": "OpenAI TTS"}
        with patch.object(gen_mod.settings, "OPENAI_API_KEY", "sk-test"), \
             patch("backend.services.tts_service.generate_tts",
                   AsyncMock(return_value=Path("/tmp/openai.mp3"))) as gt:
            await GeneratorAgent()._generate_voice("hi", opts, None)
        assert gt.await_args.kwargs["provider"] == TTSProvider.OPENAI_TTS


# ── _generate_video ─────────────────────────────────────────────────────────

class TestGenerateVideo:
    @pytest.mark.asyncio
    async def test_kenburns_used_when_start_image(self):
        with patch.object(gen_mod, "generate_kenburns_video",
                          AsyncMock(return_value=Path("/tmp/kb.mp4"))) as kb, \
             patch.object(gen_mod, "generate_stock_video", AsyncMock()) as stock:
            out = await GeneratorAgent()._generate_video(
                "script", Path("/tmp/v.mp3"), "9:16", None, start_image="/tmp/i.png"
            )
        assert out == Path("/tmp/kb.mp4")
        stock.assert_not_called()

    @pytest.mark.asyncio
    async def test_stock_used_when_no_image(self):
        with patch.object(gen_mod, "generate_stock_video",
                          AsyncMock(return_value=Path("/tmp/stock.mp4"))):
            out = await GeneratorAgent()._generate_video(
                "script", Path("/tmp/v.mp3"), "9:16", None
            )
        assert out == Path("/tmp/stock.mp4")

    @pytest.mark.asyncio
    async def test_text_fallback_when_stock_returns_none(self):
        with patch.object(gen_mod, "generate_stock_video",
                          AsyncMock(return_value=None)), \
             patch("backend.services.ffmpeg_service.generate_text_video",
                   AsyncMock(return_value=Path("/tmp/text.mp4"))) as txt:
            out = await GeneratorAgent()._generate_video(
                "script", Path("/tmp/v.mp3"), "9:16", None
            )
        assert out == Path("/tmp/text.mp4")
        txt.assert_awaited()

    @pytest.mark.asyncio
    async def test_stock_exception_falls_to_text(self):
        with patch.object(gen_mod, "generate_stock_video",
                          AsyncMock(side_effect=RuntimeError("pexels down"))), \
             patch("backend.services.ffmpeg_service.generate_text_video",
                   AsyncMock(return_value=Path("/tmp/text.mp4"))):
            out = await GeneratorAgent()._generate_video(
                "script", Path("/tmp/v.mp3"), "9:16", None
            )
        assert out == Path("/tmp/text.mp4")


# ── _mix_music ──────────────────────────────────────────────────────────────

class TestMixMusic:
    @pytest.mark.asyncio
    async def test_no_track_returns_voice_unchanged(self):
        opts = {"music_genre": "lofi", "music_volume_db": -20.0}
        with patch("backend.services.music_service.select_music",
                   AsyncMock(return_value=None)):
            out = await GeneratorAgent()._mix_music(Path("/tmp/v.mp3"), opts)
        assert out == Path("/tmp/v.mp3")

    @pytest.mark.asyncio
    async def test_track_present_mixes(self):
        opts = {"music_genre": "lofi", "music_volume_db": -18.0}
        with patch("backend.services.music_service.select_music",
                   AsyncMock(return_value=Path("/tmp/m.mp3"))), \
             patch("backend.services.music_service.mix_audio",
                   AsyncMock(return_value=Path("/tmp/mixed.mp3"))) as mix:
            out = await GeneratorAgent()._mix_music(Path("/tmp/v.mp3"), opts)
        assert out == Path("/tmp/mixed.mp3")
        assert mix.await_args.kwargs["music_volume_db"] == -18.0


# ── run ─────────────────────────────────────────────────────────────────────

class TestRun:
    @pytest.mark.asyncio
    async def test_no_script_and_no_source_fails(self):
        factory, _ = _session_cm([_result_scalar_one(None)])  # user settings
        with patch.object(gen_mod, "AsyncSessionLocal", factory), \
             patch.object(gen_mod, "update_job_status", AsyncMock()) as ujs, \
             patch.object(gen_mod.ws_manager, "send", AsyncMock()), \
             patch.object(gen_mod.ws_manager, "send_progress", AsyncMock()):
            await GeneratorAgent().run(
                job_id="g1", downloaded_video_id=None, custom_script=None,
                user_id="local",
            )
        assert ujs.await_args_list[-1].args[1] == "failed"

    @pytest.mark.asyncio
    async def test_custom_script_happy_path_saves_row(self):
        factory, session = _session_cm([_result_scalar_one(None)])  # user settings
        script = "This is a solid custom narration script for a short video."
        meta = {"youtube": {"title": "T", "description": "d", "tags": ["a"]},
                "tiktok": {"title": "tt", "description": "d"}}
        with patch.object(gen_mod, "AsyncSessionLocal", factory), \
             patch.object(gen_mod, "update_job_status", AsyncMock()) as ujs, \
             patch.object(gen_mod.ws_manager, "send", AsyncMock()), \
             patch.object(gen_mod.ws_manager, "send_progress", AsyncMock()), \
             patch.object(GeneratorAgent, "_generate_voice",
                          AsyncMock(return_value=Path("/nonexistent/voice.mp3"))), \
             patch.object(GeneratorAgent, "_generate_video",
                          AsyncMock(return_value=Path("/tmp/final.mp4"))), \
             patch.object(GeneratorAgent, "_generate_metadata",
                          AsyncMock(return_value=meta)), \
             patch("backend.services.thumbnail_service.generate_ai_thumbnail",
                   AsyncMock(return_value=None)):
            await GeneratorAgent().run(
                job_id="g2", downloaded_video_id=None, custom_script=script,
                caption_enabled=False, music_enabled=False, user_id="local",
            )
        # A GeneratedVideo row was added and committed.
        session.add.assert_called_once()
        gv = session.add.call_args.args[0]
        assert gv.script == script
        assert gv.title == "T"
        assert gv.gen_tier == "free"
        assert gv.status == "ready"
        assert ujs.await_args_list[-1].args[1] == "success"

    @pytest.mark.asyncio
    async def test_video_generation_failure_fails_job(self):
        factory, _ = _session_cm([_result_scalar_one(None)])
        script = "Another perfectly acceptable custom script for the pipeline."
        with patch.object(gen_mod, "AsyncSessionLocal", factory), \
             patch.object(gen_mod, "update_job_status", AsyncMock()) as ujs, \
             patch.object(gen_mod.ws_manager, "send", AsyncMock()), \
             patch.object(gen_mod.ws_manager, "send_progress", AsyncMock()), \
             patch.object(GeneratorAgent, "_generate_voice",
                          AsyncMock(return_value=Path("/nonexistent/voice.mp3"))), \
             patch.object(GeneratorAgent, "_generate_video",
                          AsyncMock(return_value=None)):
            await GeneratorAgent().run(
                job_id="g3", downloaded_video_id=None, custom_script=script,
                caption_enabled=False, music_enabled=False, user_id="local",
            )
        assert ujs.await_args_list[-1].args[1] == "failed"
