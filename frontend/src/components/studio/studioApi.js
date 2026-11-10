// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
// Smart Video Studio — the /api/studio client.
//
// Every project call returns the server ENVELOPE, normalised so components
// keep the field names they already use (`project.media`, `project.checks`…).
//
// A stale edit comes back as a StudioApiError with `.code ===
// "revision_conflict"` and the latest project on `.project`.
import http from "../../api/http"

export class StudioApiError extends Error {
  constructor(message, status, detail) {
    super(message)
    this.status = status
    this.detail = detail || null
    this.code = detail?.code || null
    this.project = detail?.project ? normaliseProject(detail.project) : null
  }
}

// What the user reads when the transport failed rather than the server
// answering: axios's own "Network Error" / "timeout of 0ms exceeded" is not a
// sentence anyone should see (audit 2026-09-25).
export const OFFLINE_MESSAGE = "Couldn't reach ViralMint — check the app is still running, then try again."
export const SERVER_FAULT_MESSAGE = "Something went wrong on our side — try again in a moment."

function fail(err) {
  const status = err?.response?.status
  const detail = err?.response?.data?.detail
  const fallback = !status ? OFFLINE_MESSAGE : status >= 500 ? SERVER_FAULT_MESSAGE : (err?.message || "Request failed")
  const message = typeof detail === "string"
    ? detail
    : detail?.message || (Array.isArray(detail) ? "That request wasn't valid." : fallback)
  throw new StudioApiError(message, status, detail && typeof detail === "object" && !Array.isArray(detail) ? detail : null)
}

export function normaliseProject(p) {
  if (!p) return p
  const checks = p.checks || {}
  return { ...p, media: p.media || [], checks: { blockers: checks.blockers || [], warnings: checks.warnings || [] } }
}

const envelope = (res) => normaliseProject(res.data.project)

// ── projects ────────────────────────────────────────────────────────────────

export async function listProjects({ q } = {}) {
  const res = await http.get("/api/studio/projects", { params: q ? { q } : {} }).catch(fail)
  return res.data.items || []
}

export async function createProject({ start = {}, aspect_ratio = "9:16", visual_style = "cinematic", firstDraft = false, plan = false, settings = null } = {}) {
  const res = await http.post("/api/studio/projects", {
    start: {
      mode: "script",
      script: start.script || "",
      title: start.title || null,
      topic: start.topic || null,
      sample_id: start.sample_id || null,
      // The Library source the script was drafted from — the rendered video's
      // lineage (studio_render.source_lineage). Dropped here until 2026-09-18.
      source_id: start.source_id || null,
    },
    aspect_ratio, visual_style, first_draft: firstDraft, plan, settings,
  }).catch(fail)
  return { ...envelope(res), planJobId: res.data.plan_job_id || null }
}

export async function getProject(id) {
  return envelope(await http.get(`/api/studio/projects/${id}`).catch(fail))
}

// The poll's cheap read: `{unchanged: true, revision, status, render_alive}` while
// the stored revision is still `knownRevision`, else the full project.
export async function pollProject(id, knownRevision) {
  const res = await http.get(`/api/studio/projects/${id}`, { params: { known_revision: knownRevision } }).catch(fail)
  return res.data?.unchanged ? res.data : envelope(res)
}

export async function deleteProject(id) {
  await http.delete(`/api/studio/projects/${id}`).catch(fail)
  return { ok: true }
}

export async function applyOps(id, ifRevision, ops) {
  const res = await http.patch(`/api/studio/projects/${id}`, { if_revision: ifRevision, ops }).catch(fail)
  return { project: envelope(res), created: res.data.created || [], changes: res.data.changes || [] }
}

// ── background work (each returns the updated project; a job finishes over WS) ──

function jobResult(res) {
  return {
    jobId: res.data.job_id || null,
    project: res.data.project ? normaliseProject(res.data.project) : null,
  }
}

export async function voiceScenes(id, uids) {
  return jobResult(await http.post(`/api/studio/projects/${id}/voice`, { scene_uids: uids }).catch(fail))
}

// `sceneUids`: re-plan just those scenes ("Better clip", "Re-plan this scene").
export async function planProject(id, { firstDraft = false, sceneUids = null } = {}) {
  const res = await http.post(`/api/studio/projects/${id}/plan`, sceneUids?.length ? { scene_uids: sceneUids } : null,
    { params: { first_draft: firstDraft } }).catch(fail)
  return { jobId: res.data.job_id }
}

// AI suggestions for one scene's line — {lines: [..], query: str|null}.
// Nothing changes on the server; the caller applies a pick with set_line.
export async function assistScene(id, uid, action, instruction = null) {
  const res = await http.post(`/api/studio/projects/${id}/assist`, { uid, action, instruction },
    { timeout: 60000 }).catch(fail)
  return { lines: res.data?.lines || [], query: res.data?.query || null }
}

export async function renderProject(id) {
  const res = await http.post(`/api/studio/projects/${id}/render`, {}).catch(fail)
  return { jobId: res.data.job_id }
}

// ── media ───────────────────────────────────────────────────────────────────

export async function searchCandidates(id, q) {
  const res = await http.get(`/api/studio/projects/${id}/candidates`, { params: { q } }).catch(fail)
  return res.data.candidates || []
}

const previewCache = new Map()

export async function previewCandidate(id, candidate) {
  const key = `${candidate?.provider}:${candidate?.provider_id}`
  if (previewCache.has(key)) return previewCache.get(key)
  const pending = http.post(`/api/studio/projects/${id}/media/preview`, { candidate })
    .then((res) => res.data.url)
    .catch((err) => { previewCache.delete(key); return fail(err) })
  previewCache.set(key, pending)
  return pending
}

export async function uploadMedia(id, file) {
  const form = new FormData()
  form.append("file", file)
  const res = await http.post(`/api/studio/projects/${id}/media/upload`, form).catch(fail)
  return { asset: res.data.asset, project: envelope(res) }
}

export async function deleteMedia(id, assetId) {
  return envelope(await http.delete(`/api/studio/projects/${id}/media/${assetId}`).catch(fail))
}

export const mediaUrl = (projectId, assetId, { poster = false } = {}) =>
  `/api/studio/media/${projectId}/${assetId}${poster ? "?poster=1" : ""}`

export const voiceUrl = (takeKey) => `/api/studio/voice/${takeKey}`

const metaCache = new Map()

export function takeMeta(takeKey) {
  if (!takeKey) return Promise.resolve(null)
  if (!metaCache.has(takeKey)) {
    metaCache.set(takeKey, http.get(`/api/studio/voice/${takeKey}/meta`, { timeout: 180000 })    // the first take on a new install waits for the Whisper download
      .then((res) => res.data)
      .catch(() => { metaCache.delete(takeKey); return null }))
  }
  return metaCache.get(takeKey)
}

// ── jobs + script drafting (existing endpoints) ─────────────────────────────

export async function getJob(jobId) {
  return (await http.get(`/api/jobs/${jobId}`).catch(fail)).data
}

export async function getVideo(videoId) {
  return (await http.get(`/api/videos/${videoId}`).catch(fail)).data
}

// ── remembered choices (idea: the next video starts where the last one ended) ──
const LAST_START = "vm_studio_last_start"        // { aspect, style }
const LAST_SETTINGS = "vm_studio_last_settings"  // { voice, captions }

export function readLast(key) {
  try { return JSON.parse(localStorage.getItem(key === "start" ? LAST_START : LAST_SETTINGS) || "null") } catch { return null }
}

// The ONE merge for remembered / carried-over settings. Merged into what's
// there, never replacing it: a caller that only knows some fields (an older
// render has no voice provider on record; a link that says captions=on but no
// style) must not wipe the rest. Undefined fields are skipped. Groups merge
// per field (captions, music) — except `voice`, which replaces whole: a voice
// object never mixes two voices' keys (Kore's id with Andrew's provider).
const WHOLE = new Set(["voice"])
const isObj = (x) => !!x && typeof x === "object" && !Array.isArray(x)
export function mergeRemembered(prev, value) {
  const out = { ...(isObj(prev) ? prev : {}) }
  for (const [k, v] of Object.entries(value || {})) {
    if (v === undefined) continue
    out[k] = isObj(v) && isObj(out[k]) && !WHOLE.has(k) ? mergeRemembered(out[k], v) : v
  }
  return out
}

/** Drop a remembered caption style the server no longer knows (a custom
 *  preset the user deleted). Home replays the last video's settings on every
 *  new project, so a stale id there refused EVERY new project with 400 and
 *  nothing in the UI could clear it (audit 2026-09-19). */
export function forgetLastCaptionStyle() {
  try {
    const last = readLast("settings")
    if (isObj(last) && isObj(last.captions) && "style" in last.captions) {
      const { style: _gone, ...captions } = last.captions  // eslint-disable-line no-unused-vars
      localStorage.setItem(LAST_SETTINGS, JSON.stringify({ ...last, captions }))
    }
  } catch { /* private mode */ }
}

export function rememberLast(key, value) {
  try {
    localStorage.setItem(key === "start" ? LAST_START : LAST_SETTINGS, JSON.stringify(mergeRemembered(readLast(key), value)))
  } catch { /* private mode */ }
  // The same choice in the user's saved Settings, which chat, MCP and Quick
  // Video read — the Studio's voice used to stay in this browser only, so the
  // next video from chat spoke with a different voice (2026-10-13 audit).
  if (key === "settings") saveDefaults(value)
}

function saveDefaults(value) {
  const body = {}
  const v = value?.voice
  if (v?.provider) {
    body.tts_provider = v.provider
    if (v.voice) body.preferred_tts_voice = v.voice
  }
  const c = value?.captions
  if (c && typeof c.enabled === "boolean") body.caption_enabled = c.enabled
  if (c?.style) body.caption_style = c.style
  if (Object.keys(body).length) http.post("/api/settings", body).catch(() => {})
}

// A synchronous AI write can outlive axios' default timeout.
const SCRIPT_TIMEOUT_MS = 120000

export async function draftScriptFromSource(sourceId, aspectRatio = "9:16") {
  const res = await http.post(`/api/downloaded/${sourceId}/generate-script`, {
    aspect_ratio: aspectRatio,
  }, { timeout: SCRIPT_TIMEOUT_MS }).catch(fail)
  const script = res.data?.script || ""
  if (!script.trim()) throw new StudioApiError("Couldn't draft a script from that video.", 502)
  return script
}

// POST /api/downloaded/generate-script-from-topic — the user's own AI key
// writes a narration script for a topic. Answers {script}.
export async function draftScript(topic, aspectRatio = "9:16") {
  const res = await http.post("/api/downloaded/generate-script-from-topic", {
    topic, aspect_ratio: aspectRatio,
  }, { timeout: SCRIPT_TIMEOUT_MS }).catch(fail)
  const script = res.data?.script || ""
  if (!script.trim()) throw new StudioApiError("The AI didn't write a script — try a different topic.", 502)
  return script
}
