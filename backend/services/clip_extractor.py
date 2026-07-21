# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Viral clip extraction service.
Takes a long-form downloaded video and identifies the best 30-60s segments
for YouTube Shorts / TikTok / Reels.

Features:
- AI-powered clip window selection with platform-aware prompts
- Chunk-based extraction for large requests (splits video into time windows)
- Per-clip virality scoring (1-10)
- Parallel clip processing (extract + caption + thumbnail)
- Intermediate file cleanup
- Robust error handling with status tracking
"""
import asyncio
import json
import logging
import math
import re
from pathlib import Path
from uuid import uuid4

from backend.config import settings
from backend.core.concurrency import _ffmpeg_semaphore
from backend.core.ws_manager import ws_manager

logger = logging.getLogger(__name__)


async def _ffmpeg_limited(coro):
    """Wrap an ffmpeg-using coroutine in the global ffmpeg-work semaphore so
    a single big extract (e.g. 18 clips) can't fire 18 parallel ffmpegs at
    once. Without this, heavy jobs peg the CPU and even fast UI requests
    queue behind the system load — see _ffmpeg_semaphore in
    backend.core.concurrency for the full reasoning. Coroutines are passed
    pre-built; they don't actually start until awaited inside the sem.
    """
    async with _ffmpeg_semaphore:
        return await coro

# ── AI Prompts ────────────────────────────────────────────────────────────────

CLIP_SELECTION_PROMPT = """You are an expert viral short-form video editor who has produced thousands of clips with millions of views.

Your job: identify the best non-overlapping segments ({min_clip}–{max_clip} seconds each) from this transcript. Find up to {max_clips} quality clips. Only include segments that genuinely meet the criteria below — never pad with weak content.

SELECTION CRITERIA (in priority order):
1. HOOK STRENGTH: The first 2-3 seconds of the segment must grab attention immediately. Look for: surprising statements, bold claims, questions, emotional peaks, "did you know" moments.
2. STANDALONE VALUE: Each segment must make complete sense without the rest of the video. No dangling references, no "as I mentioned earlier".
3. INFORMATION DENSITY: Every 10 seconds should advance the narrative. Cut segments that meander or repeat.
4. CLEAN BOUNDARIES: Start at natural sentence beginnings (not mid-word). End at natural conclusions (not cliffhangers that need the next sentence).
5. EMOTIONAL ARC: Prefer segments with a clear setup → payoff structure. "Aha moments", revelations, or actionable advice.
6. QUOTABILITY: Would someone share this clip? Would it spark comments or debates?

WHAT TO AVOID:
- Intros ("hey guys, welcome to my channel")
- Outros ("don't forget to subscribe")
- Slow, repetitive sections with low information density
- Segments that reference visuals not captured in audio ("as you can see on screen")
- Incomplete thoughts or arguments that require context

Video title: {title}
Video duration: {duration}s
Video niche/topic: {niche}

Transcript with timestamps:
{segments_text}

Return ONLY valid JSON array, no markdown fences:
[
  {{
    "start": 45.2,
    "end": 102.8,
    "title": "Short punchy title for this clip (5-8 words)",
    "hook": "The exact first sentence that grabs attention",
    "reason": "Why this segment works as a viral clip (be specific)",
    "virality_score": 8.5
  }}
]

IMPORTANT:
- virality_score must be 1-10 (10 = guaranteed viral, 1 = boring)
- Each segment MUST be between {min_clip} and {max_clip} seconds
- Segments must NOT overlap
- Order by virality_score descending (best clip first)
- You MUST return at least 1 clip if there is any usable content"""

CLIP_METADATA_PROMPT = """Generate platform metadata for this viral short clip.

Clip title: {title}
Clip transcript:
{transcript_text}

Return ONLY valid JSON, no markdown fences:
{{
  "youtube_title": "Catchy YouTube Shorts title (under 70 chars, front-load keywords, use power words)",
  "youtube_description": "2-3 sentence description with relevant keywords for SEO",
  "youtube_tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "tiktok_title": "TikTok caption with hashtags (under 150 chars, hook-first)"
}}"""


CLIP_METADATA_BATCH_PROMPT = """Generate platform metadata for {count} viral short clips. Each clip is independent — write metadata tailored to that clip's specific transcript and title.

Clips:
{clips_block}

Return ONLY a valid JSON array with one object per clip (same order as input). Each object MUST include the `index` field matching the clip's number (0-based). No markdown fences.

[
  {{
    "index": 0,
    "youtube_title": "Catchy YouTube Shorts title (under 70 chars, front-load keywords, use power words)",
    "youtube_description": "2-3 sentence description with relevant keywords for SEO",
    "youtube_tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
    "tiktok_title": "TikTok caption with hashtags (under 150 chars, hook-first)"
  }},
  ...
]

IMPORTANT:
- Return exactly {count} objects, one per input clip
- The `index` field MUST match the input clip number — do not reorder or skip
- Each object MUST have ALL five fields (index + 4 metadata fields)
- If a clip's transcript is empty, base the metadata on the title alone"""


# ── Clip count estimation ──────────────────────────────────────────────────────

def _has_cjk(text: str) -> bool:
    """Check if text contains CJK characters (Chinese, Japanese, Korean)."""
    import unicodedata
    for ch in text[:500]:  # sample first 500 chars
        try:
            name = unicodedata.name(ch, "")
            if "CJK" in name or "HANGUL" in name or "HIRAGANA" in name or "KATAKANA" in name:
                return True
        except ValueError:
            continue
    return False


def _count_speech_units(text: str) -> int:
    """
    Count speech units in a language-aware way.
    For CJK languages (Chinese, Japanese, Korean), count characters instead of
    space-separated words since these languages don't use spaces.
    For alphabetic languages, count words normally.
    """
    if not text:
        return 0
    if _has_cjk(text):
        # CJK: count characters (excluding spaces and common punctuation)
        import re
        chars = re.sub(r'[\s.,!?;:\-\"\'\(\)\[\]，。！？；：、""''（）《》【】…～·]', '', text)
        return len(chars)
    return len(text.split())


def _estimate_realistic_clip_count(
    segments: list[dict],
    duration: int,
    requested_max: int,
    min_duration: int | None = None,
) -> int:
    """
    Estimate how many clips can realistically be extracted, based on transcript
    content density and video length.

    Language-aware: handles CJK (Chinese/Japanese/Korean) where text has no
    spaces by counting characters instead of words.

    `min_duration` MUST match the user's requested minimum clip length (the
    same value passed to the AI prompt as `min_clip`). Without it the
    estimator silently assumed every clip would be ~40s long, which capped
    a 1-minute source asking for 3×15s clips down to 1 — bug observed
    2026-04-30 with a 63-second video. The fix divides by the actual
    minimum, not an internal average.
    """
    if not segments or duration <= 0:
        return min(requested_max, 3)

    # Same default the AI prompt uses (see `_select_clip_windows*` callers).
    # Anything shorter than 10s makes for unwatchable shorts; floor it.
    min_clip_floor = max(10, int(min_duration)) if min_duration else 15

    # Calculate speech density
    total_text = " ".join(s.get("text", "") for s in segments)
    is_cjk = _has_cjk(total_text)
    speech_units = _count_speech_units(total_text)

    # Speech coverage: what fraction of video has speech
    speech_duration = sum(s.get("end", 0) - s.get("start", 0) for s in segments)
    speech_coverage = speech_duration / max(duration, 1)

    # Content density floor — how many minimum-length clips' worth of speech
    # we have. CJK speech rate ~4.5 chars/sec; English ~2.5 words/sec.
    units_per_sec = 4.5 if is_cjk else 2.5
    units_per_min_clip = max(1, int(min_clip_floor * units_per_sec))
    content_based_max = max(1, speech_units // units_per_min_clip)

    # Duration floor — how many minimum-length non-overlapping clips fit.
    duration_based_max = max(1, duration // min_clip_floor)

    # Use the more restrictive estimate.
    realistic = min(content_based_max, duration_based_max, requested_max)

    lang_label = "CJK chars" if is_cjk else "words"
    logger.info(
        f"Clip count estimation: {speech_units} {lang_label}, "
        f"{speech_coverage:.0%} speech coverage, min_clip_floor={min_clip_floor}s → "
        f"content_max={content_based_max}, duration_max={duration_based_max}, "
        f"requested={requested_max}, final={realistic}"
    )

    return max(1, realistic)


# ── Main pipeline ──────────────────────────────────────────────────────────────

async def extract_viral_clips(
    video,
    user_settings,
    max_clips: int = 3,
    caption_style: str = "viral",
    whisper_quality: str = "balanced",
    force_retranscribe: bool = False,
    job_id: str = None,
    user_id: str = "local",
    min_duration: int = None,
    max_duration: int = None,
) -> list[dict]:
    """
    Full clip extraction pipeline.
    Returns list of dicts with video_path, title, metadata, status flags, etc.
    """
    # Step 1: Load or transcribe segments (10%)
    if job_id:
        await ws_manager.send_progress(job_id, 5, "Loading transcript...", user_id)
    segments = await _load_or_transcribe_segments(
        video, user_settings, whisper_quality=whisper_quality,
        force_retranscribe=force_retranscribe, job_id=job_id, user_id=user_id,
    )

    duration = video.duration_seconds or 0

    # ── Clip-window selection ──────────────────────────────────────────
    # Two branches, in priority order:
    #   1. Short-video    — emit the whole source as a single clip.
    #   2. AI selection   — transcript-aware picker (or no-speech fallback).
    #
    # Each branch produces a `clip_windows` list that the shared
    # _process_clips_parallel block below consumes.
    #
    # Short-video fast-path background: below SHORT_VIDEO_THRESHOLD the AI
    # can't pick multiple non-overlapping clips, and users usually want the
    # whole thing anyway. Saves an AI call and avoids the "3 clips from 25s
    # = overlapping garbage" failure mode. Users sometimes want to repurpose
    # a short clip they already have (TikTok B-roll, a reaction, a tight
    # Reel) so this path carries them through to a captioned mp4.
    SHORT_VIDEO_THRESHOLD = 20
    if 0 < duration < SHORT_VIDEO_THRESHOLD:
        logger.info(
            f"Short video ({duration}s) — emitting single whole-video clip, skipping AI selection"
        )
        if job_id:
            await ws_manager.send_progress(
                job_id, 15,
                f"Short video ({int(duration)}s) — using as a single clip...",
                user_id,
            )
        clip_windows = [{
            "start": 0.0,
            "end": float(duration),
            "title": video.title or "Full clip",
            "hook": "",
            "reason": "Video is short; emitted as a single clip.",
            "virality_score": 5.0,
            "hook_score": 5.0,
        }]
        # Keep segments as-is so captions still render; _process_clips_parallel
        # handles empty segments for silent inputs via the existing no-segments
        # path in _burn_clip_captions.
    else:
        # Check if the video has usable transcript. Whisper returns
        # SUCCESSFULLY with an empty `segments` list (or almost no text) when
        # the source is music/silence — that's the genuine no-speech case and
        # routes to the duration-based fallback below. A Whisper *crash* never
        # reaches here: `_load_or_transcribe_segments` re-raises it (Wave-1
        # raise-on-crash), so the job fails loudly instead of silently
        # downgrading to random time-cut clips.
        has_transcript = bool(segments) and len(" ".join(s.get("text", "") for s in segments).strip()) >= 50

        if has_transcript:
            # ── Normal path: AI-driven clip selection from transcript ──
            # Auto-scale max_clips based on actual content.
            effective_max = _estimate_realistic_clip_count(
                segments, duration, max_clips, min_duration=min_duration,
            )
            if effective_max < max_clips:
                logger.info(f"Scaled max_clips from {max_clips} to {effective_max} based on content analysis")

            # Step 2: AI selects best clip windows (15-30%)
            if job_id:
                await ws_manager.send_progress(job_id, 15, "AI analyzing transcript for viral moments...", user_id)

            # Use chunk-based extraction for large requests
            if effective_max > 8 and duration > 300:
                clip_windows = await _chunked_clip_selection(
                    segments, video.title or "Untitled", duration,
                    effective_max, user_settings,
                    min_duration=min_duration, max_duration=max_duration,
                    job_id=job_id, user_id=user_id,
                )
            else:
                clip_windows = await _select_clip_windows_with_retries(
                    segments, video.title or "Untitled", duration,
                    effective_max, user_settings,
                    min_duration=min_duration, max_duration=max_duration,
                    job_id=job_id, user_id=user_id,
                )

            if not clip_windows:
                # AI returned nothing after retries — either content too sparse,
                # AI hit a refusal mood, or the user's range is genuinely too
                # tight. Rather than failing the job, fall back to evenly-spaced
                # duration-based clips so the user still gets a deliverable.
                #
                # Use the user's original `max_clips`, not the density-scaled
                # `effective_max`: once we've decided to fall back, the
                # estimator's content-density reasoning no longer applies —
                # we're slicing time, not narrative.
                #
                # Captions are cleared (segments = []) because the AI's verdict
                # is that there's no narrative worth tracking word-by-word.
                logger.info(
                    f"AI returned no clips for {video.id[:8]} after retries "
                    f"(video: {duration}s, {len(segments)} segments) — "
                    f"falling back to duration-based split "
                    f"(range {min_duration or 'auto'}-{max_duration or 'auto'}s, "
                    f"max={max_clips})"
                )
                if job_id:
                    await ws_manager.send_progress(
                        job_id, 28,
                        "AI found no narrative clips — splitting by duration instead...",
                        user_id,
                    )
                clip_windows = _generate_duration_based_clips(
                    duration, max_clips,
                    min_duration=min_duration, max_duration=max_duration,
                    title=video.title or "Untitled",
                )
                segments = []
                if not clip_windows:
                    if min_duration is not None and max_duration is not None:
                        raise ValueError(
                            f"No clips fit your {min_duration}-{max_duration}s range "
                            f"(video: {duration}s). "
                            f"Try widening the duration range or lowering the min."
                        )
                    raise ValueError(
                        f"Could not extract any clips from this video "
                        f"(duration: {duration}s). "
                        f"The source may be too short for the requested range — "
                        f"try lowering the min duration or using a longer video."
                    )
            else:
                # AI succeeded — apply the two refinements that only make sense
                # when we have real narrative-aligned windows:
                #
                # 1. Sentence-snap. Whisper segments break at sentence-ish
                #    utterances, so nudging each clip's start/end ±2s onto the
                #    nearest segment edge fixes "cut mid-word / before the
                #    punchline".
                #
                # 2. Silent-region backfill. Add un-spoken stretches of the
                #    source up to the user's original `max_clips` cap so the
                #    output covers more of the video. Additive only.
                clip_windows = _snap_to_sentence_boundaries(clip_windows, segments)
                if len(clip_windows) < max_clips:
                    silent_windows = _find_silent_gaps(
                        segments=segments,
                        clip_windows=clip_windows,
                        duration=duration,
                        min_gap_duration=float(min_duration) if min_duration else 10.0,
                        max_clip_duration=float(max_duration or 60),
                        budget=max_clips - len(clip_windows),
                    )
                    if silent_windows:
                        logger.info(
                            f"Adding {len(silent_windows)} silent-region clip(s) "
                            f"to AI's {len(clip_windows)} speech clips (max={max_clips})"
                        )
                        clip_windows = list(clip_windows) + silent_windows
        else:
            # ── No-speech fallback: split video into even duration-based clips ──
            # This REPLACES the old hard-raise `_validate_transcript` for the
            # genuine no-speech case (transcription succeeded but returned no
            # usable speech). The user gets a deliverable instead of an error.
            logger.info(f"No transcript for {video.id[:8]} — using duration-based clip splitting")
            if job_id:
                await ws_manager.send_progress(job_id, 15, "No speech detected — splitting by duration...", user_id)

            clip_windows = _generate_duration_based_clips(
                duration, max_clips,
                min_duration=min_duration, max_duration=max_duration,
                title=video.title or "Untitled",
            )
            # Clear segments so caption step is skipped for no-speech clips
            segments = []
            if not clip_windows:
                raise ValueError(
                    f"Could not extract any clips from this video "
                    f"(duration: {duration}s, no usable speech). "
                    f"The source may be too short for the requested range — "
                    f"try lowering the min duration or using a longer video."
                )

    logger.info(f"AI selected {len(clip_windows)} clip windows from {video.id[:8]}")

    # Step 3: Process all clips in PARALLEL (30-95%)
    if job_id:
        await ws_manager.send_progress(job_id, 30, f"Processing {len(clip_windows)} clips (extracting, captioning, thumbnails)...", user_id)

    results = await _process_clips_parallel(
        video=video,
        clip_windows=clip_windows,
        segments=segments,
        caption_style=caption_style,
        user_settings=user_settings,
        job_id=job_id,
        user_id=user_id,
    )

    if not results:
        raise ValueError(
            f"All {len(clip_windows)} clip extractions failed during video processing. "
            f"This may indicate a corrupt source video or FFmpeg issue. "
            f"Try re-downloading the video or check that the video file plays correctly."
        )

    logger.info(f"Successfully extracted {len(results)}/{len(clip_windows)} clips from {video.id[:8]}")
    return results


def _generate_duration_based_clips(
    duration: int,
    max_clips: int,
    min_duration: int = None,
    max_duration: int = None,
    title: str = "Untitled",
) -> list[dict]:
    """
    Generate evenly-spaced clip windows for videos without speech.
    Falls back to splitting by duration rather than transcript analysis.
    """
    clip_len = max_duration or 45
    min_len = min_duration or 15
    clip_len = max(min_len, min(clip_len, duration))

    if duration <= clip_len:
        # Whole video is one clip
        return [{
            "start": 0,
            "end": duration,
            "title": f"{title} — Full clip",
            "hook": "",
            "score": 5,
        }]

    # How many clips fit without overlap
    possible = max(1, int(duration // clip_len))
    num_clips = min(possible, max_clips)

    # Space them evenly across the video
    if num_clips == 1:
        # Take from the start
        return [{
            "start": 0,
            "end": clip_len,
            "title": f"{title} — Clip 1",
            "hook": "",
            "score": 5,
        }]

    gap = (duration - num_clips * clip_len) / max(num_clips - 1, 1)
    clips = []
    pos = 0.0
    for i in range(num_clips):
        start = round(pos, 1)
        end = round(min(start + clip_len, duration), 1)
        if end - start < min_len:
            break
        clips.append({
            "start": start,
            "end": end,
            "title": f"{title} — Clip {i + 1}",
            "hook": "",
            "score": 5,
        })
        pos = end + gap

    logger.info(f"Generated {len(clips)} duration-based clips ({clip_len}s each) from {duration}s video")
    return clips


def _find_silent_gaps(
    segments: list[dict],
    clip_windows: list[dict],
    duration: float,
    min_gap_duration: float = 10.0,
    max_clip_duration: float = 60.0,
    budget: int | None = None,
) -> list[dict]:
    """
    Return clip-window dicts covering stretches of the source video that have
    no speech AND don't overlap any AI-selected clip.

    Short silent pauses (< min_gap_duration) are left to be absorbed into
    neighbouring speech clips. Long silent stretches are split into chunks of
    at most max_clip_duration so individual clips stay playable. When budget
    is given, emission stops once that many windows have been produced.
    """
    if duration <= 0:
        return []

    covered: list[tuple[float, float]] = []
    for s in segments:
        start = float(s.get("start", 0))
        end = float(s.get("end", 0))
        if end > start:
            covered.append((start, end))
    for w in clip_windows:
        start = float(w.get("start", 0))
        end = float(w.get("end", 0))
        if end > start:
            covered.append((start, end))

    # Fully silent videos are handled upstream by _generate_duration_based_clips.
    if not covered:
        return []

    covered.sort()
    merged: list[list[float]] = [list(covered[0])]
    for s, e in covered[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    gaps: list[tuple[float, float]] = []
    prev_end = 0.0
    for s, e in merged:
        if s - prev_end >= min_gap_duration:
            gaps.append((prev_end, s))
        prev_end = e
    if duration - prev_end >= min_gap_duration:
        gaps.append((prev_end, duration))

    if not gaps:
        return []

    result: list[dict] = []
    for g_start, g_end in gaps:
        gap_len = g_end - g_start
        if gap_len <= max_clip_duration:
            ranges = [(g_start, g_end)]
        else:
            n = math.ceil(gap_len / max_clip_duration)
            chunk = gap_len / n
            ranges = [(g_start + i * chunk, g_start + (i + 1) * chunk) for i in range(n)]
        for r_start, r_end in ranges:
            if r_end - r_start < min_gap_duration:
                continue
            result.append({
                "start": round(r_start, 1),
                "end": round(r_end, 1),
                "title": f"Silent section {int(r_start)}s–{int(r_end)}s",
                "hook": "",
                "reason": "No-speech section of the source video",
                "virality_score": 3.0,
                "hook_score": 3.0,
            })
            if budget is not None and len(result) >= budget:
                return result

    return result


async def _load_or_transcribe_segments(video, user_settings, whisper_quality: str = "balanced", force_retranscribe: bool = False, job_id: str = None, user_id: str = "local") -> list[dict]:
    """Load segments from DB or run Whisper if needed."""
    # Try loading from DB first (skip if user wants to re-transcribe)
    if not force_retranscribe and video.transcript_segments_json:
        try:
            segments = json.loads(video.transcript_segments_json)
            if segments:
                logger.info(f"Using cached transcript segments for {video.id[:8]}")
                return segments
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Corrupt transcript_segments_json for {video.id[:8]}, re-transcribing")

    # Need to run Whisper
    audio_path = video.audio_path or video.video_path
    if not audio_path:
        logger.error(f"No audio/video path for {video.id[:8]}")
        return []
    if not Path(audio_path).exists():
        logger.error(f"Audio file not found: {audio_path} (video {video.id[:8]})")
        return []

    duration_min = round((video.duration_seconds or 0) / 60, 1)
    logger.info(f"Running Whisper for clip extraction on {video.id[:8]} (quality={whisper_quality})")
    if job_id:
        await ws_manager.send_progress(
            job_id, 7,
            f"Transcribing {duration_min}min audio (this may take a few minutes)...",
            user_id,
        )

    from backend.services.whisper_service import whisper_service

    try:
        await asyncio.to_thread(whisper_service.load, whisper_quality)
        transcript_data = await whisper_service.transcribe(audio_path)
    except Exception as e:
        # Whisper crashed (corrupt audio, OOM, model load failure, etc.) — re-raise
        # so the job fails LOUDLY instead of silently downgrading to duration-based
        # clips. Returning [] here used to land us in the no-speech fallback path,
        # which produced random time-cut clips. The legitimate "video has no speech"
        # case is unaffected: Whisper returns successfully with an empty `segments`
        # list, which the caller routes to the duration-based fallback as before.
        logger.error(f"Whisper transcription failed for {video.id[:8]}: {e}", exc_info=True)
        raise ValueError(
            f"Audio transcription failed: {e}. "
            f"This usually means the audio track is corrupt, missing, or in an "
            f"unsupported format. Try re-downloading the video, or re-analyze "
            f"with a different Whisper quality setting."
        )

    segments = transcript_data.get("segments", [])

    # Back-fill DB
    if segments:
        from backend.database import AsyncSessionLocal
        from backend.models.downloaded_video import DownloadedVideo
        from sqlalchemy import select
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(DownloadedVideo).where(DownloadedVideo.id == video.id)
                )
                row = result.scalar_one_or_none()
                if row:
                    row.transcript_segments_json = json.dumps(segments)
                    if not row.transcript:
                        row.transcript = transcript_data.get("text", "")
                        row.transcript_language = transcript_data.get("language")
                    await db.commit()
            logger.info(f"Back-filled transcript_segments_json for {video.id[:8]}")
        except Exception as e:
            logger.warning(f"Failed to back-fill transcript for {video.id[:8]}: {e}")

    return segments


# ── Clip selection strategies ──────────────────────────────────────────────────

async def _select_clip_windows_with_retries(
    segments, title, duration, max_clips, user_settings,
    min_duration=None, max_duration=None, job_id=None, user_id="local",
) -> list[dict]:
    """Try clip selection with progressively relaxed constraints.

    User-pinned bounds are non-negotiable: the fallback cascade only widens
    the side(s) the user did NOT specify. Pre-2026-05-26 the retries
    hardcoded 10-90s then 5-120s regardless of input, so a request for
    10-20s clips could silently ship a 53s clip when attempt 1 found
    nothing in-range. _select_clip_windows already retries the AI call
    twice internally, so when both bounds are pinned a third call with the
    same bounds is unlikely to help — return [] and let extract_viral_clips
    raise a clean "no clips found" error.
    """
    # Attempt 1: try the requested constraints exactly.
    clip_windows = await _select_clip_windows(
        segments, title, duration, max_clips, user_settings,
        min_duration=min_duration, max_duration=max_duration,
    )
    if clip_windows:
        return clip_windows

    # If the user pinned BOTH bounds the duration window is non-negotiable.
    # _select_clip_windows already retried the AI 2x; widening would silently
    # violate the user's request. Surface the failure cleanly instead.
    if min_duration is not None and max_duration is not None:
        logger.info(
            f"No clips found within user range {min_duration}-{max_duration}s "
            f"(not relaxing — user pinned both bounds)"
        )
        return []

    # Auto-mode or one-sided override: widen only the unspecified side(s).
    relax_min = min_duration if min_duration is not None else 10
    relax_max = max_duration if max_duration is not None else 90
    logger.info(
        f"No clips found with default constraints, retrying with relaxed duration "
        f"({relax_min}-{relax_max}s)..."
    )
    if job_id:
        await ws_manager.send_progress(job_id, 20, "Retrying with relaxed duration constraints...", user_id)
    clip_windows = await _select_clip_windows(
        segments, title, duration, max_clips, user_settings,
        min_duration=relax_min, max_duration=relax_max,
    )
    if clip_windows:
        return clip_windows

    # Attempt 3: most permissive — but user-set bounds still win where present.
    perm_min = min_duration if min_duration is not None else 5
    perm_max = max_duration if max_duration is not None else 120
    reduced_clips = max(2, max_clips // 2)
    logger.info(
        f"Still no clips, trying permissive extraction "
        f"({perm_min}-{perm_max}s, {reduced_clips} clips)..."
    )
    if job_id:
        await ws_manager.send_progress(job_id, 25, "Last attempt with relaxed constraints...", user_id)
    clip_windows = await _select_clip_windows(
        segments, title, duration, reduced_clips, user_settings,
        min_duration=perm_min, max_duration=perm_max,
    )
    return clip_windows or []


async def _chunked_clip_selection(
    segments, title, duration, max_clips, user_settings,
    min_duration=None, max_duration=None, job_id=None, user_id="local",
) -> list[dict]:
    """
    Split the video into time chunks and run AI clip selection on each chunk
    in parallel. This solves the problem of truncated transcripts and AI
    struggling to output many clips in one call.

    For a 18-min video requesting 35 clips:
    - Split into 4 chunks of ~4.5min each
    - Ask each chunk for ~9 clips
    - Merge, deduplicate, and return top clips by virality score
    """
    # Calculate chunk parameters
    clips_per_ai_call = 8  # sweet spot for AI reliability
    num_chunks = max(2, math.ceil(max_clips / clips_per_ai_call))
    chunk_duration = duration / num_chunks
    clips_per_chunk = math.ceil(max_clips / num_chunks) + 1  # +1 for safety margin

    logger.info(
        f"Chunked extraction: {num_chunks} chunks of {chunk_duration:.0f}s, "
        f"{clips_per_chunk} clips/chunk, total target={max_clips}"
    )

    if job_id:
        await ws_manager.send_progress(
            job_id, 18,
            f"Analyzing {num_chunks} sections of the video in parallel...",
            user_id,
        )

    # Build tasks for each chunk
    async def _process_chunk(chunk_idx):
        chunk_start = chunk_idx * chunk_duration
        chunk_end = min((chunk_idx + 1) * chunk_duration, duration)

        # Filter segments to this chunk
        chunk_segments = [
            s for s in segments
            if s.get("end", 0) > chunk_start and s.get("start", 0) < chunk_end
        ]

        if not chunk_segments:
            return []

        # Check if chunk has enough content
        chunk_text = " ".join(s.get("text", "") for s in chunk_segments)
        if len(chunk_text.split()) < 30:  # less than ~15s of speech
            logger.debug(f"Chunk {chunk_idx+1}: skipping, only {len(chunk_text.split())} words")
            return []

        chunk_title = f"{title} (section {chunk_idx+1}/{num_chunks}, {chunk_start:.0f}s-{chunk_end:.0f}s)"

        windows = await _select_clip_windows(
            chunk_segments, chunk_title, chunk_end - chunk_start,
            clips_per_chunk, user_settings,
            min_duration=min_duration, max_duration=max_duration,
            time_offset=chunk_start,
        )
        return windows or []

    # Run all chunks in parallel
    chunk_tasks = [_process_chunk(i) for i in range(num_chunks)]
    chunk_results = await asyncio.gather(*chunk_tasks, return_exceptions=True)

    # Merge results
    all_windows = []
    for i, result in enumerate(chunk_results):
        if isinstance(result, Exception):
            logger.warning(f"Chunk {i+1} failed: {result}")
            continue
        logger.info(f"Chunk {i+1}: found {len(result)} clips")
        all_windows.extend(result)

    if not all_windows:
        # Fallback: try single-call extraction with reduced clip count
        logger.warning("All chunks returned 0 clips, falling back to single-call extraction")
        return await _select_clip_windows_with_retries(
            segments, title, duration, min(max_clips, 5), user_settings,
            min_duration=min_duration, max_duration=max_duration,
            job_id=job_id, user_id=user_id,
        )

    # Remove overlapping clips (keep higher virality score), then dedupe
    # near-duplicate beats by topic-keyword similarity. Both passes are
    # cheap and additive — time-overlap catches "the AI gave me chunks 1
    # and 2's adjacent picks"; topic-overlap catches "same story retold
    # in chunk 4". Order matters: time-overlap first so we don't compare
    # keyword sets across nearly-identical time windows.
    all_windows.sort(key=lambda w: w.get("virality_score", 0), reverse=True)
    deduped = _remove_overlapping_clips(all_windows)
    deduped = _dedupe_clips_by_topic(deduped)

    # Return top clips by virality
    result = deduped[:max_clips]
    logger.info(f"Chunked extraction: {len(all_windows)} raw → {len(deduped)} deduped → {len(result)} final")
    return result


# Max distance (seconds) we'll slide a clip's start/end to land on a natural
# sentence boundary. ±2s is wide enough to catch the "cut mid-word" failure
# mode the AI commonly produces, narrow enough that clip duration stays
# within the user's requested min/max even after snapping both ends.
_MAX_BOUNDARY_SLIDE_SEC = 2.0


def _snap_to_sentence_boundaries(
    windows: list[dict], segments: list[dict],
) -> list[dict]:
    """Nudge each clip's start/end onto the nearest Whisper segment edge if
    one is within ±_MAX_BOUNDARY_SLIDE_SEC.

    Whisper produces segments at sentence-like boundaries (utterance pauses,
    punctuation), so snapping to a segment edge is equivalent to snapping
    to a natural sentence boundary. We never extend past the slide budget,
    so clips can't grow beyond the user's max_duration after snap.

    Returns a NEW list — the input windows are not mutated.
    """
    if not segments or not windows:
        return windows

    # Pre-extract sorted boundary timestamps (start of each segment + end of
    # the last one) so the snap is a search over a small fixed set.
    starts = sorted({float(s.get("start", 0)) for s in segments if s.get("start") is not None})
    ends = sorted({float(s.get("end", 0)) for s in segments if s.get("end") is not None})

    def _nearest(target: float, candidates: list[float]) -> float | None:
        if not candidates:
            return None
        # Linear is fine — segments are small (typically <500). Avoids a
        # bisect import for a 30-line helper.
        best = min(candidates, key=lambda c: abs(c - target))
        return best if abs(best - target) <= _MAX_BOUNDARY_SLIDE_SEC else None

    snapped = []
    for w in windows:
        try:
            orig_start = float(w["start"])
            orig_end = float(w["end"])
        except (KeyError, TypeError, ValueError):
            snapped.append(w)
            continue

        new_start = _nearest(orig_start, starts)
        new_end = _nearest(orig_end, ends)

        # Only commit the snap if the result still has positive duration.
        # (Pathological inputs where start == end after snap are rare but
        # cheap to defend against.)
        nw = dict(w)
        if new_start is not None:
            nw["start"] = round(new_start, 1)
        if new_end is not None:
            nw["end"] = round(new_end, 1)
        if nw["end"] - nw["start"] < 1.0:
            # Snap would collapse the clip — keep the original.
            snapped.append(w)
            continue
        snapped.append(nw)

    return snapped


def _remove_overlapping_clips(windows: list[dict]) -> list[dict]:
    """Remove overlapping clips, keeping higher-scored ones (assumed pre-sorted by score desc)."""
    kept = []
    for w in windows:
        overlaps = False
        for k in kept:
            # Check if they overlap (with 2s tolerance)
            if w["start"] < k["end"] - 2 and w["end"] > k["start"] + 2:
                overlaps = True
                break
        if not overlaps:
            kept.append(w)
    return kept


# ── Topic dedup (cross-time-window near-duplicate detection) ────────────────
#
# The AI sometimes picks two clips that don't overlap in TIME but cover the
# same TOPIC — a guest who re-tells a story at minute 5 and minute 25, or a
# host who restates a punchline. _remove_overlapping_clips catches time
# overlap; this catches topic overlap so the user gets distinct beats.
#
# Tactics:
#   - Tokenize each clip's (title + hook + reason) — already-AI-written text
#     that summarizes the clip in ~30 words. Compact and topic-rich.
#   - Strip a small English stopword set. Tokens under 4 chars are also
#     dropped. Non-English videos still get useful signal because content
#     words are 4+ chars in most languages.
#   - Jaccard similarity per pair; if >= TOPIC_DEDUP_THRESHOLD, drop the
#     lower-virality clip from the pair.

_TOPIC_STOPWORDS = frozenset({
    "about", "after", "again", "against", "their", "there", "these", "those",
    "which", "while", "would", "could", "should", "where", "every", "before",
    "being", "doing", "down", "from", "have", "having", "into", "more", "most",
    "much", "other", "over", "same", "such", "than", "that", "this", "very",
    "well", "what", "when", "with", "your", "yours", "they", "them", "were",
    "will", "just", "even", "also", "make", "made", "like", "many", "some",
    "only", "first", "thing", "things", "really", "still", "back", "good",
    "great", "right", "want", "need", "know", "going", "didnt", "doesnt",
    "isnt", "wasnt", "didn", "weren", "video", "clip", "viral", "moment",
    "segment", "audience", "viewer", "viewers",
})

TOPIC_DEDUP_THRESHOLD = 0.55


def _topic_keywords(text: str) -> set[str]:
    """Return the set of content tokens in *text* for Jaccard comparison.

    Lowercased, alphabetic-only tokens of length ≥ 4 minus a small English
    stopword set. Non-content words ("video", "clip", "moment") are filtered
    too — they're high-frequency across the AI's reasoning text and would
    inflate similarity between unrelated clips.
    """
    if not text:
        return set()
    tokens = re.findall(r"[a-z]{4,}", text.lower())
    return {t for t in tokens if t not in _TOPIC_STOPWORDS}


def _dedupe_clips_by_topic(
    windows: list[dict],
    threshold: float = TOPIC_DEDUP_THRESHOLD,
) -> list[dict]:
    """Drop near-duplicate clips by Jaccard similarity on AI-summarized text.

    Input is assumed already sorted by virality_score desc — the function
    walks top-down and drops anything that closely matches an already-kept
    clip. Preserves the input order in the return value (the surviving
    high-virality clips stay at the top).

    Pure function; doesn't touch the network or the filesystem.
    """
    if len(windows) <= 1:
        return list(windows)

    # Pre-compute keyword sets once.
    keywords = [
        _topic_keywords(" ".join([
            str(w.get("title") or ""),
            str(w.get("hook") or ""),
            str(w.get("reason") or ""),
        ]))
        for w in windows
    ]

    kept_indices: list[int] = []
    for i, w in enumerate(windows):
        kw_i = keywords[i]
        if not kw_i:
            # No usable keywords — keep it (can't compare). Rare; happens
            # when title/hook/reason are all empty (legacy or malformed AI).
            kept_indices.append(i)
            continue
        is_dup = False
        for j in kept_indices:
            kw_j = keywords[j]
            if not kw_j:
                continue
            inter = len(kw_i & kw_j)
            union = len(kw_i | kw_j)
            sim = inter / union if union else 0.0
            if sim >= threshold:
                logger.info(
                    f"Topic dedup: dropping clip {w.get('start',0):.0f}-{w.get('end',0):.0f}s "
                    f"(virality {w.get('virality_score','?')}) — {sim:.0%} keyword overlap "
                    f"with kept clip {windows[j].get('start',0):.0f}-{windows[j].get('end',0):.0f}s "
                    f"(virality {windows[j].get('virality_score','?')})"
                )
                is_dup = True
                break
        if not is_dup:
            kept_indices.append(i)

    return [windows[i] for i in kept_indices]


async def _select_clip_windows(
    segments: list[dict],
    title: str,
    duration: int,
    max_clips: int,
    user_settings,
    min_duration: int = None,
    max_duration: int = None,
    time_offset: float = 0,
) -> list[dict]:
    """Use AI to identify the best clip windows from the transcript."""
    # Resolve clip duration range from user input + source duration. Order
    # matters: explicit user values win in full; if the user gave only one
    # bound we derive the other while respecting the bound they typed;
    # otherwise we fall back to the duration-based defaults that have shipped
    # since launch. The earlier code only honored user input when BOTH bounds
    # were set, silently ignoring a single-bound override (bug 2026-04-30).
    if min_duration and max_duration:
        min_clip_sec, max_clip_sec = min_duration, max_duration
    elif min_duration:
        # User typed only a minimum. Pick a max that respects min and the
        # source length: the clip can't be longer than the source itself,
        # and we keep at least min+5s of headroom so the AI has flexibility.
        if duration and duration < 120:
            max_clip_sec = max(min_duration + 5, min(60, duration - 5))
        elif duration and duration < 300:
            max_clip_sec = max(min_duration + 5, 60)
        else:
            max_clip_sec = max(min_duration + 5, 90)
        min_clip_sec = min_duration
    elif max_duration:
        # User typed only a maximum. Derive a sensible min — third of the
        # max, floored at 10s so we never end up below the API's lower bound.
        min_clip_sec = max(10, min(15, max_duration // 3))
        max_clip_sec = max_duration
    elif duration and duration < 120:
        min_clip_sec, max_clip_sec = 15, min(60, max(duration - 5, 20))
    elif duration and duration < 300:
        min_clip_sec, max_clip_sec = 20, 60
    else:
        min_clip_sec, max_clip_sec = 30, 60

    # Build segments text — scale budget based on clip count
    char_budget = min(20000, max(8000, max_clips * 1500))
    segments_text = _build_segments_text(segments, duration, max_chars=char_budget)

    # Infer niche from title
    niche = title if title != "Untitled" else "general content"

    prompt = CLIP_SELECTION_PROMPT.format(
        max_clips=max_clips,
        min_clip=min_clip_sec,
        max_clip=max_clip_sec,
        title=title,
        duration=duration,
        niche=niche,
        segments_text=segments_text,
    )

    from backend.core.ai_provider import get_ai_client
    ai = get_ai_client(user_settings)

    # Try up to 2 times if AI returns invalid response
    windows = None
    for attempt in range(2):
        try:
            # Scale max_tokens: ~200 tokens per clip JSON object + buffer
            token_limit = max(2000, min(max_clips * 250 + 500, 16000))
            response = await ai.chat(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=token_limit,
            )
            parsed = _parse_json_response(response)
            if parsed and isinstance(parsed, list):
                windows = parsed
                break
            elif parsed and isinstance(parsed, dict):
                # AI returned a single clip as dict instead of array
                windows = [parsed]
                break
            else:
                logger.warning(
                    f"AI clip selection attempt {attempt + 1}: invalid format (parsed={type(parsed).__name__}). "
                    f"Response length={len(response)}, first 300 chars: {response[:300]}"
                )
        except Exception as e:
            logger.warning(f"AI clip selection attempt {attempt + 1} failed: {e}")

    if not windows:
        logger.warning(f"AI clip selection returned no valid response after 2 attempts for '{title[:50]}'")
        return []

    logger.info(f"AI returned {len(windows)} raw clip candidates for '{title[:50]}'")

    # Validate and clean windows
    valid = []
    for w in windows:
        try:
            start = float(w.get("start", 0))
            end = float(w.get("end", 0))
        except (TypeError, ValueError):
            continue

        # Clamp to valid range. The chunked path passes segments to the AI
        # with their *global* timestamps already (see `_chunked_clip_selection`
        # — `chunk_segments` keeps each segment's original start/end, and
        # `_build_segments_text` formats them as-is). So AI returns clips
        # in global time, and we should NOT add `time_offset` here — doing
        # so was double-counting and made chunks 2+ silently lose almost
        # every clip via the `start >= end` skip below. (Bug 2026-04-30:
        # an 18-clip request on a 9.2-min source returned only 4 because
        # chunks 2 & 3 produced unusable times.)
        #
        # `total_duration` still uses `duration + time_offset` so we clamp
        # at the chunk's *global* end, not the chunk's local length.
        if start < 0:
            start = 0
        total_duration = duration + time_offset if time_offset > 0 else duration
        if total_duration and end > total_duration:
            end = total_duration

        if start >= end:
            # AI returned a clip that fell outside the chunk's global range
            # (e.g. an end time smaller than start after clamp). Visible at
            # info level so future regressions of this kind aren't silent.
            logger.info(
                f"Skipping invalid clip after clamp: start={start:.1f}, end={end:.1f} "
                f"(chunk total_duration={total_duration})"
            )
            continue

        # Re-validate length AFTER clamping — be lenient
        clip_len = end - start
        if clip_len < 5:
            logger.debug(f"Skipping clip {start:.1f}-{end:.1f} ({clip_len:.1f}s): under 5s minimum")
            continue
        if clip_len > max_clip_sec + 30:
            # Way too long — trim from end rather than discarding
            end = start + max_clip_sec
            clip_len = max_clip_sec
            logger.debug(f"Trimmed oversized clip to {start:.1f}-{end:.1f} ({clip_len:.1f}s)")
        if clip_len < min_clip_sec and clip_len >= 5:
            logger.debug(f"Accepting short clip {start:.1f}-{end:.1f} ({clip_len:.1f}s, below {min_clip_sec}s min)")

        w["start"] = round(start, 1)
        w["end"] = round(end, 1)
        # Ensure virality_score is present and valid
        try:
            score = float(w.get("virality_score", 5))
            w["virality_score"] = max(1, min(10, score))
        except (TypeError, ValueError):
            w["virality_score"] = 5.0
        valid.append(w)

    # Sort by virality score descending
    valid.sort(key=lambda w: w.get("virality_score", 0), reverse=True)
    return valid[:max_clips]


def _build_segments_text(segments: list[dict], duration: int, max_chars: int = 12000) -> str:
    """Build transcript text for AI, with intelligent handling of long videos."""
    if not segments:
        return ""

    # For very long videos (>30 min), sample from beginning, middle, and end
    total_text_len = sum(len(f"[{s['start']:.1f}s] {s.get('text', '')}") for s in segments)
    if total_text_len > max_chars * 2 and duration > 1800:
        # Sample strategy: first 25%, skip, middle 25%, skip, last 25%
        quarter = max(len(segments) // 4, 1)
        mid_start = len(segments) // 2 - quarter // 2
        sampled = (
            segments[:quarter]
            + [{"start": segments[quarter]["start"], "end": segments[quarter]["end"], "text": "... (segments omitted) ..."}]
            + segments[mid_start:mid_start + quarter]
            + [{"start": segments[-quarter]["start"], "end": segments[-quarter]["end"], "text": "... (segments omitted) ..."}]
            + segments[-quarter:]
        )
        lines = []
        char_count = 0
        for s in sampled:
            line = f"[{s['start']:.1f}s - {s['end']:.1f}s] {s.get('text', '')}"
            if char_count + len(line) > max_chars:
                lines.append(f"... (truncated at {s['start']:.0f}s of {duration}s total)")
                break
            lines.append(line)
            char_count += len(line)
        return "\n".join(lines)

    # Normal case: include all segments up to limit
    lines = []
    char_count = 0
    for s in segments:
        line = f"[{s['start']:.1f}s - {s['end']:.1f}s] {s.get('text', '')}"
        if char_count + len(line) > max_chars:
            lines.append(f"... (truncated at {s['start']:.0f}s of {duration}s total)")
            break
        lines.append(line)
        char_count += len(line)
    return "\n".join(lines)


async def _process_clips_parallel(
    video,
    clip_windows: list[dict],
    segments: list[dict],
    caption_style: str,
    user_settings,
    job_id: str = None,
    user_id: str = "local",
) -> list[dict]:
    """Process all clips in parallel: extract → caption → thumbnail → metadata."""
    from backend.services.ffmpeg_service import extract_clip, extract_thumbnail

    total = len(clip_windows)

    # Phase 1: Extract all clips in parallel
    if job_id:
        await ws_manager.send_progress(job_id, 35, f"Cutting {total} clips from source video...", user_id)

    async def _extract_one(i, window):
        clip_path = settings.GENERATED_DIR / f"clip_{video.id[:8]}_{uuid4().hex[:6]}.mp4"
        await extract_clip(
            Path(video.video_path), window["start"], window["end"],
            clip_path, vertical=True,
        )
        return clip_path

    extract_tasks = [_ffmpeg_limited(_extract_one(i, w)) for i, w in enumerate(clip_windows)]
    clip_paths = await asyncio.gather(*extract_tasks, return_exceptions=True)

    # Phase 2: Burn captions in parallel (on successfully extracted clips)
    if job_id:
        await ws_manager.send_progress(job_id, 55, f"Adding captions to {total} clips...", user_id)

    async def _caption_one(i, clip_path, window):
        if isinstance(clip_path, Exception):
            return clip_path, "extract_failed"

        clip_segments = _filter_and_offset_segments(segments, window["start"], window["end"])
        captioned_path, caption_status = await _burn_clip_captions(clip_path, clip_segments, caption_style)
        return captioned_path, caption_status

    caption_tasks = [_ffmpeg_limited(_caption_one(i, cp, w)) for i, (cp, w) in enumerate(zip(clip_paths, clip_windows))]
    caption_results = await asyncio.gather(*caption_tasks, return_exceptions=True)

    # Phase 3: Thumbnails + metadata in parallel
    if job_id:
        await ws_manager.send_progress(job_id, 75, f"Generating thumbnails and metadata...", user_id)

    results = []
    thumb_tasks = []
    meta_inputs: list[tuple[str, str]] = []  # (title, transcript_text) per kept clip

    for i, (cap_result, window) in enumerate(zip(caption_results, clip_windows)):
        if isinstance(cap_result, Exception):
            logger.error(f"Clip {i+1} processing failed: {cap_result}")
            continue

        captioned_path, caption_status = cap_result
        # `caption_status == "extract_failed"` means the underlying clip
        # extract raised; `captioned_path` is the original Exception object,
        # NOT a real Path. Letting it through would propagate a garbage
        # `video_path` (the str() of the exception) into the GeneratedVideo
        # row downstream. Drop it here.
        if caption_status == "extract_failed":
            logger.warning(
                f"Clip {i+1} skipped: extract step failed for window "
                f"{window.get('start')}-{window.get('end')}s"
            )
            continue
        clip_segments = _filter_and_offset_segments(segments, window["start"], window["end"])

        # Adaptive thumbnail timestamp (30% through clip, max 5s)
        clip_dur = window["end"] - window["start"]
        thumb_ts = min(clip_dur * 0.3, 5.0)
        thumb_tasks.append(_ffmpeg_limited(extract_thumbnail(captioned_path, timestamp=thumb_ts)))

        transcript_text = " ".join(s["text"] for s in clip_segments)
        meta_inputs.append((window.get("title", f"Clip {i+1}"), transcript_text))

        results.append({
            "_index": i,
            "captioned_path": captioned_path,
            "caption_status": caption_status,
            "window": window,
            "clip_segments": clip_segments,
            "raw_clip_path": clip_paths[i] if not isinstance(clip_paths[i], Exception) else None,
        })

    # Metadata (one batched AI round-trip) and the thumbnail FFmpeg are
    # independent — start the metadata call FIRST so its cloud latency overlaps
    # the local thumbnail extraction instead of running strictly after it.
    # The batch covers every kept clip instead of N parallel calls: one AI call
    # for N clips. On hard failure (raised / parse failed / no usable entries)
    # we fall back to the per-clip parallel path — same final shape, same
    # per-clip `metadata_status` granularity.
    meta_task = (
        asyncio.create_task(_generate_clip_metadata_batch(meta_inputs, user_settings))
        if meta_inputs else None
    )

    # Thumbnails run in parallel — local FFmpeg, no AI cost.
    all_thumbs = await asyncio.gather(*thumb_tasks, return_exceptions=True)

    # Collect the metadata that's been generating concurrently.
    all_metas: list = []
    if meta_task is not None:
        batched = await meta_task
        if batched is not None and len(batched) == len(meta_inputs):
            all_metas = batched
        else:
            logger.info(
                f"Metadata batch unavailable, falling back to {len(meta_inputs)} "
                f"per-clip calls."
            )
            fallback_tasks = [
                _generate_clip_metadata(title, transcript_text, user_settings)
                for title, transcript_text in meta_inputs
            ]
            all_metas = await asyncio.gather(*fallback_tasks, return_exceptions=True)

    # Phase 4: Assemble final results + cleanup
    if job_id:
        await ws_manager.send_progress(job_id, 90, "Finalizing clips...", user_id)

    final_results = []
    for idx, r in enumerate(results):
        window = r["window"]
        captioned_path = r["captioned_path"]
        caption_status = r["caption_status"]

        # Thumbnail
        thumb_path = all_thumbs[idx] if idx < len(all_thumbs) else None
        if isinstance(thumb_path, Exception):
            logger.warning(f"Thumbnail extraction failed for clip {idx+1}: {thumb_path}")
            thumb_path = None

        # Metadata
        metadata = all_metas[idx] if idx < len(all_metas) else {}
        metadata_status = "ai_generated"
        if isinstance(metadata, Exception):
            logger.warning(f"Metadata generation failed for clip {idx+1}: {metadata}")
            metadata = _default_metadata(window.get("title", f"Clip {idx+1}"), "")
            metadata_status = "fallback"
        elif not metadata or not isinstance(metadata, dict):
            metadata = _default_metadata(window.get("title", f"Clip {idx+1}"), "")
            metadata_status = "fallback"

        transcript_text = " ".join(s["text"] for s in r["clip_segments"])

        # Cleanup intermediate files (raw clip before captioning)
        raw_path = r.get("raw_clip_path")
        if raw_path and isinstance(raw_path, Path) and raw_path != captioned_path:
            try:
                raw_path.unlink(missing_ok=True)
            except Exception:
                pass

        final_results.append({
            "video_path": captioned_path,
            "thumbnail_path": thumb_path,
            "title": window.get("title", f"Clip {idx+1}"),
            "transcript_text": transcript_text,
            "reason": window.get("reason", ""),
            "start": window["start"],
            "end": window["end"],
            "duration_seconds": int(window["end"] - window["start"]),
            "virality_score": window.get("virality_score", 5.0),
            "caption_status": caption_status,
            "metadata_status": metadata_status,
            **metadata,
        })

    return final_results


def _filter_and_offset_segments(segments: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    """Filter segments to clip range and offset timestamps to start at 0."""
    filtered = []
    for s in segments:
        seg_start = s.get("start", 0)
        seg_end = s.get("end", 0)
        # Include if segment overlaps with clip range
        if seg_end > clip_start and seg_start < clip_end:
            adjusted = dict(s)
            adjusted["start"] = max(seg_start - clip_start, 0)
            adjusted["end"] = min(seg_end - clip_start, clip_end - clip_start)
            # Also adjust word timestamps if present
            if "words" in adjusted:
                adjusted["words"] = [
                    {**w, "start": max(w["start"] - clip_start, 0), "end": min(w["end"] - clip_start, clip_end - clip_start)}
                    for w in adjusted["words"]
                    if w.get("end", 0) > clip_start and w.get("start", 0) < clip_end
                ]
            filtered.append(adjusted)
    return filtered


async def _burn_clip_captions(clip_path: Path, segments: list[dict], style: str = "viral") -> tuple[Path, str]:
    """Burn captions onto a clip. Returns (output_path, status)."""
    if not segments:
        return clip_path, "no_segments"

    try:
        from backend.services.caption_service import generate_captions_ass, burn_captions
        ass_path = await generate_captions_ass(segments, style=style, aspect_ratio="9:16")
        captioned = await burn_captions(clip_path, ass_path)

        # Cleanup ASS file
        if ass_path and ass_path.exists():
            try:
                ass_path.unlink(missing_ok=True)
            except Exception:
                pass

        # `caption_service.burn_captions` returns the *input* path unchanged
        # in three failure modes that don't raise: FFmpeg missing libass,
        # the burn command returning non-zero, or the ASS file ending up
        # empty (no extractable words for these segments). Treat any of
        # those as a failure — otherwise the clip ships with NO captions
        # but `caption_status="applied"`. Bug 2026-04-30.
        if captioned == clip_path:
            logger.warning(
                f"Caption burn returned input path unchanged for {clip_path.name} — "
                f"libass missing, ffmpeg burn failed, or no words extractable from "
                f"{len(segments)} segment(s). Marking caption_status=failed."
            )
            return clip_path, "failed"

        return captioned, "applied"
    except Exception as e:
        logger.warning(f"Caption burning failed for clip {clip_path.name}: {e}")
        return clip_path, "failed"


def _default_metadata(title: str, transcript_text: str) -> dict:
    """Default metadata when AI generation fails."""
    return {
        "youtube_title": title,
        "youtube_description": transcript_text[:200] if transcript_text else "",
        "youtube_tags": [],
        "tiktok_title": title,
    }


async def _generate_clip_metadata(title: str, transcript_text: str, user_settings) -> dict:
    """Generate YouTube/TikTok metadata for a clip via AI."""
    defaults = _default_metadata(title, transcript_text)

    try:
        from backend.core.ai_provider import get_ai_client
        ai = get_ai_client(user_settings)
        prompt = CLIP_METADATA_PROMPT.format(
            title=title,
            transcript_text=transcript_text[:2000],
        )
        response = await ai.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
        )
        parsed = _parse_json_response(response)
        if parsed and isinstance(parsed, dict):
            return {**defaults, **parsed}
    except Exception as e:
        logger.warning(f"Clip metadata generation failed for '{title[:30]}': {e}")

    return defaults


async def _generate_clip_metadata_batch(
    clips: list[tuple[str, str]],
    user_settings,
) -> list[dict | None] | None:
    """Generate metadata for N clips in ONE AI call.

    Replaces the per-clip-call pattern (N calls for an N-clip extraction)
    with a single batched call regardless of N.

    Returns a list of length N (same order as `clips`) where each entry
    is EITHER:
      - dict: the AI-generated metadata for that clip (caller marks
        `metadata_status="ai_generated"`)
      - None: the AI response didn't include this clip's index (caller
        treats it as a falsy value and marks `metadata_status="fallback"`
        via its existing default-fallback path)

    Returns `None` (the whole list, not a per-entry None) on a hard
    failure: call raised, response not a list, or zero valid entries.
    The caller falls back to N parallel per-clip calls so robustness is
    preserved exactly.
    """
    if not clips:
        return []

    # Build per-clip input lines for the prompt. Cap each transcript at
    # 1500 chars — N × 2000 starts to bloat the prompt for large jobs, and
    # metadata generation only needs the gist of the clip's content. Empty
    # transcripts are passed through as "(no speech)" so the AI leans on the
    # title.
    lines = []
    for idx, (title, transcript_text) in enumerate(clips):
        safe_title = (title or f"Clip {idx + 1}").strip()
        safe_transcript = (transcript_text or "").strip()[:1500] or "(no speech)"
        lines.append(
            f"Clip {idx} | title: {safe_title}\nTranscript: {safe_transcript}"
        )
    clips_block = "\n\n".join(lines)

    prompt = CLIP_METADATA_BATCH_PROMPT.format(
        count=len(clips),
        clips_block=clips_block,
    )

    # Token budget: ~200 output tokens per clip × N + 500 buffer, capped
    # at 8000.
    max_tokens = min(8000, max(2000, len(clips) * 200 + 500))

    try:
        from backend.core.ai_provider import get_ai_client
        ai = get_ai_client(user_settings)
        response = await ai.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        parsed = _parse_json_response(response)
    except Exception as e:
        logger.warning(
            f"Batched clip metadata generation failed for {len(clips)} clips: {e} "
            f"— caller will fall back to per-clip calls."
        )
        return None

    # Accept the canonical array shape, plus two defensive shapes the
    # model sometimes emits: {"clips": [...]} / {"metadata": [...]}.
    if isinstance(parsed, dict):
        for wrapper_key in ("clips", "metadata", "items", "results"):
            if isinstance(parsed.get(wrapper_key), list):
                parsed = parsed[wrapper_key]
                break

    if not isinstance(parsed, list) or not parsed:
        logger.warning(
            f"Batched clip metadata: parsed response is not a non-empty list "
            f"(type={type(parsed).__name__}); falling back to per-clip calls."
        )
        return None

    # Index the response by `index` field. Order may be wrong, items may
    # be missing, indices may be out of range — handle each defensively.
    by_index: dict[int, dict] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(clips):
            by_index[idx] = item

    # Hard failure: if the AI returned ZERO usable entries, return None
    # so the caller falls back to per-clip parallel calls.
    if not by_index:
        logger.warning(
            f"Batched clip metadata: no valid index-keyed entries in response "
            f"(parsed {len(parsed)} items but none usable); falling back to "
            f"per-clip calls."
        )
        return None

    # Assemble the result list. Each entry is either the AI-generated
    # dict OR None for missing/invalid indices. For PRESENT entries, merge
    # over per-clip defaults so a partial AI response still has all four
    # fields populated. Merge order: defaults first, AI on top.
    result: list[dict | None] = []
    for idx, (title, transcript_text) in enumerate(clips):
        ai_entry = by_index.get(idx)
        if ai_entry is None:
            result.append(None)
            continue
        defaults = _default_metadata(title, transcript_text)
        merged = {**defaults, **ai_entry}
        # Strip the routing field — downstream consumers don't expect it.
        merged.pop("index", None)
        result.append(merged)

    return result


def _parse_json_response(text: str) -> any:
    """Extract JSON from AI response, handling markdown fences."""
    if not text:
        return None
    text = text.strip()
    # Remove markdown code fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON array or object in the text
        for pattern in [r'\[.*\]', r'\{.*\}']:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    continue
    return None
