# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Silence/filler removal constants and the caption retiming that follows a cut. Pure functions; the ffmpeg
pass itself (_remove_silence_and_fillers) stays in clip_extractor.py. Split out of clip_extractor.py;
clip_extractor re-exports every name, so import from either."""


def _trim_wordless_cue(text: str, seg_start: float, seg_end: float,
                       clip_start: float, clip_end: float) -> str:
    """Drop the parts of a WORDLESS cue that fall outside the clip.

    A cue WITH word timings needs none of this — the word filter below drops
    out-of-range words exactly. A cue WITHOUT them (an imported SRT/VTT, and
    every YouTube subtitle track, because the inline `<00:00:05.120><c>` word
    timestamps are stripped when the track is parsed) carries only a start/end
    and a blob of text, and `caption_service._extract_word_timestamps` spreads
    that text EVENLY across the cue.

    So when a cut lands mid-cue, clamping the cue's start to 0 while keeping all
    of its text renders words spoken BEFORE the cut from the clip's very first
    frame — burned captions running ahead of the audio by up to a full cue
    length, 3-8s for YouTube's rolling cues. It is worst on manual and bench
    cuts, which start mid-cue by construction.

    The fix applies the SAME uniform-distribution assumption the even-spread
    builder already makes: if N words are going to be spread evenly over the
    cue, the fraction of the cue preceding the cut holds that fraction of the
    words. An approximation, but the one already in force downstream, so it is
    consistent rather than newly clever. Returns "" when nothing survives, and
    the caller then skips the cue.
    """
    dur = seg_end - seg_start
    toks = (text or "").split()
    if dur <= 0 or not toks:
        return text or ""
    lead = max(0.0, (clip_start - seg_start) / dur)
    tail = max(0.0, (seg_end - clip_end) / dur)
    if lead <= 0 and tail <= 0:
        return text
    first = int(round(lead * len(toks)))
    last = len(toks) - int(round(tail * len(toks)))
    if last <= first:
        return ""
    return " ".join(toks[first:last])


def _filter_and_offset_segments(segments: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    """Filter segments to clip range and offset timestamps to start at 0."""
    filtered = []
    for s in segments:
        seg_start = s.get("start", 0)
        seg_end = s.get("end", 0)
        # Include if segment overlaps with clip range
        if seg_end > clip_start and seg_start < clip_end:
            adjusted = dict(s)
            # A wordless cue straddling either cut point: drop the text that
            # belongs outside the clip BEFORE the start gets clamped to 0.
            if not s.get("words") and (seg_start < clip_start or seg_end > clip_end):
                kept = _trim_wordless_cue(
                    s.get("text", ""), seg_start, seg_end, clip_start, clip_end)
                if not kept.strip():
                    continue
                adjusted["text"] = kept
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


# Max gap between words (seconds) before it's considered silence worth removing
SILENCE_GAP_THRESHOLD = 0.4


# Minimum keep-segment duration to avoid micro-fragments
MIN_KEEP_DURATION = 0.15


# Padding around kept segments to avoid harsh cuts
KEEP_PAD_SECONDS = 0.03


# The select/aselect pass re-encodes the WHOLE timeline, so its runtime scales
# with the source, not with how much gets cut. A flat 120 s cap was fine while
# this only ever saw 30-60 s clipper clips; the Silence Remover TOOL feeds it
# whole videos and podcasts, where the cap fired as a raw TimeoutExpired
# carrying the entire ffmpeg argv into the UI. Budget ~6x realtime —
# comfortably slower than any machine we support — with a floor for short clips
# and a 30 min ceiling so a pathological input can't pin a worker forever.
SILENCE_TIMEOUT_FLOOR_S = 300


SILENCE_TIMEOUT_CEILING_S = 1800


SILENCE_TIMEOUT_REALTIME_FACTOR = 6


# Below this share of the clip surviving, the trim has changed the artifact
# enough that the user needs to hear about it. 0.5 is a judgement call, not a
# measurement — tune it freely; nothing branches on it but the warning.
DRASTIC_TRIM_KEEP_RATIO = 0.5


def _silence_removal_timeout(duration_s: float) -> int:
    """ffmpeg wall-clock budget for a select/aselect pass over `duration_s`."""
    budget = float(duration_s or 0) * SILENCE_TIMEOUT_REALTIME_FACTOR
    return int(min(max(budget, SILENCE_TIMEOUT_FLOOR_S), SILENCE_TIMEOUT_CEILING_S))


def _retime_segments_after_removal(segments: list[dict], keep_ranges: list[tuple[float, float]]) -> list[dict]:
    """
    Re-calculate segment timestamps after silence removal.
    Maps original timestamps to their new positions in the shortened video.
    """
    def map_time(t: float) -> float | None:
        """Map an original timestamp to its position in the cleaned video."""
        elapsed = 0.0
        for rs, re_ in keep_ranges:
            if t < rs:
                # Before this range — clamp to start of range
                return elapsed
            if t <= re_:
                # Inside this range
                return elapsed + (t - rs)
            elapsed += re_ - rs
        # After all ranges
        return elapsed

    adjusted = []
    for seg in segments:
        new_start = map_time(seg["start"])
        new_end = map_time(seg["end"])
        if new_start is None or new_end is None or new_end <= new_start:
            continue

        new_seg = dict(seg)
        new_seg["start"] = new_start
        new_seg["end"] = new_end

        if "words" in new_seg:
            new_words = []
            for w in new_seg["words"]:
                ws = map_time(w["start"])
                we = map_time(w["end"])
                if ws is not None and we is not None and we > ws:
                    new_words.append({**w, "start": ws, "end": we})
            new_seg["words"] = new_words
            if not new_words:
                continue

        adjusted.append(new_seg)

    return adjusted
