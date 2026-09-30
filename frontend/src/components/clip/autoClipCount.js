// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
/* ── What Auto-cut will actually cut ───────────────────────────
   Mirrors `auto_clip_count` in backend/api/downloaded.py, pinned across the
   two by tests/test_auto_clip_count_parity.py. If that test is failing, the
   dialog is promising a number the runner will not honour.

   Why it exists at all: leaving "Clips (max)" blank means "~1 per 30s", so a
   22-minute source silently means 43 clips — 43 renders, 43 Whisper-timed
   caption burns, and a Library page you then have to weed. The dialog showed
   no figure at all, so pressing Extract on an empty form was agreeing to that
   without ever being shown it.
*/
export const AUTO_CLIP_SECONDS_PER_CLIP = 30
export const AUTO_CLIP_MIN = 3
export const AUTO_CLIP_MAX = 99
export const AUTO_CLIP_UNKNOWN_DURATION = 5

/** How many clips Auto-cut aims for when the user names no number. */
export function autoClipCount(durationSeconds) {
  if (!durationSeconds || durationSeconds <= 0) return AUTO_CLIP_UNKNOWN_DURATION
  return Math.max(
    AUTO_CLIP_MIN,
    Math.min(AUTO_CLIP_MAX, Math.floor(durationSeconds / AUTO_CLIP_SECONDS_PER_CLIP)),
  )
}

/**
 * The figure to put in front of the user before they press the button.
 *
 * An UPPER bound, and worded as one: the backend scales the count down when
 * the video has less quality material than the budget allows, so the run can
 * produce fewer — never more.
 */
export function autoCutEstimate({ durationSeconds, requested, minDurationSeconds }) {
  const asked = requested
    ? Math.max(1, Math.min(AUTO_CLIP_MAX, requested))
    : autoClipCount(durationSeconds)
  // A length floor is also a count ceiling: clips don't overlap
  // (_remove_overlapping_clips), so only `duration // floor` of them fit. The
  // backend already reasons this way — clip_extractor's
  // `min_clip_floor = max(10, min_duration)`, `duration_based_max =
  // duration // min_clip_floor`, then "use the more restrictive estimate" —
  // and without it "Min 60s" on a 14-minute source still quoted "up to 28
  // clips", which is 28 minutes of material.
  //
  // Only applied when the user NAMES a minimum. The backend has a default
  // floor too, but guessing it here could promise fewer than the run
  // produces, and "up to" must never be an underestimate.
  const floor = minDurationSeconds > 0 ? Math.max(10, minDurationSeconds) : 0
  const fit = floor > 0 && durationSeconds > 0
    ? Math.max(1, Math.floor(durationSeconds / floor))
    : Infinity
  const clips = Math.min(asked, fit)
  return {
    clips,
    // "up to" because the backend may return fewer; never because we're unsure.
    label: `up to ${clips} clip${clips === 1 ? "" : "s"}`,
  }
}
