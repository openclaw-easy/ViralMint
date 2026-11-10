// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
// Smart Video Studio — the shared scene model.
//
// Pure helpers only: derived timing, critic notes, labels, and the script
// packing rules the backend uses (studio_doc.py).
import { narrationStats } from "../../utils/spokenLength"

// ── Enums shared by every Smart Video surface (one module per side, derive, don't duplicate;
// the backend twins live in studio_doc.py). ─────────────────────────────────
export const ASPECTS = ["9:16", "16:9", "1:1"]
const ASPECT_RATIO = { "9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1 }
export const aspectValue = (r) => ASPECT_RATIO[r] || 9 / 16
// The CSS `aspect-ratio` for a project's aspect ("9 / 16").
export const aspectCss = (r) => (ASPECTS.includes(r) ? r.replace(":", " / ") : "9 / 16")
// One TTS call per scene (D2): a scene's line stays ≤ 400 characters.
export const LINE_MAX = 400

export const KIND_META = {
  stock: { label: "Stock", short: "stock", color: (t) => t.palette.grey[500] },
  user_clip: { label: "Your clip", short: "yours", color: (t) => t.palette.success.main },
}

export const VOICE_STATE = {
  none: { label: "not voiced", tone: "default" },
  generating: { label: "voicing…", tone: "info" },
  ready: { label: "ready", tone: "success" },
  stale: { label: "line or voice changed", tone: "warning" },
  failed: { label: "failed", tone: "error" },
}

export const FIT_OPTIONS = [
  { value: "loop", label: "Loop", hint: "repeat the clip to fill the line" },
  { value: "freeze", label: "Freeze", hint: "hold the last frame" },
  { value: "slow", label: "Slow", hint: "play slower (down to 0.75×)" },
]

export const TRANSITIONS = [
  { value: "none", label: "Cut" },
  { value: "smooth", label: "Smooth" },
  { value: "dynamic", label: "Dynamic" },
  { value: "punchy", label: "Punchy" },
]

// CSS stand-ins for the render's LUT per visual style — preview only; the
// render grades with ffmpeg (stock_video_service.LUT_PRESETS).
export const STYLE_FILTER = {
  cinematic: "contrast(1.08) saturate(1.12) sepia(0.08) hue-rotate(-6deg)",
  documentary: "contrast(1.04) saturate(0.92)",
  vlog: "sepia(0.18) saturate(1.05) brightness(1.02)",
  card: "brightness(0.82) contrast(1.15) saturate(0.7)",
  educational: "contrast(1.03)",
  noir: "grayscale(1) contrast(1.22)",
  neon: "saturate(1.45) contrast(1.1) hue-rotate(12deg)",
  vintage: "sepia(0.35) saturate(0.85) contrast(1.03)",
}

export const TRANSITION_VALUES = TRANSITIONS.map((t) => t.value)

// ── Small shared presentation helpers ─────────────────────────────────────
export function hashString(s) {
  let h = 0x811c9dc5
  const str = String(s || "")
  for (let i = 0; i < str.length; i += 1) { h ^= str.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0 }
  return h
}

// A stable placeholder gradient for a tile with no thumbnail.
export function queryGradient(q) {
  const h = hashString(q || "") % 360
  return `linear-gradient(135deg, hsl(${h} 38% 34%), hsl(${(h + 40) % 360} 42% 16%))`
}

export const providerLabel = (provider) => (provider === "pixabay" ? "Pixabay" : "Pexels")
// "Pexels · Jane Doe" — how a stock candidate is named on tiles and previews.
export const stockLabel = (asset) =>
  `${providerLabel(asset?.provider)}${asset?.creator ? ` · ${asset.creator}` : ""}`

// Is this clip card what the scene already shows? Stock by provider + id, your
// own clips by asset id.
export const clipInUse = (scene, clip) => {
  const v = scene?.visual
  const a = v?.asset
  if (!a || !clip) return false
  if (v.kind === "stock") return a.provider_id != null && clip.provider === a.provider && String(clip.provider_id) === String(a.provider_id)
  if (v.kind === "user_clip") return !!a.asset_id && clip.asset_id === a.asset_id
  return false
}

// ── Voices (D3, D8) ────────────────────────────────────────────────────────
// Edge TTS runs locally and fast, so a changed line re-records by itself.
// OpenAI TTS runs on the user's own key: it records only when asked (Voice
// this scene / Voice all).
export const voiceProvider = (settings) => settings?.voice?.provider || "edge_tts"
export const autoRevoices = (provider) => (provider || "edge_tts") === "edge_tts"
export const voiceKindLabel = (provider) => (provider === "openai_tts" ? "OpenAI voice" : "Edge voice")

// ── Music: one label for every surface ─────────────────────────────────────
// The library track a bed actually plays, or null (off / not picked).
export function musicTrackName(music) {
  const m = music || {}
  if (!m.enabled) return null
  return m.track ? (m.track_name || m.track) : null
}
export function musicLabel(music) {
  const m = music || {}
  if (!m.enabled) return "Off"
  return musicTrackName(m) || "No track picked"
}

export const newUid = () =>
  `u${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`

// The opening title (studio_doc.hook_title / HOOK_SECONDS): the user's text,
// else the first spoken line, only while captions and the hook are on.
export const HOOK_SECONDS = 2.5
export const HOOK_TEXT_MAX = 80
export const HOOK_AUTO_MAX = 48
const squash = (s) => String(s || "").split(/\s+/).filter(Boolean).join(" ")
export function hookTitle(settings, scenes) {
  const c = settings?.captions
  if (!c?.enabled || !c?.hook) return null
  const own = squash(c.hook_text)
  if (own) return clipTitle(own)
  const first = squash((scenes || []).find((s) => squash(s?.line))?.line)
  return first ? autoTitle(first) || null : null
}
// studio_doc.auto_title: a first line's first sentence when it fits
// HOOK_AUTO_MAX, else its opening clause.
export function autoTitle(line) {
  let text = squash(line)
  const first = splitSentences(text)[0] || text
  if (first.length <= HOOK_AUTO_MAX) return first.length > 1 && /[.。]$/.test(first) ? first.slice(0, -1) : first
  const m = /[,;:!?.。！？，；：—]/.exec(text)
  if (m && m.index >= 12) text = "!?！？".includes(text[m.index]) ? text.slice(0, m.index + 1) : text.slice(0, m.index)
  return clipTitle(text, HOOK_AUTO_MAX)
}
// studio_doc.hook_is_auto: the title is the first line's own words, so the
// captions under it wait until it has gone (no text said twice on screen).
export function hookIsAuto(settings, scenes) {
  const c = settings?.captions
  return !!(c?.enabled && c?.hook && !squash(c.hook_text) && hookTitle(settings, scenes))
}
// studio_doc.clip_title: cut at the last word boundary with "…".
export function clipTitle(text, limit = HOOK_TEXT_MAX) {
  if (text.length <= limit) return text
  let cut = text.lastIndexOf(" ", limit - 2)
  if (cut < Math.floor(limit * 0.6)) cut = limit - 1
  return text.slice(0, cut).replace(/[ ,;:.—-]+$/, "") + "…"
}

export function estimateSeconds(line) {
  const s = narrationStats(line || "").seconds
  return Math.max(1.2, Number.isFinite(s) && s > 0 ? s : 1.2)
}

// D10: a voiced scene lasts exactly its take (+ tail gap); an unvoiced one is
// an estimate, and the UI says so.
export function sceneDuration(scene, settings) {
  // Only a READY take matches its line; a stale take was recorded for the old
  // one, so a stale scene is estimated from the line it has now.
  const v = scene?.voice || {}
  const voiced = v.state === "ready" && v.duration
  const base = voiced ? v.duration : estimateSeconds(scene?.line)
  return base + (settings?.tail_gap_s ?? 0.15)
}

// `derived` is the server's timeline for this same document (the envelope's
// `derived`, studio_doc.derive_timeline): when it matches, its durations are
// used, so unvoiced estimates agree with the server and the render instead of
// being re-estimated here with slightly different rounding.
export function withTimeline(doc, derived = null) {
  let t = 0
  const list = doc?.scenes || []
  const server = derived?.durations?.length === list.length ? derived.durations : null
  const scenes = list.map((s, i) => {
    const dur = server && Number.isFinite(server[i]) ? server[i] : sceneDuration(s, doc.settings)
    const out = {
      ...s, index: i, start: t, dur,
      estimated: s.voice?.state !== "ready",
    }
    t += dur
    return out
  })
  return { scenes, total: t }
}

// studio_doc.second_shots: a long, unpinned, bound stock scene cuts to an
// alternate no other scene shows, halfway through — the render does the same.
export const SECOND_SHOT_MIN_S = 5.0
export function secondShots(scenes) {
  const used = new Set((scenes || []).map((sc) => sc?.visual).filter((v) => v?.kind === "stock" && v.asset)
    .map((v) => v.asset.provider_id))
  const out = {}
  for (const sc of scenes || []) {
    const v = sc?.visual || {}
    if (v.kind !== "stock" || v.state !== "bound" || !v.asset || v.pinned || !(sc.dur >= SECOND_SHOT_MIN_S)) continue
    const alt = (v.alternates || []).find((a) => a?.provider_id && a.download_url && !used.has(a.provider_id))
    if (!alt) continue
    used.add(alt.provider_id)
    out[sc.uid] = { asset: alt, cut: Math.round((sc.dur / 2) * 100) / 100 }
  }
  return out
}

export function sceneAt(scenes, t) {
  if (!scenes?.length) return null
  let found = scenes[0]
  for (const s of scenes) {
    if (s.start <= t + 1e-6) found = s
    else break
  }
  return found
}

// A visual that is shorter than its line needs a fit (D11).
export const isShortVisual = (scene) => {
  const n = scene?.visual?.asset?.native_duration
  return !!n && ["stock", "user_clip"].includes(scene.visual.kind) && n + 0.05 < scene.dur
}

// Each finding carries the fix that resolves it" with
// fix chips): a note without a button is homework. `fix.kind` is what the
// editor does — split the scene, search stock for a longer clip, or go back
// to stock — all plain edits.
export function critique(timeline) {
  const out = []
  const scenes = timeline?.scenes || []
  if (!scenes.length) return out
  // Before a scene is voiced its `dur` is this module's own estimate from the
  // line length, and the rest of the editor labels that "est.". The critic
  // used to state it as measured fact, so a note could ask you to fix a number
  // nobody had measured and then vanish once the take landed. Say which it is.
  const secs = (sc) => `${sc.dur.toFixed(1)}s${sc.estimated ? " (estimated)" : ""}`
  if (scenes[0].dur > 3.2 && spokenUnits(scenes[0].line) >= 20) {
    out.push({ uid: scenes[0].uid, severity: "high", message: `The opening line runs ${secs(scenes[0])} — most viewers decide in the first 3. Split it or shorten the hook.`,
      fix: { kind: "split", label: "Split this line" } })
  }
  for (const sc of scenes) {
    if (sc.dur > 7 && spokenUnits(sc.line) >= 20) out.push({ uid: sc.uid, severity: "medium", message: `Scene ${sc.index + 1} holds one visual for ${secs(sc)}. Splitting it adds a cut.`,
      fix: { kind: "split", label: "Split (S)" } })
    if (isShortVisual(sc)) out.push({ uid: sc.uid, severity: "low", message: `Scene ${sc.index + 1}'s clip is ${(sc.dur - sc.visual.asset.native_duration).toFixed(1)}s shorter than its line — it will ${sc.visual.fit || "loop"}.`,
      fix: { kind: "search", label: "Find a longer clip", query: sc.visual.asset?.query || sc.visual.query || "" } })
    // The planner's vision pass saw nothing in this clip's results that
    // depicts the line (studio_planner `weak_id`); binding any other clip clears it.
    if (sc.visual?.kind === "stock" && sc.visual.weak_id && sc.visual.asset?.provider_id === sc.visual.weak_id) out.push({
      uid: sc.uid, severity: "medium", message: `Scene ${sc.index + 1}'s clip may not match its line.`,
      fix: { kind: "search", label: "Find a better clip", query: sc.visual.query || sc.visual.asset?.query || "" } })
    if (sc.visual?.state === "failed") out.push({ uid: sc.uid, severity: "high", message: `Scene ${sc.index + 1} has no usable visual.`,
      fix: { kind: "stock", label: "Use stock instead" } })
  }
  return out
}

export function visualSourceLabel(visual) {
  if (!visual) return "—"
  const a = visual.asset || {}
  switch (visual.kind) {
    case "stock": return `Stock · “${a.query || visual.query || ""}”`
    case "user_clip": return a.title || "Your clip"
    default: return visual.kind
  }
}

// ── Script → scene lines, and scene rules shared with the backend ─────────
// Mirrors backend/services/studio_doc.py (split_sentences / pack_scenes) and
// studio_ops (is_fixed_visual) rule for rule, so Home's live
// scene list and the editor's labels match what the server builds.

export const SCENE_MAX = 18

const normalise = (x) => String(x || "").replace(/\s+/g, " ").trim()

export function splitSentences(script) {
  return normalise(script).split(/(?<=[.!?。！？])\s*/).map((s) => s.trim()).filter(Boolean)
}

function splitLong(sentence) {
  if (sentence.length <= LINE_MAX) return [sentence]
  if (!/\s/.test(sentence)) return sentence.match(new RegExp(`[\\s\\S]{1,${LINE_MAX}}`, "g"))
  const out = []
  let cur = ""
  for (const w of sentence.split(/\s+/)) {
    const next = cur ? `${cur} ${w}` : w
    if (next.length > LINE_MAX && cur) { out.push(...splitLong(cur)); cur = w } else cur = next
  }
  if (cur) out.push(...splitLong(cur))
  return out
}

// studio_doc.spoken_units / join_sentences: scenes pack by speaking time, so a
// CJK character weighs ~4.3 Latin characters, and CJK sentences join without
// a space. SCENE_TARGET_MIN_CHARS matches the backend (was 80 here vs 60
// there — Home promised 14 scenes and the editor built 18, 2026-10-13).
export const SCENE_TARGET_MIN_CHARS = 60
export const CJK_CHAR_WEIGHT = 4.3
const CJK_CHAR = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\u3000-\u303f\uff00-\uffef]/
const isCjk = (ch) => !!ch && CJK_CHAR.test(ch)
export function spokenUnits(text) {
  const t = String(text || "")
  let cjk = 0
  for (const ch of t) if (isCjk(ch)) cjk += 1
  return ([...t].length - cjk) + cjk * CJK_CHAR_WEIGHT
}
export function joinSentences(a, b) {
  if (!a) return b
  if (!b) return a
  return isCjk(a[a.length - 1]) && isCjk(b[0]) ? `${a}${b}` : `${a} ${b}`
}

function groupScenes(sentences, target) {
  const total = sentences.reduce((a, s) => a + spokenUnits(s), 0)
  const t = target ?? Math.max(SCENE_TARGET_MIN_CHARS, Math.ceil(total / 18))
  const maxSentences = t > 160 ? 99 : 2
  const lines = []
  let cur = ""
  let count = 0
  for (const s of sentences) {
    const next = joinSentences(cur, s)
    if (cur && (spokenUnits(next) > t || next.length > LINE_MAX || count >= maxSentences)) {
      lines.push(cur); cur = s; count = 1
    } else {
      cur = next; count += 1
    }
  }
  if (cur) lines.push(cur)
  return lines
}

// Scene lines for a script; more than SCENE_MAX only when it can't fit at all.
export function planLines(script) {
  const parts = splitSentences(script).flatMap(splitLong)
  let lines = groupScenes(parts)
  let target = Math.max(SCENE_TARGET_MIN_CHARS, Math.ceil(parts.reduce((a, x) => a + spokenUnits(x), 0) / 18))
  const ceiling = LINE_MAX * CJK_CHAR_WEIGHT
  while (lines.length > SCENE_MAX && target < ceiling) {
    target = Math.min(ceiling, Math.ceil(target * 1.25))
    lines = groupScenes(parts, target)
  }
  return lines
}

// A scene a re-plan or shuffle may not reassign (D30).
export const isFixedVisual = (scene) =>
  !!scene?.visual?.pinned || scene?.visual?.kind === "user_clip"
  || scene?.visual?.state === "ready" || scene?.visual?.state === "generating"

// Put unused clips on the open scenes whose line shares words with the clip's
// name, then on the remaining open scenes in order. Local, no AI call.
export function suggestMatches(scenes, assets) {
  const open = scenes.filter((sc) => sc.line && !isFixedVisual(sc))
  const taken = new Set()
  const out = []
  for (const asset of assets) {
    const words = String(asset.title || "").toLowerCase().replace(/\.[a-z0-9]{2,4}$/, "")
      .split(/[^a-z0-9]+/).filter((w) => w.length > 3)
    let best = null
    let bestScore = 0
    for (const sc of open) {
      if (taken.has(sc.uid)) continue
      const score = words.filter((w) => sc.line.toLowerCase().includes(w)).length
      if (score > bestScore) { best = sc; bestScore = score }
    }
    if (!best) best = open.find((sc) => !taken.has(sc.uid)) || null
    if (!best) break
    taken.add(best.uid)
    out.push({ uid: best.uid, asset })
  }
  return out
}
