# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The Clipper's prompts and the prompt blocks built from a request (platform, genre), plus the
metadata defaults and JSON salvage. Pure text: no I/O, no AI call. Split out of clip_extractor.py;
clip_extractor re-exports every name, so import from either."""
import json

from backend.core.prompt_rules import OUTPUT_LANGUAGE_RULE


# ── AI Prompts ────────────────────────────────────────────────────────────────

CLIP_SELECTION_PROMPT = """You are an expert viral short-form video editor who has produced thousands of clips with millions of views.

Your job: identify the best non-overlapping segments ({min_clip}–{max_clip} seconds each) from this transcript. Find up to {max_clips} quality clips. Only include segments that genuinely meet the criteria below — never pad with weak content.
{user_query_block}{platform_bias_block}{genre_bias_block}
SELECTION CRITERIA (in priority order):
1. HOOK STRENGTH: The first 2-3 seconds of the segment must grab attention immediately. Look for: surprising statements, bold claims, questions, emotional peaks, "did you know" moments.
2. STANDALONE VALUE: Each segment must make complete sense without the rest of the video. No dangling references, no "as I mentioned earlier".
3. INFORMATION DENSITY: Every 10 seconds should advance the narrative. Cut segments that meander or repeat.
4. CLEAN BOUNDARIES: Start at natural sentence beginnings (not mid-word). End at natural conclusions (not cliffhangers that need the next sentence). Prefer segment timestamps from the transcript over arbitrary times — the system will snap your start/end to the nearest sentence boundary, but the closer you start, the better.
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
    "hook_score": 9,
    "hook_type": "curiosity_gap",
    "reason": "Why this segment works as a viral clip (be specific, 1-2 sentences)",
    "virality_score": 8.5,
    "score_breakdown": {{
      "flow": 8,
      "value": 9,
      "trend": 7,
      "shareability": 8
    }}
  }}
]

IMPORTANT:
- virality_score must be 1-10 (10 = guaranteed viral, 1 = boring) — it's your overall judgment; the sub-scores are the rubric behind it.
- hook_score must be 1-10 (10 = irresistible hook, stops the scroll; 1 = weak/boring opening). Score ONLY the first 2-3 seconds of the segment.
- hook_type must be ONE of: curiosity_gap | contrarian | emotional_peak | question | number_promise | story_loop | actionable_tip | shocking_claim | general
- score_breakdown sub-scores each 1-10:
    flow         — narrative arc + satisfying close (no dangling thoughts, no cliffhanger that needs context)
    value        — emotional / practical resonance ("did you know" payoff, actionable takeaway, gut reaction)
    trend        — alignment with what audiences are clicking on right now in this niche
    shareability — would a viewer quote, screenshot, or send this to a friend
- Each segment MUST be between {min_clip} and {max_clip} seconds
- Segments must NOT overlap
- Order by virality_score descending (best clip first)
- You MUST return at least 1 clip if there is any usable content"""
CLIP_SELECTION_PROMPT += "\n\n" + OUTPUT_LANGUAGE_RULE  # state the language — Claude does not infer it (prompt_rules)


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
CLIP_METADATA_PROMPT += "\n\n" + OUTPUT_LANGUAGE_RULE  # state the language — Claude does not infer it (prompt_rules)


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
CLIP_METADATA_BATCH_PROMPT += "\n\n" + OUTPUT_LANGUAGE_RULE  # state the language — Claude does not infer it (prompt_rules)


# ── Platform-bias prompt block ───────────────────────────────────────────────
# Each platform's algorithm rewards different hook types — TikTok loves
# shock + emotion, LinkedIn wants actionable expertise, YouTube Shorts
# splits the difference. When `target_platform` is set on extraction,
# we inject a 1-2 sentence bias into the AI selection prompt so the
# ranker can prefer hooks that fit. User's `min_duration` / `max_duration`
# always win — this only affects hook-type ranking, not clip length.
#
# Lowercase keys; unknown / None platforms get no bias (vanilla virality
# ranking). Adding a platform = one entry in this dict; the helper +
# threading handle the rest.
_PLATFORM_BIAS: dict[str, str] = {
    "tiktok": (
        "TARGET PLATFORM: TikTok. Bias selection toward shocking_claim, "
        "emotional_peak, and contrarian hook types. The first 1-2 seconds "
        "MUST stop the scroll — punchy, declarative, high-energy openings. "
        "Prefer the shorter end of the allowed clip range."
    ),
    "youtube_shorts": (
        "TARGET PLATFORM: YouTube Shorts. Bias toward curiosity_gap, "
        "number_promise, and story_loop hook types. Slightly more "
        "informational/educational tone than TikTok — viewers expect a "
        "payoff for the click."
    ),
    "reels": (
        "TARGET PLATFORM: Instagram Reels. Bias toward emotional_peak, "
        "story_loop, and curiosity_gap hook types. Lifestyle / visual "
        "storytelling tone — moments that feel cinematic or aspirational."
    ),
    "linkedin": (
        "TARGET PLATFORM: LinkedIn. Bias toward actionable_tip, "
        "number_promise, and contrarian (business-framed) hook types. "
        "Professional tone — avoid clickbait/shock. When credentials or "
        "data are present, foreground them. Prefer the longer end of "
        "the allowed clip range; LinkedIn viewers watch longer."
    ),
    "twitter": (
        "TARGET PLATFORM: Twitter / X. Bias toward shocking_claim, "
        "contrarian, and question hook types. Punchy, debate-starting "
        "takes — the best clips are the ones that make people want to "
        "quote-tweet them."
    ),
}


# Accept common aliases so callers don't have to remember the canonical key.
_PLATFORM_ALIASES: dict[str, str] = {
    "youtube": "youtube_shorts",
    "shorts": "youtube_shorts",
    "instagram_reels": "reels",
    "instagram": "reels",
    "x": "twitter",
}


def _build_platform_bias_block(target_platform: str | None) -> str:
    """Return the platform-bias text to inject into CLIP_SELECTION_PROMPT.

    Returns "" when:
      - target_platform is None / empty / whitespace
      - target_platform doesn't match a known key or alias

    The empty-string fallback keeps the prompt format-string happy
    (`{platform_bias_block}` resolves to nothing) and means callers can
    always pass a target_platform value through without first checking
    whether it's known — unknown ones quietly degrade to vanilla
    virality ranking rather than raising.
    """
    if not target_platform:
        return ""
    key = target_platform.strip().lower()
    if not key:
        return ""
    # Resolve aliases first ("youtube" → "youtube_shorts").
    key = _PLATFORM_ALIASES.get(key, key)
    bias = _PLATFORM_BIAS.get(key)
    if not bias:
        return ""
    # Leading newline so it stays clearly separated from user_query_block
    # when both are present.
    return f"\n{bias}\n"


# ── Genre-bias prompt block ─────────────────────────────────────────────────
# Different content types reward different clip-picking heuristics. A podcast
# clip is good when the guest lands a one-line insight; a tutorial clip is
# good when it stands alone as a how-to step; a Q&A clip works when the
# question and answer fit in a single beat. Same idea, free for us to inject
# as a 1-2 sentence guidance block.
#
# Lowercase keys; unknown / None genres get no bias (the platform-bias and
# default selection criteria do the work alone). Pairs cleanly with
# target_platform: a "podcast" video tagged "tiktok" will get both blocks.

_GENRE_BIAS: dict[str, str] = {
    "podcast": (
        "GENRE: Podcast. Bias selection toward moments where the guest "
        "(not the host) lands a memorable one-liner, an unexpected take, "
        "or a vulnerable confession. Avoid generic intros, sponsor reads, "
        "and pleasantries. Prefer segments that work standalone without "
        "host context."
    ),
    "interview": (
        "GENRE: Interview. Bias toward the interviewee's most quotable "
        "answers, especially ones that reveal new information or contradict "
        "expectations. The interviewer's question can be included if it sets "
        "up the answer, but the punch should be the answer itself."
    ),
    "qa": (
        "GENRE: Q&A. Each clip should pair a single question with its "
        "answer in one beat. Skip standalone questions (no payoff) and "
        "standalone answers (no setup). Look for question-answer pairs "
        "that resolve in 20-60 seconds."
    ),
    "vlog": (
        "GENRE: Vlog. Bias toward moments of genuine reaction, surprise, "
        "or storytelling beats with a clear setup → payoff. Skip travel "
        "B-roll narration and filler ('so anyway', 'and then we…'). "
        "Prefer the creator's most expressive, on-camera moments."
    ),
    "tutorial": (
        "GENRE: Tutorial / how-to. Bias toward single complete tips that "
        "work standalone: a problem statement plus the fix in one segment. "
        "Skip preambles ('today I'll show you'), tool intros, and "
        "summaries. The viewer should be able to apply the tip immediately."
    ),
    "gaming": (
        "GENRE: Gaming. Bias toward big moments: clutch plays, fails, "
        "reactions, hype peaks, surprising mechanics. Look for "
        "before-after structure (setup → payoff). Commentary that reacts "
        "to the on-screen action beats explanatory commentary."
    ),
    "reaction": (
        "GENRE: Reaction. Bias toward the strongest emotional peaks — "
        "laughter, shock, disbelief, vindication. Skip the setup of what "
        "they're reacting to (audience will infer); foreground the "
        "reaction itself. Short and punchy beats long and explanatory."
    ),
    "lecture": (
        "GENRE: Lecture / educational. Bias toward standalone insights "
        "that don't require the prior 20 minutes of context — a definition, "
        "a counterintuitive fact, a one-paragraph explanation of a concept. "
        "Prefer the longer end of the clip range; educational viewers tolerate "
        "more depth."
    ),
}


# Aliases mirror the alias-resolution pattern in _PLATFORM_ALIASES so callers
# can pass common synonyms without us shipping them into every endpoint doc.
_GENRE_ALIASES: dict[str, str] = {
    "q&a": "qa",
    "qanda": "qa",
    "ama": "qa",
    "how_to": "tutorial",
    "howto": "tutorial",
    "education": "lecture",
    "educational": "lecture",
    "talk": "lecture",
    "react": "reaction",
    "gameplay": "gaming",
    "let's_play": "gaming",
    "lets_play": "gaming",
}


def _build_genre_bias_block(genre: str | None) -> str:
    """Return the genre-bias text to inject into CLIP_SELECTION_PROMPT.

    Same fallback semantics as `_build_platform_bias_block`: unknown /
    None values return "" so callers don't need to validate first.
    """
    if not genre:
        return ""
    key = genre.strip().lower()
    if not key:
        return ""
    key = _GENRE_ALIASES.get(key, key)
    bias = _GENRE_BIAS.get(key)
    if not bias:
        return ""
    return f"\n{bias}\n"


def _build_segments_text(segments: list[dict], duration: int, max_chars: int = 12000) -> str:
    """Build transcript text for AI, with intelligent handling of long videos."""
    if not segments:
        return ""

    # For very long videos (>30 min), sample from beginning, middle, and end.
    total_text_len = sum(len(f"[{s['start']:.1f}s] {s.get('text', '')}") for s in segments)
    if total_text_len > max_chars * 2 and duration > 1800:
        # Each window gets its OWN third of the budget.
        #
        # This used to concatenate the three slices and then truncate the
        # whole thing at max_chars, which could never work: the branch only
        # runs when the transcript is more than TWICE the budget, so the three
        # quarters (75% of the segments) cannot possibly fit in it. The output
        # always ran out of room inside the FIRST quarter, which made the
        # sampling identical to plain truncation — measured on a 3-hour
        # podcast, the model was handed the first ~860s of 10800s and clip
        # selection only ever considered the opening ~14 minutes. The
        # second "omitted" marker was unreachable by construction, so the END
        # of a long video was never shown at all.
        #
        # Budgeting per window is what makes "beginning, middle, end" true.
        # The two markers come out of the budget so the caller's max_chars is
        # still an upper bound on what it gets back.
        _OMITTED = "... (segments omitted) ..."
        per_window = max((max_chars - 2 * (len(_OMITTED) + 1)) // 3, 1)
        quarter = max(len(segments) // 4, 1)
        mid_start = max(0, len(segments) // 2 - quarter // 2)
        windows = [
            segments[:quarter],
            segments[mid_start:mid_start + quarter],
            segments[-quarter:],
        ]

        lines: list[str] = []
        for i, window in enumerate(windows):
            # The CLOSING window fills backwards, so what the model sees is
            # the video's actual ending — the payoff, the CTA, the last beat.
            # Filling it forwards would show the start of the final quarter
            # and stop, which is just a fourth truncation point.
            from_end = (i == len(windows) - 1)
            ordered = reversed(window) if from_end else window
            rendered: list[str] = []
            used = 0
            for s in ordered:
                line = f"[{s['start']:.1f}s - {s['end']:.1f}s] {s.get('text', '')}"
                # +1 for the newline "\n".join adds — without it the joined
                # result overshoots the caller's max_chars.
                if used + len(line) + 1 > per_window:
                    break
                rendered.insert(0, line) if from_end else rendered.append(line)
                used += len(line) + 1
            if not rendered:
                continue
            if lines:
                lines.append(_OMITTED)
            lines.extend(rendered)
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


# The only keys the metadata prompts ask for, and the only ones any consumer
# reads (see run_extract_clips' GeneratedVideo(...) construction).
_METADATA_KEYS = ("youtube_title", "youtube_description", "youtube_tags", "tiktok_title")


def _metadata_only(metadata) -> dict:
    """Keep just the four metadata fields; drop anything else the model added.

    The clip result dict is assembled with the metadata spread LAST, so an
    unfiltered merge let a hallucinated key silently overwrite a
    pipeline-owned field — `video_path` (what we persist and serve),
    `start`/`end`/`duration_seconds`, or `caption_status`. Filtering at the
    point where model output joins our own dict is the one choke point that
    covers every metadata path (batched, per-clip fallback, and
    `_default_metadata`).
    """
    if not isinstance(metadata, dict):
        return {}
    return {k: metadata[k] for k in _METADATA_KEYS if k in metadata}


def _default_metadata(title: str, transcript_text: str) -> dict:
    """Default metadata when AI generation fails."""
    return {
        "youtube_title": title,
        "youtube_description": transcript_text[:200] if transcript_text else "",
        "youtube_tags": [],
        "tiktok_title": title,
    }


def _salvage_truncated_array(text: str):
    """The complete objects of a JSON array that was cut off mid-element (an
    output-token ceiling hit on a long clip list) — `[{..}, {..}, {..` →
    the first two. None when there is no array or nothing complete in it."""
    start = text.find("[")
    if start < 0:
        return None
    body = text[start:]
    end = body.rfind("}")
    while end > 0:
        try:
            out = json.loads(body[: end + 1] + "]")
            return out if isinstance(out, list) and out else None
        except json.JSONDecodeError:
            end = body.rfind("}", 0, end)
    return None
