// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { useCallback, useEffect, useRef, useState } from "react"
import { storageKey } from "../../../utils/storage"

/* ── Bench range state ────────────────────────────────────────
   The cutting bench's model is a list of pending cuts on ONE source.
   Everything here is free and local: nothing in this hook talks to the
   backend, so dragging a handle is instant and can be undone by dragging it
   back. Cutting is the only step that produces anything.

   Caps mirror the backend so the UI can't compose a request the API
   will reject:
     MAX_RANGES  ↔ downloaded._MANUAL_MAX_RANGES (10)
     (pinned by tests/test_clip_extractor_manual_mode.py — it was
      deliberately lowered from 20 in May, so raise it there first)
     MIN_LEN_SEC ↔ downloaded._MANUAL_MIN_CLIP_SEC (1.0)
   If those move server-side, move them here — the drift shows up as a
   400 on submit, which is the one place we can't fix it for the user.
*/
export const MAX_RANGES = 10
export const MIN_LEN_SEC = 1.0

// A fresh range's target length. 30s is the middle of the AI picker's
// 15-60s auto band, so a range dropped with one click is already a
// plausible short rather than something you must resize before it's useful.
const DEFAULT_LEN_SEC = 30

let _seq = 0
const nextId = () => `r${++_seq}`

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))
const round3 = (v) => Math.round(v * 1000) / 1000

/**
 * Ranges pending on one source, plus the gestures that edit them.
 *
 * Work survives a source switch (and a page reload) via sessionStorage,
 * keyed per source: hopping to another video to check something and
 * coming back should not silently discard five hand-placed cuts.
 *
 * @param {string|null} sourceId  Downloaded-video id. Changing it swaps
 *   the whole working set.
 * @param {number} duration       Source duration in seconds; every edit
 *   is clamped into [0, duration].
 */
export default function useBenchRanges(sourceId, duration) {
  const [ranges, setRanges] = useState([])
  const [activeId, setActiveId] = useState(null)
  // Guards the restore→persist round trip: without it the first render
  // after a source switch persists the OUTGOING source's empty state
  // over the incoming source's saved ranges.
  const loadedFor = useRef(null)

  const skey = sourceId ? storageKey("clipper", `bench:${sourceId}`) : null

  // ── Restore on source change ───────────────────────────────
  useEffect(() => {
    if (!sourceId) {
      setRanges([])
      setActiveId(null)
      loadedFor.current = null
      return
    }
    let restored = []
    try {
      const raw = sessionStorage.getItem(skey)
      const parsed = raw ? JSON.parse(raw) : null
      if (Array.isArray(parsed)) {
        restored = parsed
          .filter((r) => r && Number.isFinite(r.start) && Number.isFinite(r.end) && r.end > r.start)
          // A source re-fetched shorter under the same row id restores blocks
          // past the end, and the backend 400s the ENTIRE Cut submit for one
          // stale range. Clamp like add() does (duration is the mount-time
          // row value — deliberately not a dep, a re-restore would clobber
          // live edits).
          .filter((r) => !(duration > 0) || r.start < duration)
          .map((r) => ({ ...r, end: duration > 0 ? Math.min(r.end, duration) : r.end }))
          .filter((r) => r.end - r.start >= MIN_LEN_SEC)
          .slice(0, MAX_RANGES)
          .map((r) => ({ id: nextId(), start: r.start, end: r.end, meta: r.meta || null }))
      }
    } catch {
      // Private mode / disabled storage — an empty bench is a fine
      // degradation, and a throw here would blank the whole page.
    }
    setRanges(restored)
    setActiveId(restored[0]?.id ?? null)
    loadedFor.current = sourceId
  }, [sourceId, skey])

  // ── Persist ────────────────────────────────────────────────
  useEffect(() => {
    if (!skey || loadedFor.current !== sourceId) return
    try {
      if (ranges.length) {
        sessionStorage.setItem(
          skey,
          JSON.stringify(ranges.map(({ start, end, meta }) => ({ start, end, meta }))),
        )
      } else {
        sessionStorage.removeItem(skey)
      }
    } catch { /* see above */ }
  }, [ranges, skey, sourceId])

  const atCap = ranges.length >= MAX_RANGES

  /** Insert a range, clamped and length-checked. Returns its id, or null
   *  when the cap is hit or the span is too short to be a clip.
   *  `meta` optionally records where the range came from (AI proposal). */
  const add = useCallback((start, end, meta = null) => {
    // Decide OUTSIDE the state updater. React runs an updater during the
    // render pass, not at dispatch, so a caller that reads a variable the
    // updater assigned is reading whatever the eager-bailout path happened
    // to leave behind. `addAt` needs a truthful answer to tell the user
    // "no room here", so the check and the id are computed here and the
    // updater only appends.
    // `meta` is the AI's reason for proposing this span. It rides along so
    // an adopted suggestion stays labelled as one rather than becoming an
    // anonymous blue block — the user should always be able to see which
    // cuts were their own idea.
    if (ranges.length >= MAX_RANGES) return null
    let lo = clamp(Math.min(start, end), 0, duration)
    let hi = clamp(Math.max(start, end), 0, duration)
    // Clamp into the free gap around `lo`, for the reason `addAt`/`addMany`
    // spell out: manual mode cuts VERBATIM, so a drag that crossed an
    // existing block would stack a second cut over the same seconds — two
    // near-identical clips from one Cut. The N key, paste and AI adoption
    // all refused overlaps; drag-create was the one door left open (a drag
    // across a pending block landed 4:13→7:01 on top of 5:37→8:25).
    // Timeline clamps the DRAFT the same way, so what the user sees
    // mid-drag is what lands.
    const sorted = [...ranges].sort((a, b) => a.start - b.start)
    const covering = sorted.find((r) => lo >= r.start && lo < r.end)
    if (covering) lo = covering.end
    const nextBlock = sorted.find((r) => r.start >= lo && r.end > lo)
    if (nextBlock) hi = Math.min(hi, nextBlock.start)
    if (hi - lo < MIN_LEN_SEC) return null

    const created = { id: nextId(), start: round3(lo), end: round3(hi), meta }
    setRanges((prev) => (
      // Chronological order matches the backend's own sort, so the numbers
      // on the timeline are the numbers on the output clips.
      prev.length >= MAX_RANGES
        ? prev
        : [...prev, created].sort((a, b) => a.start - b.start)
    ))
    setActiveId(created.id)
    return created.id
  }, [duration, ranges])

  /** Drop a range at `t`, sized to whatever room is free from there on.
   *
   *  Pressing N with the playhead inside an existing range used to stack a
   *  second block exactly on top of the first — two ranges reading "2:18 →"
   *  in the list and one visible block on the timeline. Manual mode cuts
   *  ranges verbatim (overlap removal is AI-mode only), so that quietly
   *  bought two near-identical clips. Start after whatever covers `t`, and
   *  stop at whatever starts next.
   */
  const addAt = useCallback((t) => {
    const sorted = [...ranges].sort((a, b) => a.start - b.start)
    let cursor = clamp(t, 0, duration)

    // Walk forward to the first gap that can actually hold a clip. One
    // pass isn't enough: a playhead parked a fraction of a second before
    // an existing block finds a gap that "exists" but is 0.06s wide, and
    // refusing there is technically right and practically useless. Keep
    // stepping over blocks until a real gap turns up.
    while (cursor < duration) {
      const covering = sorted.find((r) => cursor >= r.start && cursor < r.end)
      if (covering) { cursor = covering.end; continue }
      const nextStart = sorted.find((r) => r.start > cursor)?.start ?? duration
      if (nextStart - cursor >= MIN_LEN_SEC) {
        return add(cursor, Math.min(nextStart, cursor + DEFAULT_LEN_SEC))
      }
      if (nextStart >= duration) break
      cursor = nextStart   // gap too small — the loop steps over its block next
    }
    return null   // the rest of the video is already spoken for
  }, [add, duration, ranges])

  /** Adopt a batch of proposed spans. Appends what fits under the cap and
   *  reports how many landed, so the caller can tell the user plainly
   *  rather than silently dropping the tail.
   *
   *  Built OUTSIDE the state updater, for the reason `add` spells out: React
   *  runs an updater during the render pass, not at dispatch, so a count
   *  incremented in there is still 0 when this function returns. It shipped
   *  that way and every adoption reported "Added 0 of 3 moments — the bench
   *  holds 10", blaming a cap that had not been hit, while all three blocks
   *  were sitting on the timeline. A caller that reports a number to the user
   *  needs the number, so the work happens here and the updater only appends.
   */
  const addMany = useCallback((items) => {
    const accepted = []
    let room = MAX_RANGES - ranges.length
    // Overlap check, for the same reason `addAt` has one: manual mode cuts
    // ranges VERBATIM (overlap removal is an AI-mode step), so two blocks
    // covering the same seconds produce two near-identical clips. The N key
    // refused to stack from the start; paste and AI adoption did not, so a
    // pasted chapter list with one duplicated line silently rendered the same
    // clip twice. Compare against what is already down AND against what this
    // batch has taken, or a paste can overlap itself.
    const taken = [...ranges]
    const clashes = (lo, hi) => taken.some((r) => lo < r.end && hi > r.start)
    for (const it of items) {
      if (room <= 0) break
      const lo = clamp(Math.min(it.start, it.end), 0, duration)
      const hi = clamp(Math.max(it.start, it.end), 0, duration)
      if (hi - lo < MIN_LEN_SEC) continue
      if (clashes(lo, hi)) continue
      const created = { id: nextId(), start: round3(lo), end: round3(hi), meta: it.meta || null }
      accepted.push(created)
      taken.push(created)
      room -= 1
    }
    if (!accepted.length) return 0
    setRanges((prev) => (
      // Re-check against `prev`: the closure's `ranges` is a render old, and
      // two adoptions in one tick must not push past the cap.
      [...prev, ...accepted.slice(0, Math.max(0, MAX_RANGES - prev.length))]
        .sort((a, b) => a.start - b.start)
    ))
    setActiveId((cur) => cur ?? accepted[0].id)
    return accepted.length
  }, [duration, ranges])

  const remove = useCallback((id) => {
    // Same rule as above: decide the next selection here, not inside the
    // updater. An updater that calls another setter is not pure, and React
    // is free to run it more than once.
    setActiveId((cur) => (cur === id
      ? (ranges.find((r) => r.id !== id)?.id ?? null)
      : cur))
    setRanges((prev) => prev.filter((r) => r.id !== id))
  }, [ranges])

  const clear = useCallback(() => {
    setRanges([])
    setActiveId(null)
  }, [])

  /** Edit one range. Rejects sub-minimum spans by CLAMPING rather than
   *  refusing: a handle that stops moving reads as a stuck UI, while a
   *  handle that refuses to cross its partner reads as a floor. */
  const update = useCallback((id, patch) => {
    setRanges((prev) => prev.map((r) => {
      if (r.id !== id) return r

      // The free gap around this block. `add`/`addAt`/`addMany` each refuse to
      // stack — manual mode cuts VERBATIM, so two blocks over the same seconds
      // are two near-identical clips — but `update`, which is what a resize
      // drag, a whole-block move AND the rail's editable timecodes all call,
      // only clamped to [0, duration] and never looked at the other blocks. So
      // dragging one block onto its neighbour, or typing an overlapping time,
      // stacked them and the backend cut both.
      //
      // Bounded by the gap rather than refused, per this function's own rule: a
      // handle that stops moving reads as a stuck UI, while a handle that parks
      // against its neighbour reads as a floor. Ranges never overlap (that is
      // the invariant), so every other block sits wholly left or wholly right
      // of where this one currently is, and its ORIGINAL bounds decide which —
      // which keeps the limits stable for the whole drag.
      let lo = 0
      let hi = duration
      for (const o of prev) {
        if (o.id === id) continue
        if (o.end <= r.start + 1e-6) lo = Math.max(lo, o.end)
        else if (o.start >= r.end - 1e-6) hi = Math.min(hi, o.start)
      }

      let start = patch.start != null ? patch.start : r.start
      let end = patch.end != null ? patch.end : r.end
      const len = end - start
      if (patch.start != null && patch.end != null && Math.abs(len - (r.end - r.start)) < 1e-6) {
        // Whole-block move: preserve length while sliding into bounds,
        // so dragging a block off the edge parks it rather than squashing it.
        const span = r.end - r.start
        // Park against the neighbour instead of sliding over it. If the gap
        // somehow cannot hold the block, leave it where it was rather than
        // emitting an overlap.
        if (hi - lo < span) return r
        start = clamp(start, lo, hi - span)
        end = start + span
      } else {
        start = clamp(start, lo, hi)
        end = clamp(end, lo, hi)
        if (end - start < MIN_LEN_SEC) {
          // Grow within the gap, and only in the direction that still fits —
          // otherwise a resize at a tight boundary would push past a neighbour
          // just to satisfy the minimum length.
          if (patch.start != null && patch.end == null) start = Math.max(lo, end - MIN_LEN_SEC)
          else end = Math.min(hi, start + MIN_LEN_SEC)
          if (end - start < MIN_LEN_SEC) return r
        }
      }
      return { ...r, start: round3(start), end: round3(end) }
    }))
  }, [duration])

  /** Commit a drag: re-sort so the on-screen numbering matches the order
   *  the backend will use. Called on pointerup, never mid-drag — a block
   *  that renumbers under the cursor is disorienting. */
  const settle = useCallback(() => {
    setRanges((prev) => [...prev].sort((a, b) => a.start - b.start))
  }, [])

  const active = ranges.find((r) => r.id === activeId) || null

  return {
    ranges, activeId, active, atCap,
    setActiveId, add, addAt, addMany, remove, update, clear, settle,
  }
}
