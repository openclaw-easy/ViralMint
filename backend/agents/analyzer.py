# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Agent 3b: Whisper transcription + AI insight extraction."""
import asyncio
import json
import logging
from pathlib import Path
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.downloaded_video import DownloadedVideo
from backend.models.user_settings import UserSettings
from backend.core.ai_provider import get_ai_client
from backend.core.ws_manager import ws_manager
from backend.agents.job_helper import update_job_status

logger = logging.getLogger(__name__)

TRANSCRIPT_CORRECTION_PROMPT = """Fix transcription errors in the following speech-to-text output.
Correct spelling, grammar, punctuation, and proper nouns based on context.
Do NOT change the meaning or rephrase sentences — only fix obvious transcription mistakes.
Return the corrected text only, no explanation.

Raw transcript:
{transcript}"""

INSIGHTS_PROMPT = """Analyze this video transcript and extract viral content insights.

Transcript:
{transcript}

Return JSON only (no markdown):
{{
  "hook": "Describe the first 5 seconds strategy",
  "structure": "Break down the video structure",
  "tone": "Describe the speaking style and tone",
  "topic_angle": "What makes this specific angle work",
  "why_viral": "2-3 sentence explanation of why this went viral",
  "suggested_angle": "An original angle for a new video on similar topic",
  "estimated_duration": 90,
  "key_phrases": ["phrase1", "phrase2", "phrase3"],
  "suggested_title": "A click-worthy title idea for the new video",
  "suggested_hooks": ["hook option 1", "hook option 2", "hook option 3"],
  "scores": {{
    "hook_quality": 8,
    "structure_quality": 7,
    "emotional_impact": 6,
    "trend_alignment": 8,
    "production_quality": 7,
    "uniqueness": 5,
    "actionability": 9,
    "composite": 7.1
  }},
  "structural_pattern": "hook-problem-solution or listicle or story-arc or tutorial or reaction",
  "retention_risks": ["timestamp or moment where viewers likely drop off and why"],
  "recreate_prompt": "A single ready-to-run generation brief that recreates this video's WINNING SHAPE for a new topic: name the hook archetype, beat order, pacing, tone, and visual treatment as directives (e.g. 'Open on a bold contrarian claim in the first sentence; 3 rapid proof beats of ~8s each; conversational expert tone; end with a question CTA'). Format + structure only — never reuse this video's wording or topic."
}}

IMPORTANT for the "scores" object:
- Each score is 1-10 (integer). Be honest and critical, not generous.
- hook_quality: How compelling is the first 5 seconds?
- structure_quality: Is the video well-organized with clear progression?
- emotional_impact: Does it evoke emotion (excitement, surprise, empathy, humor)?
- trend_alignment: How aligned with current trending topics/formats?
- production_quality: Audio/visual quality, editing, pacing
- uniqueness: Is the angle original or just copying others?
- actionability: Does the viewer learn something usable?
- composite: weighted average (hook 20%, structure 15%, emotional 15%, trend 15%, production 10%, uniqueness 15%, actionability 10%)"""


SEGMENT_ANALYSIS_PROMPT = """Analyze this video transcript segment-by-segment for retention and virality.

Transcript:
{transcript}

Break the transcript into 5-8 logical segments (intro/hook, each main section, conclusion).
For each segment, score it on 4 dimensions (1-10 integer, be critical):

Return JSON only (no markdown):
{{
  "segments": [
    {{
      "label": "Hook / Opening",
      "start_pct": 0,
      "end_pct": 8,
      "text_preview": "First ~20 words of this segment...",
      "scores": {{
        "hook_strength": 8,
        "emotional_intensity": 7,
        "information_density": 5,
        "pacing_score": 9
      }},
      "retention_risk": "none|low|medium|high",
      "note": "Brief note on what works or what's weak"
    }}
  ],
  "overall_retention_curve": "front-loaded|steady|back-loaded|u-shaped|declining",
  "weakest_segment_index": 2,
  "strongest_segment_index": 0
}}

SCORING GUIDE:
- hook_strength: Does this segment grab/maintain attention? (10=impossible to look away)
- emotional_intensity: Does it evoke a strong emotion? (10=tears/laughter/shock)
- information_density: How much value per second? (10=every word teaches something new)
- pacing_score: Is the rhythm right? (10=perfectly paced, no dead air, no rushing)

Use start_pct and end_pct as percentage of total transcript length (0-100).
Be honest — most segments should score 4-7, not 8-10."""


IMPROVEMENT_PROMPT = """You are a viral video strategist. Based on the analysis below, provide
specific, actionable improvements for someone creating their own version of this content.

Original video transcript (first 2000 chars):
{transcript}

AI analysis of the original:
{insights}

Provide 5 concrete improvements as JSON only (no markdown):
{{
  "improvements": [
    {{
      "category": "hook",
      "priority": "high",
      "original_approach": "What the original video did (be specific with timestamps/quotes)",
      "improvement": "Exact change to make — e.g. 'Open with the result first: show the $10K savings chart in frame 1, THEN explain how'",
      "expected_impact": "Why this works better — reference platform algorithm or viewer psychology"
    }},
    {{
      "category": "pacing",
      "priority": "high",
      "original_approach": "...",
      "improvement": "...",
      "expected_impact": "..."
    }},
    {{
      "category": "angle",
      "priority": "medium",
      "original_approach": "...",
      "improvement": "...",
      "expected_impact": "..."
    }},
    {{
      "category": "cta",
      "priority": "medium",
      "original_approach": "...",
      "improvement": "...",
      "expected_impact": "..."
    }},
    {{
      "category": "visual",
      "priority": "low",
      "original_approach": "...",
      "improvement": "...",
      "expected_impact": "..."
    }}
  ],
  "quick_wins": ["1-sentence actionable tip 1", "1-sentence actionable tip 2", "1-sentence actionable tip 3"],
  "biggest_opportunity": "The single highest-impact change in one sentence"
}}

RULES:
- Categories must be: hook, pacing, angle, cta, visual
- Priority: high (do this first), medium (nice to have), low (polish)
- Be SPECIFIC — reference actual content from the transcript, not generic advice
- "improvement" must be a concrete action, not a vague suggestion
- Each improvement should be different from the others"""


# --- Transcript correction ---------------------------------------------------
# The AI correction pass is best-effort polish: the transcript must NEVER lose
# content because of it. Long transcripts are corrected in sentence-aligned
# chunks (concurrently); a chunk whose correction fails or looks suspicious
# keeps its raw text. Chunks past MAX_CORRECTION_CHUNKS pass through raw to
# bound AI cost/latency on very long videos.
CORRECTION_CHUNK_CHARS = 2500  # stays under max_tokens=4096 even for CJK (~1 token/char)
MAX_CORRECTION_CHUNKS = 6      # ~15k chars ≈ an hour of dense speech
_CORRECTION_CONCURRENCY = 3
_SENTENCE_ENDS = "。！？…!?.\n"


def _split_for_correction(text: str, chunk_chars: int = CORRECTION_CHUNK_CHARS) -> list[str]:
    """Split text into <=chunk_chars pieces, cutting at sentence ends when possible."""
    chunks = []
    i, n = 0, len(text)
    while i < n:
        end = min(i + chunk_chars, n)
        if end < n:
            window = text[i:end]
            cut = max(window.rfind(c) for c in _SENTENCE_ENDS)
            if cut > chunk_chars // 2:
                end = i + cut + 1
        chunks.append(text[i:end])
        i = end
    return chunks


async def _ai_correct_transcript(user_settings, raw_text: str) -> str:
    """Best-effort AI cleanup of a Whisper transcript. AI failures never raise
    and never lose content — worst case is the raw text back."""
    chunks = _split_for_correction(raw_text)
    to_correct = chunks[:MAX_CORRECTION_CHUNKS]
    passthrough = chunks[MAX_CORRECTION_CHUNKS:]
    if passthrough:
        logger.info(
            "Transcript correction: correcting %d/%d chunks (%d chars pass through raw)",
            len(to_correct), len(chunks), sum(len(c) for c in passthrough),
        )

    try:
        ai = get_ai_client(user_settings)
    except Exception as e:
        logger.warning(f"AI transcript correction unavailable: {e} — using raw Whisper output")
        return raw_text
    sem = asyncio.Semaphore(_CORRECTION_CONCURRENCY)

    async def _correct_one(chunk: str) -> str:
        async with sem:
            corrected = await ai.chat(
                messages=[{"role": "user", "content": TRANSCRIPT_CORRECTION_PROMPT.format(
                    transcript=chunk.strip()
                )}],
                max_tokens=4096,
            )
        corrected = (corrected or "").strip()
        if corrected and 0.5 * len(chunk) <= len(corrected) <= 2 * len(chunk) + 50:
            return corrected
        logger.warning(
            "Transcript correction chunk looked suspicious (%d -> %d chars) — keeping raw",
            len(chunk), len(corrected),
        )
        return chunk.strip()

    results = await asyncio.gather(
        *(_correct_one(c) for c in to_correct), return_exceptions=True
    )
    out = []
    for chunk, res in zip(to_correct, results):
        if isinstance(res, BaseException):
            logger.warning(f"Transcript correction chunk failed: {res} — keeping raw")
            out.append(chunk.strip())
        else:
            out.append(res)
    out.extend(c.strip() for c in passthrough)
    return "\n\n".join(p for p in out if p)


class AnalyzerAgent:
    async def run(
        self,
        job_id: str,
        user_id: str = "local",
    ):
        """Transcribe and analyze all downloaded videos for this job."""
        logger.info("ANALYZER START | job=%s user=%s", job_id[:8], user_id)
        # Get all downloaded videos from this job's downloads
        async with AsyncSessionLocal() as db:
            from backend.models.scout_result import ScoutResult
            # Find downloaded videos that haven't been analyzed
            result = await db.execute(
                select(DownloadedVideo)
                .where(DownloadedVideo.user_id == user_id)
                .where(DownloadedVideo.transcript == None)
                .order_by(DownloadedVideo.created_at.desc())
                .limit(20)
            )
            videos = result.scalars().all()

        if not videos:
            logger.info("No unanalyzed videos found")
            return

        # Load user settings for whisper quality and AI
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            user_settings = result.scalar_one_or_none()

        whisper_quality = "balanced"

        total = len(videos)
        failed_count = 0
        for i, dv in enumerate(videos):
            step = f"Analyzing video {i + 1}/{total}"
            await ws_manager.send_progress(job_id, (i / total) * 100, step, user_id)

            try:
                logger.info("ANALYZER video %d/%d | id=%s audio=%s", i + 1, total, dv.id[:8], dv.audio_path or dv.video_path)

                # Guard: skip already-analyzed videos (insights already exist)
                if dv.insights_json and dv.transcript:
                    logger.info(f"Skipping already-analyzed video {dv.id[:8]}")
                    continue

                # Step 1: Get transcript — prefer existing subtitles over Whisper
                transcript_data = None
                transcript_source = None

                # Check if subtitles were already downloaded with the video
                if dv.transcript and dv.transcript_source in ("creator_subtitles", "auto_subtitles"):
                    # Subtitles already stored during download — reuse them
                    transcript_data = {"text": dv.transcript, "language": dv.transcript_language}
                    transcript_source = dv.transcript_source
                    logger.info(f"Using pre-downloaded subtitles for {dv.id} (source={transcript_source})")
                else:
                    # No subtitles available — fall back to Whisper transcription
                    audio_path = dv.audio_path or dv.video_path
                    if not audio_path or not Path(audio_path).exists():
                        logger.warning(f"No audio/video file for {dv.id} — skipping transcription")
                        continue

                    from backend.services.whisper_service import whisper_service
                    await asyncio.to_thread(whisper_service.load, whisper_quality)
                    transcript_data = await whisper_service.transcribe(audio_path)
                    transcript_source = "whisper"

                    # AI post-correction of Whisper transcript (chunked; never loses content)
                    raw_text = transcript_data.get("text", "")
                    if raw_text:
                        corrected = await _ai_correct_transcript(user_settings, raw_text)
                        if corrected and corrected != raw_text:
                            transcript_data["text"] = corrected
                            logger.info(f"AI-corrected transcript for {dv.id}")

                # Step 2: AI insight extraction (include chapters context if available)
                insights_json = None
                if transcript_data.get("text"):
                    try:
                        ai = get_ai_client(user_settings)

                        # Build enhanced prompt with chapters if available
                        transcript_text = transcript_data["text"][:3000]
                        chapters_context = ""
                        if dv.chapters_json:
                            from backend.core.exceptions import safe_json_loads
                            chapters = safe_json_loads(dv.chapters_json, [], logger)
                            if chapters:
                                chapter_lines = [
                                    f"  {ch['start']:.0f}s - {ch['end']:.0f}s: {ch['title']}"
                                    for ch in chapters
                                    if isinstance(ch, dict) and all(k in ch for k in ("start", "end", "title"))
                                ]
                                if chapter_lines:
                                    chapters_context = "\n\nVideo chapters (creator's own structure):\n" + "\n".join(chapter_lines)

                        prompt = INSIGHTS_PROMPT.format(
                            transcript=transcript_text + chapters_context,
                        )
                        response = await ai.chat(
                            messages=[{"role": "user", "content": prompt}],
                            max_tokens=1024,
                        )
                        # Parse JSON from response
                        response_clean = response.strip()
                        if response_clean.startswith("```"):
                            response_clean = response_clean.split("\n", 1)[1].rsplit("```", 1)[0]
                        try:
                            insights = json.loads(response_clean)
                        except json.JSONDecodeError as je:
                            # AI returned malformed JSON — ask AI to repair it
                            logger.warning(f"Insight JSON parse failed for {dv.id}, attempting AI repair...")
                            from backend.core.ai_retry import ai_fix_json
                            insights = await ai_fix_json(response_clean, str(je), user_settings)
                        if insights and isinstance(insights, dict):
                            insights_json = json.dumps(insights)
                    except Exception as e:
                        logger.warning(f"AI insight extraction failed for {dv.id}: {e}")

                # Step 3: Segment-level scored analysis (non-critical — timeout after 90s)
                segment_analysis_json = None
                if transcript_data.get("text") and len(transcript_data["text"]) > 200:
                    try:
                        async def _segment_analysis():
                            ai = get_ai_client(user_settings)
                            seg_prompt = SEGMENT_ANALYSIS_PROMPT.format(
                                transcript=transcript_data["text"][:4000],
                            )
                            seg_response = await ai.chat(
                                messages=[{"role": "user", "content": seg_prompt}],
                                max_tokens=1024,
                            )
                            seg_clean = seg_response.strip()
                            if seg_clean.startswith("```"):
                                seg_clean = seg_clean.split("\n", 1)[1].rsplit("```", 1)[0]
                            try:
                                return json.loads(seg_clean)
                            except json.JSONDecodeError as je:
                                from backend.core.ai_retry import ai_fix_json
                                return await ai_fix_json(seg_clean, str(je), user_settings)

                        seg_data = await asyncio.wait_for(_segment_analysis(), timeout=90)
                        if seg_data and isinstance(seg_data, dict):
                            segment_analysis_json = json.dumps(seg_data)
                    except asyncio.TimeoutError:
                        logger.warning(f"Segment analysis timed out for {dv.id[:8]} (90s)")
                    except Exception as e:
                        logger.warning(f"Segment analysis failed for {dv.id}: {e}")

                # Step 4: Actionable improvement suggestions (non-critical — timeout after 90s)
                improvement_json = None
                if insights_json and transcript_data.get("text"):
                    try:
                        async def _improvement_suggestions():
                            ai = get_ai_client(user_settings)
                            imp_prompt = IMPROVEMENT_PROMPT.format(
                                transcript=transcript_data["text"][:2000],
                                insights=insights_json[:2000],
                            )
                            imp_response = await ai.chat(
                                messages=[{"role": "user", "content": imp_prompt}],
                                max_tokens=1024,
                            )
                            imp_clean = imp_response.strip()
                            if imp_clean.startswith("```"):
                                imp_clean = imp_clean.split("\n", 1)[1].rsplit("```", 1)[0]
                            try:
                                return json.loads(imp_clean)
                            except json.JSONDecodeError as je:
                                from backend.core.ai_retry import ai_fix_json
                                return await ai_fix_json(imp_clean, str(je), user_settings)

                        imp_data = await asyncio.wait_for(_improvement_suggestions(), timeout=90)
                        if imp_data and isinstance(imp_data, dict):
                            improvement_json = json.dumps(imp_data)
                    except asyncio.TimeoutError:
                        logger.warning(f"Improvement suggestions timed out for {dv.id[:8]} (90s)")
                    except Exception as e:
                        logger.warning(f"Improvement suggestions failed for {dv.id}: {e}")

                # Step 5: Fetch and analyze comments (non-critical — timeout after 120s)
                comments_json_str = None
                comment_insights_json = None
                if dv.platform == "youtube" and dv.scout_result_id:
                    try:
                        async def _comment_analysis():
                            # Get the video_id from scout result
                            async with AsyncSessionLocal() as db:
                                from backend.models.scout_result import ScoutResult as SR
                                sr_res = await db.execute(select(SR).where(SR.id == dv.scout_result_id))
                                sr_obj = sr_res.scalar_one_or_none()
                                yt_video_id = sr_obj.video_id if sr_obj else None

                            if not yt_video_id:
                                return None, None

                            from backend.services.comment_service import (
                                fetch_youtube_comments, analyze_comments,
                            )
                            from backend.core.api_keys import get_youtube_api_key
                            yt_key = get_youtube_api_key(user_settings)

                            if not yt_key:
                                return None, None

                            comments = await fetch_youtube_comments(yt_video_id, yt_key, max_comments=30)
                            if not comments:
                                return None, None

                            c_json = json.dumps(comments)
                            ai = get_ai_client(user_settings)
                            ci = await analyze_comments(
                                comments, transcript_data.get("text", ""), ai,
                            )
                            ci_json = json.dumps(ci) if ci else None
                            return c_json, ci_json

                        comments_json_str, comment_insights_json = await asyncio.wait_for(
                            _comment_analysis(), timeout=120,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(f"Comment analysis timed out for {dv.id[:8]} (120s)")
                    except Exception as e:
                        logger.warning(f"Comment extraction failed for {dv.id}: {e}")

                # Save to DB
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(DownloadedVideo).where(DownloadedVideo.id == dv.id)
                    )
                    row = result.scalar_one_or_none()
                    if row:
                        row.transcript = transcript_data.get("text", "")
                        row.transcript_language = transcript_data.get("language")
                        row.transcript_source = transcript_source
                        # Persist word-level timestamps for clip extraction
                        if transcript_data.get("segments") and not row.transcript_segments_json:
                            row.transcript_segments_json = json.dumps(transcript_data["segments"])
                        if insights_json:
                            row.insights_json = insights_json
                        if segment_analysis_json:
                            row.segment_analysis_json = segment_analysis_json
                        if improvement_json:
                            row.improvement_suggestions_json = improvement_json
                        if comments_json_str:
                            row.comments_json = comments_json_str
                        if comment_insights_json:
                            row.comment_insights_json = comment_insights_json

                        # Mark scout result as analyzed
                        if row.scout_result_id:
                            from backend.models.scout_result import ScoutResult
                            sr_result = await db.execute(
                                select(ScoutResult).where(ScoutResult.id == row.scout_result_id)
                            )
                            sr = sr_result.scalar_one_or_none()
                            if sr:
                                sr.is_analyzed = True

                        await db.commit()

            except Exception as e:
                failed_count += 1
                logger.error(f"Analysis failed for video {dv.id}: {e}")
                await ws_manager.send_constraint_warning(
                    constraint="analysis",
                    message=f"Analysis failed: {e}",
                    severity="warning",
                    user_id=user_id,
                )

        succeeded = total - failed_count
        if succeeded == 0 and total > 0:
            logger.warning("ANALYZER DONE | job=%s | ALL %d videos failed", job_id[:8], total)
            await update_job_status(
                job_id, "failed",
                progress_pct=100,
                current_step=f"All {total} videos failed analysis",
                error_message=f"Failed to analyze any of {total} videos",
            )
        else:
            step_msg = f"Analyzed {succeeded}/{total} videos" if failed_count else f"Analyzed {total} videos"
            logger.info("ANALYZER DONE | job=%s | %s", job_id[:8], step_msg)
            await update_job_status(
                job_id, "success",
                progress_pct=100,
                current_step=step_msg,
            )

    async def reanalyze_single(
        self,
        job_id: str,
        video_id: str,
        whisper_quality: str = "balanced",
        user_id: str = "local",
    ):
        """Re-transcribe and re-analyze a single downloaded video with a specific Whisper model."""
        logger.info("RE-ANALYZE START | job=%s video=%s quality=%s", job_id[:8], video_id[:8], whisper_quality)

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DownloadedVideo).where(DownloadedVideo.id == video_id)
            )
            dv = result.scalar_one_or_none()
        if not dv:
            await update_job_status(job_id, "failed", error_message="Video not found")
            return

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            user_settings = result.scalar_one_or_none()

        try:
            audio_path = dv.audio_path or dv.video_path
            if not audio_path or not Path(audio_path).exists():
                await update_job_status(job_id, "failed", error_message="No audio/video file found")
                return

            # Check if model needs downloading first
            from backend.services.whisper_service import whisper_service, WHISPER_QUALITY_MAP, WHISPER_MODEL_SIZES
            model_name = WHISPER_QUALITY_MAP.get(whisper_quality, "small")
            if not whisper_service.is_model_cached(whisper_quality):
                size = WHISPER_MODEL_SIZES.get(model_name, "")
                step = f"Downloading Whisper {model_name} model ({size}) — first time only, please wait..."
                await update_job_status(job_id, "running", progress_pct=5, current_step=step)
                await ws_manager.send_progress(job_id, 5, step, user_id)
            else:
                await ws_manager.send_progress(job_id, 10, "Transcribing with Whisper...", user_id)
                await update_job_status(job_id, "running", progress_pct=10, current_step="Transcribing with Whisper...")

            # Load model in thread (may download ~3GB on first use — must not block event loop)
            await asyncio.to_thread(whisper_service.load, whisper_quality)
            await ws_manager.send_progress(job_id, 15, "Transcribing audio...", user_id)
            await update_job_status(job_id, "running", progress_pct=15, current_step="Transcribing audio...")
            transcript_data = await whisper_service.transcribe(audio_path)

            await ws_manager.send_progress(job_id, 40, "Correcting transcript...", user_id)

            # AI post-correction (chunked; never loses content)
            raw_text = transcript_data.get("text", "")
            if raw_text:
                corrected = await _ai_correct_transcript(user_settings, raw_text)
                if corrected and corrected != raw_text:
                    transcript_data["text"] = corrected

            await ws_manager.send_progress(job_id, 70, "Extracting insights...", user_id)

            # AI insight extraction
            insights_json = None
            if transcript_data.get("text"):
                try:
                    ai = get_ai_client(user_settings)
                    prompt = INSIGHTS_PROMPT.format(
                        transcript=transcript_data["text"][:3000],
                    )
                    response = await ai.chat(
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=1024,
                    )
                    response_clean = response.strip()
                    if response_clean.startswith("```"):
                        response_clean = response_clean.split("\n", 1)[1].rsplit("```", 1)[0]
                    try:
                        insights = json.loads(response_clean)
                    except json.JSONDecodeError as je:
                        from backend.core.ai_retry import ai_fix_json
                        insights = await ai_fix_json(response_clean, str(je), user_settings)
                    if insights and isinstance(insights, dict):
                        insights_json = json.dumps(insights)
                except Exception as e:
                    logger.warning(f"AI insight extraction failed: {e}")

            # Save to DB
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(DownloadedVideo).where(DownloadedVideo.id == video_id)
                )
                row = result.scalar_one_or_none()
                if row:
                    row.transcript = transcript_data.get("text", "")
                    row.transcript_language = transcript_data.get("language")
                    if transcript_data.get("segments"):
                        row.transcript_segments_json = json.dumps(transcript_data["segments"])
                    if insights_json:
                        row.insights_json = insights_json
                    await db.commit()

            logger.info("RE-ANALYZE DONE | video=%s quality=%s", video_id[:8], whisper_quality)
            await update_job_status(job_id, "success", progress_pct=100, current_step="Re-analysis complete")
            await ws_manager.send({
                "type": "job_complete",
                "job_id": job_id,
                "result": {"video_id": video_id, "whisper_quality": whisper_quality},
            }, user_id)

        except Exception as e:
            logger.error(f"Re-analysis failed for {video_id}: {e}")
            await update_job_status(job_id, "failed", error_message=str(e))
            await ws_manager.send({
                "type": "job_failed",
                "job_id": job_id,
                "error": str(e),
            }, user_id)
