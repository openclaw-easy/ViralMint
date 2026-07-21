# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Agent 4: Video Generation Pipeline
Script -> Voice -> Transcribe -> Stock video -> Music Mix -> Captions -> Final output

- Free, local generation using Pexels stock footage matched to script keywords
- Multi-TTS providers (Edge TTS free by default; OpenAI TTS via BYOK)
- Word-by-word animated captions (viral style)
- Background music mixing
- Falls back to Ken Burns image zoom or text-on-background when Pexels is unavailable
"""
import hashlib
import json
import logging
from pathlib import Path
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.downloaded_video import DownloadedVideo
from backend.models.generated_video import GeneratedVideo
from backend.models.scout_result import ScoutResult
from backend.models.user_settings import UserSettings
from backend.core.ai_provider import get_ai_client
from backend.core.ws_manager import ws_manager
from backend.agents.job_helper import update_job_status
from backend.config import settings
from backend.core.exceptions import safe_json_loads

from backend.agents.generator_prompts import (
    PLATFORM_GUIDELINES, SCRIPT_PROMPT, YOUTUBE_META_PROMPT, TIKTOK_META_PROMPT,
)
from backend.agents.generator_video import (
    generate_stock_video, generate_kenburns_video,
)

logger = logging.getLogger(__name__)


class GeneratorAgent:
    async def run(
        self,
        job_id: str,
        downloaded_video_id: str,
        aspect_ratio: str = "9:16",
        user_id: str = "local",
        # Override options (from generation dialog)
        tts_provider: str = None,
        tts_voice: str = None,
        caption_style: str = None,
        caption_enabled: bool = None,
        music_enabled: bool = None,
        music_genre: str = None,
        custom_script: str = None,
        start_image: str = None,
        **_ignored,  # absorb deprecated kwargs (gen_tier, video_model, operation_type, etc.)
    ):
        """Full generation pipeline."""
        await update_job_status(job_id, "running", progress_pct=0, current_step="Loading source data...")

        # Load source data (optional — None for sourceless generation)
        source = None
        if downloaded_video_id:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(DownloadedVideo).where(DownloadedVideo.id == downloaded_video_id)
                )
                source = result.scalar_one_or_none()
            if not source:
                await update_job_status(job_id, "failed", error_message="Source video not found")
                return

        # Load user settings
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            user_settings = result.scalar_one_or_none()

        # Resolve generation options (dialog overrides > user settings > defaults)
        opts = self._resolve_options(user_settings, tts_provider, tts_voice,
                                      caption_style, caption_enabled,
                                      music_enabled, music_genre)

        # Pre-flight: validate required API keys before starting expensive pipeline
        preflight_errors = self._preflight_check(opts)
        if preflight_errors:
            await update_job_status(job_id, "failed",
                error_message=f"Missing configuration: {'; '.join(preflight_errors)}")
            await ws_manager.send({
                "type": "job_failed", "job_id": job_id,
                "error": f"Missing configuration: {'; '.join(preflight_errors)}",
            }, user_id)
            return

        try:
            # Step 1: Generate script (5%)
            if custom_script and custom_script.strip():
                await ws_manager.send_progress(job_id, 5, "Using custom script...", user_id)
                script = custom_script.strip()
            elif source:
                await ws_manager.send_progress(job_id, 5, "Generating script...", user_id)
                script = await self._generate_script(source, aspect_ratio, user_settings)
            else:
                await update_job_status(job_id, "failed", error_message="No script provided and no source video to generate from")
                return

            # Step 1b: Verify script quality before proceeding
            await ws_manager.send_progress(job_id, 8, "Verifying script quality...", user_id)
            script = await self._verify_script(script)

            # Voice + (optional) image-to-video / stock generation
            await ws_manager.send_progress(job_id, 15, f"Generating voiceover ({opts['tts_label']})...", user_id)
            voice_path = await self._generate_voice(script, opts, user_settings)

            # Transcribe voice for word timestamps (used by captions)
            segments = []
            if voice_path and opts["caption_enabled"]:
                await ws_manager.send_progress(job_id, 25, "Extracting word timestamps...", user_id)
                segments = await self._transcribe_for_captions(voice_path, script=script)

            # Generate video clips (stock footage / kenburns / text fallback)
            await ws_manager.send_progress(job_id, 35, "Generating video (stock footage)...", user_id)
            video_path = await self._generate_video(
                script, voice_path, aspect_ratio, user_settings, start_image=start_image,
            )

            if not video_path:
                await update_job_status(job_id, "failed", error_message="Video generation failed — check API keys")
                return

            # Step 5: Mix background music (60%)
            mixed_audio = voice_path
            if voice_path and opts["music_enabled"]:
                try:
                    await ws_manager.send_progress(job_id, 60, "Mixing background music...", user_id)
                    mixed_audio = await self._mix_music(voice_path, opts)
                except Exception as e:
                    logger.warning(f"Music mix failed, continuing without music: {e}")
                    mixed_audio = voice_path

            # Step 5b: Mix sound effects (62%)
            if mixed_audio and segments and opts.get("sfx_enabled", False):
                try:
                    await ws_manager.send_progress(job_id, 62, "Adding sound effects...", user_id)
                    from backend.services.sfx_service import auto_place_sfx, mix_sfx_into_audio
                    from backend.services.caption_service import _extract_word_timestamps
                    word_ts = _extract_word_timestamps(segments)
                    sfx_placements = await auto_place_sfx(
                        word_ts, style=opts.get("sfx_style", "moderate")
                    )
                    if sfx_placements:
                        mixed_audio = await mix_sfx_into_audio(mixed_audio, sfx_placements)
                except Exception as e:
                    logger.warning(f"SFX mixing failed, continuing without SFX: {e}")

            # Step 6: Merge audio into video (65%)
            if mixed_audio and mixed_audio.exists():
                try:
                    await ws_manager.send_progress(job_id, 65, "Merging audio...", user_id)
                    from backend.services.ffmpeg_service import add_audio_to_video
                    final_with_audio = settings.GENERATED_DIR / f"gen_{hashlib.md5(script.encode()).hexdigest()[:8]}_audio.mp4"
                    video_path = await add_audio_to_video(video_path, mixed_audio, final_with_audio)
                except Exception as e:
                    logger.warning(f"Audio merge failed, saving video without audio: {e}")

            # Step 7: Burn animated captions (75%)
            if segments and opts["caption_enabled"]:
                try:
                    await ws_manager.send_progress(job_id, 75, f"Burning {opts['caption_style']} captions...", user_id)
                    video_path = await self._burn_captions(video_path, segments, aspect_ratio, opts)
                except Exception as e:
                    logger.warning(f"Caption burn failed, saving video without captions: {e}")

            # Step 7b: Apply auto-zoom on highlighted words (80%)
            if segments and opts.get("auto_zoom_enabled", False):
                try:
                    await ws_manager.send_progress(job_id, 80, "Applying auto-zoom effects...", user_id)
                    from backend.services.caption_service import _extract_word_timestamps
                    from backend.services.ffmpeg_service import apply_auto_zoom
                    word_ts = _extract_word_timestamps(segments)
                    if word_ts:
                        video_path = await apply_auto_zoom(
                            Path(video_path),
                            word_ts,
                            words_per_group=opts.get("words_per_group", 3),
                        )
                except Exception as e:
                    logger.warning(f"Auto-zoom failed, saving video without zoom: {e}")

            # Step 8: Generate metadata (85%)
            metadata = {"youtube": {}, "tiktok": {}}
            try:
                await ws_manager.send_progress(job_id, 85, "Generating metadata...", user_id)
                niche = (safe_json_loads(source.insights_json, {}, logger).get("topic_angle", "") if source and source.insights_json else "")
                metadata = await self._generate_metadata(script, niche, user_settings)
                if not metadata or not isinstance(metadata, dict):
                    metadata = {"youtube": {}, "tiktok": {}}
            except Exception as e:
                logger.warning(f"Metadata generation failed, using defaults: {e}")
                niche = ""

            # Step 9: Generate AI thumbnail (92%)
            thumb_path = None
            try:
                await ws_manager.send_progress(job_id, 92, "Generating thumbnail...", user_id)
                from backend.services.thumbnail_service import generate_ai_thumbnail
                thumb_path = await generate_ai_thumbnail(
                    video_path=video_path,
                    script=script,
                    title=metadata.get("youtube", {}).get("title", ""),
                    user_settings=user_settings,
                )
            except Exception as e:
                logger.warning(f"Thumbnail generation failed, saving without thumbnail: {e}")

            # Save to DB
            async with AsyncSessionLocal() as db:
                gv = GeneratedVideo(
                    user_id=user_id,
                    source_scout_result_id=source.scout_result_id if source else None,
                    source_downloaded_video_id=downloaded_video_id,
                    title=metadata.get("youtube", {}).get("title", "Untitled"),
                    script=script,
                    niche=niche,
                    video_path=str(video_path),
                    audio_path=str(voice_path) if voice_path else None,
                    thumbnail_path=str(thumb_path) if thumb_path else None,
                    aspect_ratio=aspect_ratio,
                    voice_id=opts.get("tts_voice"),
                    gen_tier="free",
                    youtube_title=metadata.get("youtube", {}).get("title"),
                    youtube_description=metadata.get("youtube", {}).get("description"),
                    youtube_tags_json=json.dumps(metadata.get("youtube", {}).get("tags", [])),
                    tiktok_title=metadata.get("tiktok", {}).get("title"),
                    tiktok_description=metadata.get("tiktok", {}).get("description"),
                    status="ready",
                )
                db.add(gv)
                await db.commit()
                await db.refresh(gv)

            await update_job_status(
                job_id, "success",
                progress_pct=100,
                current_step="Video ready!",
                output_data={"generated_video_id": gv.id},
            )
            await ws_manager.send({
                "type": "job_complete",
                "job_id": job_id,
                "result": {"generated_video_id": gv.id, "title": gv.title},
            }, user_id)

        except Exception as e:
            logger.error(f"Generation pipeline failed: {e}", exc_info=True)
            await update_job_status(job_id, "failed", error_message=str(e))

    # ── Options & validation ────────────────────────────────────────────

    def _resolve_options(self, user_settings, tts_provider, tts_voice,
                         caption_style, caption_enabled, music_enabled, music_genre) -> dict:
        """Merge dialog overrides with user settings and defaults."""
        from backend.services.tts_service import TTSProvider, PROVIDER_INFO

        # TTS
        tts_p = tts_provider or (user_settings.tts_provider if user_settings else None) or "edge_tts"
        tts_enum = TTSProvider(tts_p)
        tts_v = tts_voice or (user_settings.preferred_tts_voice if user_settings else None)

        # Captions
        cap_style = caption_style or (user_settings.caption_style if user_settings else None) or "viral"
        cap_on = caption_enabled if caption_enabled is not None else (
            user_settings.caption_enabled if user_settings and user_settings.caption_enabled is not None else True
        )
        cap_emoji = (user_settings.caption_emoji_style if user_settings and hasattr(user_settings, 'caption_emoji_style') and user_settings.caption_emoji_style else "moderate")

        # Music
        mus_on = music_enabled if music_enabled is not None else (
            user_settings.music_enabled if user_settings and user_settings.music_enabled is not None else True
        )
        mus_genre = music_genre or (user_settings.music_genre if user_settings else None) or "lofi"
        mus_vol = user_settings.music_volume_db if user_settings and user_settings.music_volume_db else -20.0

        return {
            "tts_provider": tts_enum,
            "tts_voice": tts_v,
            "tts_label": PROVIDER_INFO[tts_enum]["label"],
            "caption_style": cap_style,
            "caption_enabled": cap_on and cap_style != "none",
            "caption_emoji_style": cap_emoji,
            "music_enabled": mus_on,
            "music_genre": mus_genre,
            "music_volume_db": mus_vol,
            "auto_zoom_enabled": (
                user_settings.auto_zoom_enabled
                if user_settings and hasattr(user_settings, 'auto_zoom_enabled') and user_settings.auto_zoom_enabled is not None
                else False
            ),
            "sfx_enabled": (
                user_settings.sfx_enabled
                if user_settings and hasattr(user_settings, 'sfx_enabled') and user_settings.sfx_enabled is not None
                else True
            ),
            "sfx_style": (
                user_settings.sfx_style
                if user_settings and hasattr(user_settings, 'sfx_style') and user_settings.sfx_style
                else "moderate"
            ),
            "words_per_group": self._get_words_per_group(cap_style) if cap_on else 3,
        }

    def _preflight_check(self, opts: dict) -> list[str]:
        """Validate required API keys/config before starting the pipeline."""
        from backend.services.tts_service import TTSProvider

        errors = []
        # Paid TTS providers (currently OpenAI TTS) check their key at call time.
        # Edge TTS works without any key.
        return errors

    # ── Script generation ───────────────────────────────────────────────

    async def _generate_script(self, source: DownloadedVideo, aspect_ratio: str, user_settings, user_instructions: str = None, target_platform: str = None) -> str:
        """Generate an original script from competitor insights."""
        insights = safe_json_loads(source.insights_json, {}, logger)
        tone = insights.get("tone", "conversational and engaging")
        platform_format = "vertical short-form" if aspect_ratio == "9:16" else "horizontal long-form"

        transcript_section = ""
        if source.transcript:
            truncated = source.transcript[:6000]
            if len(source.transcript) > 6000:
                truncated += "\n... [transcript truncated]"
            transcript_section = f"Original transcript of the source video:\n{truncated}"

        if not target_platform:
            target_platform = "youtube_long" if aspect_ratio == "16:9" else "tiktok"
        guidelines = PLATFORM_GUIDELINES.get(target_platform, "")

        # Fetch search demand keywords for the niche
        search_demand_section = await self._get_search_demand_section(source, insights)

        # Inject performance feedback if available
        perf_section = ""
        try:
            from backend.core.user_intelligence import UserIntelligence
            perf = await UserIntelligence().get_performance_insights(
                user_settings.user_id if user_settings else "local"
            )
            if perf:
                perf_section = (
                    f"\n\nCreator's past performance data (use to guide content decisions):\n"
                    f"Best niches: {', '.join(perf['best_niches'][:3])}\n"
                    f"Avg engagement: {perf['avg_engagement']}%\n"
                    f"Top video: {perf['top_videos'][0]['title']} ({perf['top_videos'][0]['views']:,} views)"
                )
        except Exception:
            pass

        prompt = SCRIPT_PROMPT.format(
            insights_json=json.dumps(insights, indent=2)[:2000],
            transcript_section=transcript_section,
            platform_guidelines=guidelines,
            search_demand_section=search_demand_section,
            duration_seconds=insights.get("estimated_duration", 90),
            aspect_ratio=aspect_ratio,
            platform_format=platform_format,
            tone=tone,
        )

        if perf_section:
            prompt += perf_section

        if user_instructions:
            prompt += f"\n\n## IMPORTANT — User's specific instructions (follow these closely):\n{user_instructions}"

        ai = get_ai_client(user_settings)
        script = await ai.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        return script.strip()

    async def _verify_script(self, script: str) -> str:
        """Verify and clean the generated script. Returns cleaned script or raises on failure."""
        if not script or len(script.strip()) < 30:
            raise ValueError("Generated script is too short (< 30 chars). Try regenerating.")

        cleaned = script.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()

        failure_markers = [
            "I cannot", "I can't", "I'm sorry", "As an AI",
            "I don't have access", "I apologize",
        ]
        first_line = cleaned.split("\n")[0].lower()
        if any(marker.lower() in first_line for marker in failure_markers):
            raise ValueError(f"AI refused to generate script: {cleaned[:100]}")

        if "generate a script" in cleaned.lower()[:100] or "write a video script" in cleaned.lower()[:100]:
            raise ValueError("AI echoed the prompt instead of generating a script.")

        logger.info(f"Script verified: {len(cleaned)} chars, first 80: {cleaned[:80]!r}")
        return cleaned

    async def _get_search_demand_section(self, source, insights: dict) -> str:
        """Fetch YouTube search demand data for the video's niche and format for prompt injection."""
        niche = getattr(source, "niche", None) or insights.get("topic_angle", "") or ""
        if not niche:
            try:
                async with AsyncSessionLocal() as db:
                    if source.scout_result_id:
                        result = await db.execute(
                            select(ScoutResult.niche).where(ScoutResult.id == source.scout_result_id)
                        )
                        niche = result.scalar_one_or_none() or ""
            except Exception:
                pass

        if not niche:
            return ""

        try:
            from backend.services.youtube_suggest_service import get_search_demand
            demand = await get_search_demand(niche)
            if not demand.get("demand_summary"):
                return ""
            return (
                f"YouTube Search Demand Data (use these keywords to maximize discoverability):\n"
                f"{demand['demand_summary']}\n"
                f"Top keywords users search for: {', '.join(demand.get('top_keywords', [])[:10])}"
            )
        except Exception as e:
            logger.debug(f"Search demand fetch failed: {e}")
            return ""

    # ── Voice generation ────────────────────────────────────────────────

    async def _generate_voice(self, script: str, opts: dict, user_settings) -> Path:
        """Generate TTS audio using the configured provider."""
        from backend.services.tts_service import generate_tts, TTSProvider

        provider = opts["tts_provider"]
        voice = opts.get("tts_voice")
        api_key = ""

        if provider == TTSProvider.OPENAI_TTS:
            api_key = settings.OPENAI_API_KEY
            if not voice:
                voice = "alloy"
        elif provider == TTSProvider.EDGE_TTS:
            if not voice:
                voice = "en-US-AndrewMultilingualNeural"

        # Fall back to free Edge TTS if a paid provider is selected without a key
        if provider != TTSProvider.EDGE_TTS and not api_key:
            logger.warning(f"{opts['tts_label']} key missing — falling back to Edge TTS (free)")
            provider = TTSProvider.EDGE_TTS
            voice = voice or "en-US-AndrewMultilingualNeural"
            api_key = ""

        try:
            return await generate_tts(
                text=script, provider=provider, voice_id=voice, api_key=api_key,
            )
        except Exception as e:
            if provider != TTSProvider.EDGE_TTS:
                logger.warning(f"TTS failed with {provider}, falling back to Edge TTS: {e}")
                return await generate_tts(text=script, provider=TTSProvider.EDGE_TTS, voice_id="en-US-AndrewMultilingualNeural")
            raise

    # ── Transcription ───────────────────────────────────────────────────

    async def _transcribe_for_captions(self, voice_path: Path, script: str | None = None) -> list[dict]:
        """Transcribe the generated voice audio to get word-level timestamps.

        Whisper is used for TIMING ONLY — when the narration `script` is known,
        its true text is aligned onto the timed words so CJK captions never
        display ASR homophone errors (崇祯→重真, 勤政→情政 burned into a real
        render's captions). Fail-open: no script or non-CJK → unchanged."""
        try:
            from backend.services.whisper_service import whisper_service
            whisper_service.load("fast")
            result = await whisper_service.transcribe(voice_path, language=None)
            segments = result.get("segments", [])
            if script:
                from backend.services.caption_service import align_script_to_segments
                segments = align_script_to_segments(script, segments)
            return segments
        except Exception as e:
            logger.warning(f"Voice transcription for captions failed: {e}")
            return []

    # ── Video generation ────────────────────────────────────────────────

    async def _generate_video(self, script: str, voice_path: Path, aspect_ratio: str, user_settings, start_image: str = None) -> Path:
        """Generate video: Pexels stock footage → Ken Burns image → text fallback."""
        result = None

        # Ken Burns from start image (if provided)
        if start_image:
            try:
                result = await generate_kenburns_video(start_image, voice_path, aspect_ratio)
            except Exception as e:
                logger.warning(f"Ken Burns video generation failed: {e}")

        # Pexels stock footage
        if not result:
            try:
                result = await generate_stock_video(script, voice_path, aspect_ratio, user_settings)
            except Exception as e:
                logger.warning(f"Stock video generation failed: {e}")

        # Text-on-background fallback
        if not result:
            logger.info("Stock video unavailable — using text-on-background fallback")
            from backend.services.ffmpeg_service import generate_text_video
            result = await generate_text_video(script=script, audio_path=voice_path, aspect_ratio=aspect_ratio)

        return result

    # ── Post-processing helpers ─────────────────────────────────────────

    async def _mix_music(self, voice_path: Path, opts: dict) -> Path:
        """Add background music to voice audio."""
        from backend.services.music_service import select_music, mix_audio

        music_path = await select_music(genre=opts["music_genre"])
        if not music_path:
            return voice_path

        return await mix_audio(
            voice_path=voice_path,
            music_path=music_path,
            music_volume_db=opts.get("music_volume_db", -20.0),
        )

    @staticmethod
    def _get_words_per_group(style: str) -> int:
        from backend.services.caption_service import CAPTION_STYLES
        return CAPTION_STYLES.get(style, {}).get("words_per_group", 3)

    async def _burn_captions(self, video_path: Path, segments: list[dict], aspect_ratio: str, opts: dict) -> Path:
        """Generate and burn word-by-word animated captions."""
        from backend.services.caption_service import generate_captions_ass, burn_captions

        ass_path = await generate_captions_ass(
            segments=segments,
            style=opts["caption_style"],
            aspect_ratio=aspect_ratio,
            emoji_style=opts.get("caption_emoji_style", "moderate"),
        )
        result = await burn_captions(video_path, ass_path)
        # Clean up the actual temp ASS file returned by generate_captions_ass
        # (Wave-1 writes captions_{uuid}.ass, not a fixed name).
        try:
            ass_path.unlink(missing_ok=True)
        except Exception:
            pass
        return result

    async def _generate_metadata(self, script: str, niche: str, user_settings) -> dict:
        """Generate YouTube + TikTok metadata via AI."""
        ai = get_ai_client(user_settings)
        metadata = {"youtube": {}, "tiktok": {}}

        script_preview = script[:500]

        # Fetch search demand for metadata optimization
        search_demand_section = ""
        if niche:
            try:
                from backend.services.youtube_suggest_service import get_search_demand
                demand = await get_search_demand(niche)
                if demand.get("demand_summary"):
                    search_demand_section = (
                        f"YouTube Search Demand Data:\n{demand['demand_summary']}\n"
                        f"Top keywords users search for: {', '.join(demand.get('top_keywords', [])[:10])}"
                    )
            except Exception as e:
                logger.debug(f"Search demand fetch for metadata failed: {e}")

        # YouTube metadata
        try:
            yt_prompt = YOUTUBE_META_PROMPT.format(
                script_preview=script_preview, niche=niche,
                search_demand_section=search_demand_section,
            )
            yt_resp = await ai.chat(messages=[{"role": "user", "content": yt_prompt}], max_tokens=512)
            yt_clean = yt_resp.strip()
            if yt_clean.startswith("```"):
                yt_clean = yt_clean.split("\n", 1)[1].rsplit("```", 1)[0]
            try:
                metadata["youtube"] = json.loads(yt_clean)
            except json.JSONDecodeError as je:
                from backend.core.ai_retry import ai_fix_json
                repaired = await ai_fix_json(yt_clean, str(je), user_settings)
                metadata["youtube"] = repaired if repaired and isinstance(repaired, dict) else {
                    "title": "Untitled Video", "description": script_preview, "tags": []}
        except Exception as e:
            logger.warning(f"YouTube metadata generation failed: {e}")
            metadata["youtube"] = {"title": "Untitled Video", "description": script_preview, "tags": []}

        # TikTok metadata
        try:
            tt_prompt = TIKTOK_META_PROMPT.format(
                script_preview=script_preview, niche=niche,
                search_demand_section=search_demand_section,
            )
            tt_resp = await ai.chat(messages=[{"role": "user", "content": tt_prompt}], max_tokens=256)
            tt_clean = tt_resp.strip()
            if tt_clean.startswith("```"):
                tt_clean = tt_clean.split("\n", 1)[1].rsplit("```", 1)[0]
            try:
                metadata["tiktok"] = json.loads(tt_clean)
            except json.JSONDecodeError as je:
                from backend.core.ai_retry import ai_fix_json
                repaired = await ai_fix_json(tt_clean, str(je), user_settings)
                metadata["tiktok"] = repaired if repaired and isinstance(repaired, dict) else {
                    "title": script_preview[:150], "description": ""}
        except Exception as e:
            logger.warning(f"TikTok metadata generation failed: {e}")
            metadata["tiktok"] = {"title": script_preview[:150], "description": ""}

        return metadata
