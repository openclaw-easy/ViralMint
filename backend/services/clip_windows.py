# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The Clipper's window math: timestamps, manual windows, silent gaps, sentence snapping, overlap and topic
dedupe, scoring. Pure functions over segments and clip dicts. Split out of clip_extractor.py;
clip_extractor re-exports every name, so import from either."""
import json
import logging
import math
import re

# The clip_extractor logger name, so existing log filters and caplog checks keep matching.
logger = logging.getLogger('backend.services.clip_extractor')


# ── Manual-mode helpers ──────────────────────────────────────────────────────

def _parse_timestamp(value) -> float:
    """Parse a flexible timestamp string into seconds.

    Accepts:
      - numeric input          (45, 45.5 → 45.0, 45.5)
      - "SS" / "SS.fff"        ("18" → 18.0, "18.5" → 18.5)
      - "MM:SS" / "MM:SS.fff"  ("10:38" → 638.0)
      - "HH:MM:SS"             ("1:02:15" → 3735.0)

    Raises ValueError with a user-readable message on bad input. The
    API layer catches and surfaces as a 400 with the original message
    so the user knows which range failed.
    """
    if value is None:
        raise ValueError("empty timestamp")
    if isinstance(value, (int, float)):
        out = float(value)
        # NaN slips through every later comparison (end<=start, beyond-EOF
        # are all False against NaN) and reaches ffmpeg as `-ss nan`, turning
        # a bad range into an opaque "No clips were extracted" job failure
        # instead of this 400. json.loads accepts bare NaN/Infinity.
        if out < 0:
            raise ValueError(f"negative timestamp: {value}")
        if not math.isfinite(out):
            raise ValueError(f"non-finite timestamp: {value}")
        return out
    s = str(value).strip()
    if not s:
        raise ValueError("empty timestamp")
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError(
            f"too many ':' in timestamp '{s}' "
            f"(expected SS, MM:SS, or HH:MM:SS)"
        )
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(
            f"non-numeric component in timestamp '{s}' "
            f"(expected digits and optional '.fff')"
        )
    if any(n < 0 for n in nums):
        raise ValueError(f"negative component in timestamp '{s}'")
    # MM and SS must be < 60 in the multi-part forms; reject "70:00" rather
    # than silently converting to 70 minutes (which is what a naive
    # implementation would do but is almost always a user typo).
    if len(nums) >= 2 and nums[-1] >= 60:
        raise ValueError(f"seconds field ≥ 60 in '{s}'")
    if len(nums) == 3 and nums[1] >= 60:
        raise ValueError(f"minutes field ≥ 60 in '{s}'")
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] * 3600 + nums[1] * 60 + nums[2]


def _build_manual_clip_windows(
    time_ranges: list[dict],
    duration: float,
    title: str = "Untitled",
) -> list[dict]:
    """Convert validated time ranges into clip_windows for _process_clips_parallel.

    `time_ranges` is `[{"start": float, "end": float}, ...]` — already
    parsed + validated by the API layer (so we don't re-validate here;
    just defensively clamp `end` to source duration in case the row's
    cached duration drifted from the actual file).

    Skips virality / hook fields — manual mode has no AI judgment to
    inject. Each window gets a generic title; task_runner's
    `_clip_title` overrides this with the AI-batch's `youtube_title`
    when batched metadata generation succeeds, otherwise falls back to
    "{source} — clip N" via its existing fallback chain. The
    `clip_virality_score` / `clip_hook_score` / `clip_hook_type`
    columns persist as NULL for manual clips.
    """
    windows = []
    for i, r in enumerate(time_ranges):
        start = float(r["start"])
        end = float(r["end"])
        if duration and end > duration:
            end = duration
        windows.append({
            "start": round(start, 1),
            "end": round(end, 1),
            "title": f"{title} — clip {i + 1}",
            # Tagged at the source. Without this the downstream default ("ai")
            # would report every bench cut and every explicit time range as an
            # AI pick — to the caller least likely to believe it.
            "selection": "manual",
            "hook": "",
            "reason": "",  # explicitly empty so task_runner's _clip_title
                          # falls through to the "clip N" branch when batched
                          # AI metadata fails.
            # virality_score / hook_score deliberately omitted — they default
            # to None downstream, which the persistence layer stores as NULL.
        })
    return windows


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
    # An explicit budget of zero means "no room for backfill". The cap was
    # applied as a slice AFTER the first candidate was built, so budget=0
    # still returned one window — a clip the caller didn't ask for. Not
    # reachable from today's only caller (it invokes this when
    # `len(clip_windows) < max_clips`, so the budget is at least 1), but it
    # would be the moment a second caller appears.
    if budget is not None and budget <= 0:
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


def _parse_segments(raw: str | None, video_id: str) -> list[dict] | None:
    """Cached segments, or None when there are none worth using."""
    if not raw:
        return None
    try:
        segments = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"Corrupt transcript_segments_json for {video_id[:8]}, re-transcribing")
        return None
    return segments or None


# Max distance (seconds) we'll slide a clip's start/end to land on a natural
# sentence boundary. ±2s is wide enough to catch the "cut mid-word" failure
# mode the AI commonly produces. Both ends can move, so a snap can change a
# clip's length by up to twice this: _snap_to_sentence_boundaries checks the
# user's pinned min/max itself rather than trusting the slide to be small.
_MAX_BOUNDARY_SLIDE_SEC = 2.0


def _snap_to_sentence_boundaries(
    windows: list[dict], segments: list[dict],
    max_duration: int | None = None,
    min_duration: int | None = None,
) -> list[dict]:
    """Nudge each clip's start/end onto the nearest Whisper segment edge if
    one is within ±_MAX_BOUNDARY_SLIDE_SEC.

    Whisper produces segments at sentence-like boundaries (utterance pauses,
    punctuation), so snapping to a segment edge is equivalent to snapping
    to a natural sentence boundary.

    `max_duration` is the user's pinned ceiling, when they set one. Each end
    may slide up to ±_MAX_BOUNDARY_SLIDE_SEC, so a two-sided snap can add up
    to 2× that — enough to push a clip that was exactly at the ceiling over
    it (a 20s window became 22s). When the two-sided snap would break the
    ceiling we fall back through end-only (the end matters most — snapping
    it is what stops a cut landing before the punchline), then start-only,
    then the original window. A candidate is also accepted if it's no longer
    than the input, so a window that ALREADY exceeded the ceiling can still
    be tidied.

    `min_duration` is the same rule for the floor: snapping both ends inward
    can shorten a clip by up to 2× the slide (a 20s window became 16s under
    a pinned 20s minimum), so a candidate shorter than the floor is skipped
    unless the input was already that short.

    Returns a NEW list — the input windows are not mutated.
    """
    if not segments or not windows:
        return windows

    # Pre-extract sorted boundary timestamps (start of each segment + end of
    # the last one) so the snap is a binary search over a small fixed set.
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

        orig_len = orig_end - orig_start
        # Candidates in preference order: both ends, end-only, start-only.
        # The original is the implicit last resort below.
        candidates = [
            (new_start if new_start is not None else orig_start,
             new_end if new_end is not None else orig_end),
            (orig_start, new_end if new_end is not None else orig_end),
            (new_start if new_start is not None else orig_start, orig_end),
        ]

        chosen = None
        for cand_start, cand_end in candidates:
            cand_len = cand_end - cand_start
            # Never collapse a clip. (Pathological inputs where start == end
            # after snap are rare but cheap to defend against.)
            if cand_len < 1.0:
                continue
            # Respect a pinned ceiling, unless the input already broke it —
            # then "no worse than we started" is the bar.
            if max_duration and cand_len > max_duration and cand_len > orig_len:
                continue
            # The same for a pinned floor.
            if min_duration and cand_len < min_duration and cand_len < orig_len:
                continue
            chosen = (cand_start, cand_end)
            break

        if chosen is None:
            snapped.append(w)
            continue

        nw = dict(w)
        nw["start"] = round(chosen[0], 1)
        nw["end"] = round(chosen[1], 1)
        snapped.append(nw)

    return snapped


def _remove_overlapping_clips(windows: list[dict]) -> list[dict]:
    """Remove overlapping clips, keeping higher-scored ones (assumed pre-sorted by score desc).

    Bounds are compared numerically, and every window here came from an LLM.
    `_select_clip_windows` does coerce them before returning, so today no
    caller can hand this a null — but that is a property of one function
    upstream, not of this one, and a missing bound would raise a TypeError
    that fails the ENTIRE search rather than dropping one bad pick. Skip what
    cannot be compared; a window with no start is not a clip.
    """
    kept = []
    for w in windows:
        try:
            ws, we = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            logger.info("Dropping a clip window with unusable bounds: %r",
                        {k: w.get(k) for k in ("start", "end")} if isinstance(w, dict) else w)
            continue
        overlaps = False
        for k in kept:
            # Check if they overlap (with 2s tolerance)
            if ws < k["end"] - 2 and we > k["start"] + 2:
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


# ── Score breakdown normalization ────────────────────────────────────────────
# The AI returns a 4-key dict for `score_breakdown` (flow / value / trend /
# shareability), but rarely it omits keys or returns wrong types. Normalizing
# in a small pure helper means the validation is testable without faking the
# whole AI call.

_SCORE_BREAKDOWN_KEYS = ("flow", "value", "trend", "shareability")


def _normalize_score_breakdown(raw, default_score: float) -> dict[str, float]:
    """Coerce the AI's score_breakdown payload into a 4-key float dict.

    `default_score` is the clip's overall virality_score — used when the AI
    omits a sub-score so the UI bars don't collapse to zero on partial AI
    responses. Each value is clamped to [1.0, 10.0]; non-numeric values
    fall back to the default. A non-dict input returns {} (the UI hides
    the panel for that clip).
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key in _SCORE_BREAKDOWN_KEYS:
        try:
            v = float(raw.get(key, default_score))
        except (TypeError, ValueError):
            v = default_score
        out[key] = max(1.0, min(10.0, v))
    return out


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
