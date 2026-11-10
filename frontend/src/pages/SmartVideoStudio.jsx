// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useLocation, useNavigate, useParams } from "react-router-dom"
import { Box, Button, CircularProgress, IconButton, LinearProgress, Stack, Typography, alpha } from "@mui/material"
import PlayArrowRoundedIcon from "@mui/icons-material/PlayArrowRounded"
import CloseIcon from "@mui/icons-material/Close"
import TopBar from "../components/studio/TopBar"
import MediaBin, { MEDIA_BIN_WIDTH } from "../components/studio/MediaBin"
import { editorColumns, playerMinWidth } from "../components/studio/editorLayout"
import useResizableWidth from "../hooks/useResizableWidth"
import SequencePlayer from "../components/studio/SequencePlayer"
import Inspector, { INSPECTOR_WIDTH } from "../components/studio/Inspector"
import ScenePanel from "../components/studio/ScenePanel"
import StudioTimeline from "../components/studio/StudioTimeline"
import RenderDialog from "../components/studio/RenderDialog"
import { StudioRenderingContext } from "../components/studio/StudioRenderingContext"
import RenderDoneCard from "../components/studio/RenderDoneCard"
import ScriptView from "../components/studio/ScriptView"
import useStudioProject, { renderInProgress } from "../components/studio/useStudioProject"
import useJobWatch from "../components/studio/useJobWatch"
import * as studioApi from "../components/studio/studioApi"
import {
  autoRevoices, critique, isFixedVisual, sceneAt, suggestMatches, voiceProvider, withTimeline,
} from "../components/studio/studioModel"
import { createPlayhead, usePlayhead } from "../components/studio/playheadStore"
import { projectSettingsFrom } from "../utils/studioHandoff"
import useAppStore from "../store/appStore"
import useDocumentTitle from "../hooks/useDocumentTitle"


// Smart Video Studio — the editor.
//
//   TopBar
//   MediaBin | SequencePlayer [ScenePanel while a scene is selected] | Inspector (Project)
//   Timeline
//
// Backed by /api/studio (components/studio/studioApi.js): edits are saved as
// you go; voicing, planning and the render run as background jobs that write
// back into the project.

// Keyed on the project: "Make another like this" navigates /stock/p/A →
// /stock/p/B, and React Router would otherwise keep this page mounted with A's
// completion card, arrival/landing refs and watched job.
export default function SmartVideoStudio() {
  const { projectId } = useParams()
  return <StudioEditor key={projectId} projectId={projectId} />
}

function StudioEditor({ projectId }) {
  const navigate = useNavigate()
  const location = useLocation()
  useDocumentTitle("Smart Video Studio")
  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const onError = useCallback((m) => showSnackbar(m, "error"), [showSnackbar])
  const studio = useStudioProject(projectId, { onError })
  const { project, apply, undo, redo, voice, plan, render, refresh, adopt } = studio

  // The server's derived timing for this document (one estimate, D10).
  const timeline = useMemo(() => withTimeline(project?.doc, project?.derived), [project?.doc, project?.derived])
  // The critic runs once; the Project panel shows all notes, the Scene panel its own.
  const findings = useMemo(() => critique(timeline), [timeline])
  const projectRef = useRef(project); projectRef.current = project
  const scenes = timeline.scenes
  const [selectedUid, setSelectedUid] = useState(null)
  const selected = scenes.find((s) => s.uid === selectedUid) || null
  // The playhead lives in a store, not in this page's state: playback moves
  // it ~30×/s and only the parts that draw it subscribe (playheadStore.js).
  // Before, every tick re-rendered the whole editor (audit 2026-09-25).
  const [clock] = useState(() => createPlayhead(0))
  const setPlayhead = clock.set
  const [playing, setPlaying] = useState(false)
  const [preview, setPreview] = useState(null)
  const [voiceMuted, setVoiceMuted] = useState(false)
  const [musicMuted, setMusicMuted] = useState(false)
  const [loopScene, setLoopScene] = useState(false)
  const [renderOpen, setRenderOpen] = useState(false)
  const [scriptOpen, setScriptOpen] = useState(false)
  const [rendering, setRendering] = useState(null)
  const [binRequest, setBinRequest] = useState(null)
  const uploadRef = useRef(null)
  const mine = project?.media || []

  useEffect(() => {
    if (selectedUid && project && !project.doc.scenes.some((s) => s.uid === selectedUid)) setSelectedUid(null)
  }, [project, selectedUid])
  // The editor lands with NO scene selected (owner, 2026-09-16): the scene
  // panel opens beside the player when a scene is picked in the timeline.

  // ── The job behind the page (plan / first draft / re-plan) ─────────────
  // The planner and the voice fill the project in over a minute; the
  // user sees that happening as a banner with a progress bar instead of a
  // silent, estimated timeline. Voicing progress comes from the document
  // itself (takes land one by one); the planning phase from the job row.
  const [watchJob, setWatchJob] = useState(null)      // { id, label, kind }
  const jobInfo = useJobWatch(watchJob?.id, {
    onDone: ({ status, error }) => {
      if (status === "failed") showSnackbar(error || "Planning failed — try Re-plan.", "error")
      refresh()
      setWatchJob(null)
    },
  })
  const spokenScenes = scenes.filter((s) => s.line)
  const voicingN = spokenScenes.filter((s) => s.voice?.state === "generating").length
  const readyN = spokenScenes.filter((s) => s.voice?.state === "ready").length
  const drafting = watchJob?.kind === "first_draft"
  const banner = voicingN > 0
    ? { text: `Voicing scenes · ${readyN} of ${spokenScenes.length} ready`, pct: (readyN / Math.max(1, spokenScenes.length)) * 100 }
    : watchJob ? { text: `${watchJob.label}${jobInfo?.step ? ` · ${jobInfo.step}` : "…"}`, pct: jobInfo?.pct ?? 0 } : null
  // Nothing to hear yet: while a draft is being voiced and no take has landed,
  // Play would run a silent estimate — wait for scene 1 instead.
  const canPlay = !((drafting || voicingN > 0) && readyN === 0)
  // When a first draft finishes, say so and point at the two things that
  // matter (watch, then render) — it used to just stop showing the banner
  // in an editor of ~40 controls (2026-10-13 audit).
  const sawDraft = useRef(false)
  const [draftReady, setDraftReady] = useState(false)
  useEffect(() => {
    if (drafting) sawDraft.current = true
    else if (sawDraft.current && !banner) { sawDraft.current = false; setDraftReady(true) }
  }, [drafting, banner])
  const canPlayRef = useRef(canPlay); canPlayRef.current = canPlay
  useEffect(() => { if (clock.get() > timeline.total) setPlayhead(timeline.total) }, [timeline.total, clock, setPlayhead])

  const autoVoiceRef = useRef(false)
  // Only the local Edge voice re-records by itself after an edit; an OpenAI
  // voice (the user's own key) records when asked (Voice this scene / all).
  autoVoiceRef.current = !!project && autoRevoices(voiceProvider(project.doc.settings))

  // ── Actions ─────────────────────────────────────────────────────────────
  const voiceScenes = useCallback(async (uids) => {
    const list = (uids || []).filter(Boolean)
    if (!list.length) return null
    return voice(list)
  }, [voice])

  const unvoiced = (doc) => doc.scenes
    .filter((s) => s.line && s.voice.state !== "ready" && s.voice.state !== "generating").map((s) => s.uid)

  const applyOps = useCallback(async (ops, msg, opts) => {
    const res = await apply(ops)
    if (res && msg) showSnackbar(msg, "success")
    if (res && opts?.revoice) voiceScenes(unvoiced(res.project.doc))
    // The next video starts with this one's voice and caption style.
    if (res && opts?.remember !== false && ops.some((o) => o.op === "set_setting" && (o.path === "voice" || String(o.path).startsWith("captions")))) {
      const st = res.project.doc.settings
      // The opening title's TEXT belongs to this video; its on/off does carry.
      const { hook_text: _own, ...captions } = st.captions || {}  // eslint-disable-line no-unused-vars
      studioApi.rememberLast("settings", { voice: st.voice, captions })
    }
    return res
  }, [apply, voiceScenes, showSnackbar])

  // A critic note's fix button (studioModel.critique): all plain edits.
  const fixFinding = useCallback((f) => {
    if (!f?.fix) return
    if (f.fix.kind === "split") splitScene(f.uid)
    else if (f.fix.kind === "stock") applyOps([{ op: "clear_visual", uid: f.uid }], "Back to stock footage")
    else if (f.fix.kind === "search") { selectScene(f.uid); searchStock(f.fix.query) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applyOps])

  const commitLine = useCallback(async (uid, line) => {
    const res = await apply([{ op: "set_line", uid, line }])
    if (res && autoVoiceRef.current) voiceScenes([uid])
  }, [apply, voiceScenes])

  const splitScene = useCallback(async (uid) => {
    const res = await apply([{ op: "split_scene", uid }])
    if (res && autoVoiceRef.current) voiceScenes([uid, ...(res.created || [])])
  }, [apply, voiceScenes])

  const mergeScene = useCallback(async (uid) => {
    const list = timelineRef.current.scenes
    const next = list[list.findIndex((s) => s.uid === uid) + 1]
    if (!next) return
    const res = await apply([{ op: "merge_scenes", uid, next_uid: next.uid }])
    if (res && autoVoiceRef.current) voiceScenes([uid])
  }, [apply, voiceScenes])

  const insertAfter = useCallback(async (uid) => {
    // Starts with no line: it isn't voiced or rendered until written.
    const res = await apply([{ op: "insert_scene", after_uid: uid, line: "" }])
    if (res?.created?.[0]) setSelectedUid(res.created[0])
  }, [apply])

  const appendLine = useCallback(async (line) => {
    const last = timelineRef.current.scenes.at(-1)
    const res = await apply([{ op: "insert_scene", after_uid: last ? last.uid : null, line }])
    if (res && autoVoiceRef.current) voiceScenes(res.created || [])
  }, [apply, voiceScenes])

  // Cycle a stock scene through the planner's alternates (N): the footage
  // changes without leaving the timeline. Chosen = pinned (D29), like Use.
  const nextAlternate = useCallback((uid) => {
    const sc = timelineRef.current.scenes.find((s) => s.uid === uid)
    const v = sc?.visual
    if (!v || v.kind !== "stock" || !(v.alternates?.length >= 2)) { showSnackbar("This scene has no other footage to cycle — search or re-plan", "info"); return }
    if (v.state === "generating") return
    const cur = v.alternates.findIndex((a) => a.provider === v.asset?.provider && a.provider_id === v.asset?.provider_id)
    const next = v.alternates[(cur + 1) % v.alternates.length]
    applyOps([{ op: "bind_visual", uid, visual: { kind: "stock", candidate: next } }],
      `Scene ${sc.index + 1} · footage ${((cur + 1) % v.alternates.length) + 1} of ${v.alternates.length}`)
  }, [applyOps, showSnackbar])

  // Fresh footage for every scene the user hasn't chosen: each open stock
  // scene moves to its next alternate, unpinned so a re-plan can still move it.
  const shuffleFootage = useCallback(() => {
    const ops = []
    for (const sc of timelineRef.current.scenes) {
      const v = sc.visual
      if (v?.kind !== "stock" || isFixedVisual(sc) || !(v.alternates?.length >= 2)) continue
      const cur = v.alternates.findIndex((a) => a.provider === v.asset?.provider && a.provider_id === v.asset?.provider_id)
      ops.push({ op: "bind_visual", uid: sc.uid, visual: { kind: "stock", candidate: v.alternates[(cur + 1) % v.alternates.length], pinned: false } })
    }
    if (!ops.length) { showSnackbar("Nothing to shuffle — every scene is pinned or has no alternates", "info"); return }
    applyOps(ops, `Fresh footage on ${ops.length} scene${ops.length === 1 ? "" : "s"}`)
  }, [applyOps, showSnackbar])

  const removeScene = useCallback(async (uid) => {
    const list = timelineRef.current.scenes
    const i = list.findIndex((s) => s.uid === uid)
    const neighbour = list[i + 1]?.uid || list[i - 1]?.uid || null
    const res = await apply([{ op: "remove_scene", uid }])
    if (res) setSelectedUid(neighbour)
  }, [apply])

  const addSceneAtEnd = useCallback(() => {
    const last = timelineRef.current.scenes.at(-1)
    insertAfter(last ? last.uid : null)
  }, [insertAfter])

  const reorder = useCallback((order) => apply([{ op: "reorder_scenes", order }]), [apply])

  const bindable = useCallback((sc) => {
    if (!sc) { showSnackbar("Select a scene in the timeline first", "info"); return false }
    return true
  }, [showSnackbar])

  const useStock = useCallback((candidate) => {
    const sel = selectedRef.current
    if (!bindable(sel)) return false
    applyOps([{ op: "bind_visual", uid: sel.uid, visual: { kind: "stock", candidate } }],
      `Scene ${sel.index + 1} now uses that clip`)
    return true
  }, [applyOps, bindable])

  const clipBindOp = (uid, asset) => ({ op: "bind_visual", uid, visual: { kind: "user_clip", asset_id: asset.asset_id } })

  const useMine = useCallback((asset) => {
    const sel = selectedRef.current
    if (!bindable(sel)) return false
    applyOps([clipBindOp(sel.uid, asset)], `Scene ${sel.index + 1} now uses ${asset.title}`)
    return true
  }, [applyOps, bindable])

  // ── Your clips ──────────────────────────────────────────────────────────
  const matchClips = useCallback(async (assets) => {
    const matches = suggestMatches(timelineRef.current.scenes, assets)
    if (!matches.length) { showSnackbar("No open scenes left. Unpin a scene or add one first.", "info"); return }
    await applyOps(matches.map((m) => clipBindOp(m.uid, m.asset)))
    const left = assets.length - matches.length
    showSnackbar(`Placed ${matches.length} clip${matches.length === 1 ? "" : "s"}${left ? ` · ${left} didn't fit an open scene` : ""}`, "success")
  }, [applyOps, showSnackbar])

  const removeClip = useCallback(async (asset) => {
    const used = timelineRef.current.scenes.filter((sc) => sc.visual?.kind === "user_clip" && sc.visual.asset?.asset_id === asset.asset_id)
    if (used.length && !(await apply(used.map((sc) => ({ op: "clear_visual", uid: sc.uid }))))) return
    try {
      adopt(await studioApi.deleteMedia(projectId, asset.asset_id))
      showSnackbar(`${asset.title} removed${used.length ? ` · ${used.length} scene${used.length === 1 ? "" : "s"} back to stock` : ""}`, "success")
    } catch (e) {
      showSnackbar(e.message, "error")
    }
  }, [apply, adopt, projectId, showSnackbar])

  const openUpload = useCallback(() => uploadRef.current?.click(), [])
  const uploadFiles = async (files) => {
    for (const file of Array.from(files || [])) {
      try {
        showSnackbar(`Uploading ${file.name}…`, "info")
        const res = await studioApi.uploadMedia(projectId, file)
        adopt(res.project)
        setBinRequest({ tab: "mine", n: Date.now() })
        showSnackbar(`${file.name} added to this project`, "success")
      } catch (e) {
        showSnackbar(`${file.name}: ${e.message}`, "error")
      }
    }
  }

  const replan = useCallback(async () => {
    const res = await plan()
    if (res?.jobId) setWatchJob({ id: res.jobId, label: "Re-planning visuals", kind: "plan" })
  }, [plan])
  // One scene only — "Better clip" and "Re-plan this scene" (lands only if
  // the scene is still open, like any plan).
  const replanScene = useCallback(async (uid) => {
    const n = scenes.findIndex((s) => s.uid === uid) + 1
    const res = await plan({ sceneUids: [uid] })
    if (res?.jobId) setWatchJob({ id: res.jobId, label: n ? `Finding a better clip for scene ${n}` : "Finding a better clip", kind: "plan" })
  }, [plan, scenes])

  const searchStock = useCallback((q) => setBinRequest({ tab: "stock", query: q, n: Date.now() }), [])
  // One home per job: your clips live in the media bin's Media tab. The
  // Project and Scene panels open it instead of repeating it.
  const showMedia = useCallback(() => setBinRequest({ tab: "mine", n: Date.now() }), [])
  const runSearch = useCallback((q) => studioApi.searchCandidates(projectId, q), [projectId])

  const openPreview = useCallback((p) => {
    if (!p || !(p.url || p.candidate)) return
    setPlaying(false)
    setPreview(p)
  }, [])
  const usePreview = useCallback(() => {
    const p = preview
    if (!p?.useAs) return
    const used = p.useAs.kind === "stock" ? useStock(p.useAs.candidate) : p.useAs.asset ? useMine(p.useAs.asset) : false
    // Keep the preview open when nothing was bound (no scene selected yet).
    if (used) setPreview(null)
  }, [preview, useStock, useMine])

  const closeScene = useCallback(() => setSelectedUid(null), [])
  // Layout with a scene open (coordinator, 2026-09-16): the panel sits BESIDE
  // the player and never covers it. Space comes from flexbox — the centre's
  // min-width is player-min + panel-min, so the Project column (flex-shrink,
  // capped at 300px while a scene is open) gives way first, the media bin a
  // little. Only a window too small for all of that shows the scene panel as
  // a drawer over the Project column.
  // Measured on the editor row (not the window): the app sidebar's width
  // varies, and a fixed breakpoint let the row overflow on mid-size windows.
  const rowRef = useRef(null)
  const [rowW, setRowW] = useState(0)
  useEffect(() => {
    const el = rowRef.current
    if (!el || typeof ResizeObserver === "undefined") return undefined
    const ro = new ResizeObserver(([e]) => setRowW(Math.round(e.contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [project?.id])
  // Both side columns' drag handles live here so ONE budget (editorLayout.js)
  // sizes the whole row — the saved widths are preferences it clamps.
  const binResize = useResizableWidth(MEDIA_BIN_WIDTH)
  const projectResize = useResizableWidth(INSPECTOR_WIDTH)
  const columns = editorColumns({
    rowW, binSaved: binResize.width, projectSaved: projectResize.width,
    aspect: project?.doc.aspect_ratio, sceneSelected: !!selectedUid,
  })
  const { sceneAsDrawer, projectAsDrawer } = columns
  // On a phone the media bin is a drawer too (editorLayout.js). It covers the player, so a preview closes it,
  // and while it's open it comes first: the scene drawer waits, or a scene tapped in the timeline hides the bin.
  const binAsDrawer = !!columns.binAsDrawer
  const [binOpen, setBinOpen] = useState(false)
  const binShown = binAsDrawer && binOpen
  useEffect(() => { if (preview) setBinOpen(false) }, [preview])
  // The folded Project column's drawer (a row too narrow for three columns).
  // A selected scene opens in it on its own; this is the Project view.
  const [projectOpen, setProjectOpen] = useState(false)
  const projectOpenRef = useRef(false); projectOpenRef.current = projectAsDrawer && projectOpen
  const onProjectOpen = useCallback((next) => {
    // The rail button while a scene fills the drawer: show the Project view.
    if (next === false && selectedRef.current && !projectOpenRef.current) { setSelectedUid(null); setProjectOpen(true); return }
    if (next === false) setSelectedUid(null)
    setProjectOpen(next)
  }, [])

  // ONE current scene (audit 2026-09-25): with a scene selected, whatever
  // moves the player to another scene — ←/→, the transport's skip buttons, a
  // click on the ruler or scrub bar — moves the selection with it. Before,
  // ←/→ moved only the picture: the Scene panel and the timeline highlight
  // stayed behind, and S/M/N/⌫ then edited (⌫
  // deleted) a scene the player was no longer showing.
  const followPlayer = useCallback((t) => {
    const sel = selectedRef.current
    if (!sel) return
    const at = sceneAt(timelineRef.current.scenes, t)
    if (at && at.uid !== sel.uid) setSelectedUid(at.uid)
  }, [])

  const waitingPreviewRef = useRef(false)
  waitingPreviewRef.current = !selectedUid && !!preview
  const selectScene = useCallback((uid) => {
    const sc = timelineRef.current.scenes.find((s) => s.uid === uid)
    // A preview opened with NO scene selected is waiting for one ("Select a
    // scene in the timeline to use this") — picking it keeps the preview so
    // Use is one click away. Otherwise selecting closes it and seeks, as before.
    if (!waitingPreviewRef.current) {
      setPreview(null)
      if (sc) setPlayhead(sc.start + 0.001)
    }
    setSelectedUid(uid)
  }, [])

  // The Scene panel's play/pause: pauses while THIS scene is playing; plays it
  // otherwise — resuming where it paused inside the scene, else from its start.
  const playScene = useCallback((uid) => {
    const sc = timelineRef.current.scenes.find((s) => s.uid === uid)
    if (!sc) return
    const here = sceneAt(timelineRef.current.scenes, clock.get())?.uid === uid
    if (playingRef.current && here) { setPlaying(false); return }
    if (!canPlayRef.current) { showSnackbar("The first scene is still being voiced — one moment.", "info"); return }
    setPreview(null)
    if (!here) setPlayhead(sc.start + 0.001)
    setPlaying(true)
  }, [showSnackbar, clock, setPlayhead])

  const togglePlay = useCallback(() => {
    if (!canPlayRef.current) { showSnackbar("The first scene is still being voiced — one moment.", "info"); return }
    setPreview(null)
    // Pausing with a scene selected lands the selection on the scene the
    // player stopped on — the one the S/M/N/⌫ keys are about to act on.
    if (playingRef.current) followPlayer(clock.get())
    setPlaying((p) => {
      if (!p && clock.get() >= timelineRef.current.total - 0.05) setPlayhead(0)
      return !p
    })
  }, [showSnackbar, clock, setPlayhead, followPlayer])

  const stepScene = useCallback((dir) => {
    const list = timelineRef.current.scenes
    const now = clock.get()
    const cur = sceneAt(list, now)
    if (!cur) return
    let to = null
    if (dir < 0) {
      to = now - cur.start > 1 ? cur : list[cur.index - 1] || cur
    } else {
      to = list[cur.index + 1] || null
    }
    if (!to) return
    setPlayhead(to.start + 0.001)
    followPlayer(to.start + 0.001)
  }, [clock, setPlayhead, followPlayer])

  const seek = useCallback((t) => { setPreview(null); setPlayhead(t); followPlayer(t) }, [setPlayhead, followPlayer])

  const voiceAll = useCallback(() => voiceScenes(unvoiced(timelineRef.current)), [voiceScenes])
  const rename = useCallback((title) => apply([{ op: "set_title", title }], { record: false }), [apply])
  const changeAspect = useCallback((ratio) => apply([{ op: "set_aspect", ratio }]), [apply])
  const openRender = useCallback(() => setRenderOpen(true), [])
  const openScript = useCallback(() => setScriptOpen(true), [])
  const goBack = useCallback(() => navigate("/stock"), [navigate])
  const closePreview = useCallback(() => setPreview(null), [])
  // One pause for every other player in the editor: the scene panel's
  // "Play take" and the Project panel's music preview both used to start on
  // top of a running sequence, so you heard two takes and the music bed.
  const pausePlayback = useCallback(() => setPlaying(false), [])
  const toggleVoice = useCallback(() => setVoiceMuted((v) => !v), [])
  const toggleMusic = useCallback(() => setMusicMuted((v) => !v), [])
  const toggleLoop = useCallback(() => setLoopScene((v) => !v), [])
  const prevScene = useCallback(() => stepScene(-1), [stepScene])
  const nextScene = useCallback(() => stepScene(1), [stepScene])

  const mountedRef = useRef(true)
  useEffect(() => () => { mountedRef.current = false }, [])
  const [completed, setCompleted] = useState(null)      // the finished GeneratedVideo
  // The card can be closed to keep editing; this keeps the way back to it
  // (it used to be gone until the next render — 2026-10-13 audit).
  const [lastVideo, setLastVideo] = useState(null)
  useEffect(() => { if (completed) setLastVideo(completed) }, [completed])
  // A project rendered in an earlier visit: its video is one click away too.
  const lastVideoId = project?.last_video_id || project?.lastVideoId || null
  useEffect(() => {
    if (!lastVideoId || lastVideo?.id === lastVideoId) return undefined
    let live = true
    studioApi.getVideo(lastVideoId).then((v) => { if (live && v) setLastVideo(v) }).catch(() => {})
    return () => { live = false }
  }, [lastVideoId])  // eslint-disable-line react-hooks/exhaustive-deps
  const [making, setMaking] = useState(false)
  // "Make another like this": the NEXT video — this one's settings, a new
  // topic (the Library's behaviour). It used to create a
  // project from the SAME script, titled "(again)": a near-duplicate is not
  // a second video (2026-09-29 conversion audit).
  const makeAnother = useCallback(() => {
    const p = projectRef.current
    if (!p) return
    setMaking(true)
    const { hook_text: _own, ...captions } = p.doc.settings.captions || {}  // eslint-disable-line no-unused-vars
    const settings = { ...p.doc.settings, captions }
    studioApi.rememberLast("start", { aspect: p.doc.aspect_ratio, style: p.doc.settings.visual_style })
    studioApi.rememberLast("settings", projectSettingsFrom(settings))
    navigate("/stock", { state: { start: "topic" } })
  }, [navigate])
  const renderingRef = useRef(false)
  const [renderJobId, setRenderJobId] = useState(null)
  const finishRender = () => {
    renderingRef.current = false
    setRenderJobId(null)
    if (mountedRef.current) setRendering(null)
    refresh()
  }
  const renderJob = useJobWatch(renderJobId, {
    onDone: async ({ status, output, error }) => {
      try {
        if (status === "success") {
          try {
            if (output.generated_video_id) setCompleted(await studioApi.getVideo(output.generated_video_id))
            else showSnackbar("Your video is ready — it's in your Library.", "success")
          } catch { showSnackbar("Your video is ready — it's in your Library.", "success") }
        } else if (status === "failed") {
          showSnackbar(error || "The render failed.", "error")
        } else {
          showSnackbar("Render cancelled.", "info")
        }
      } finally {
        finishRender()
      }
    },
  })
  useEffect(() => {
    if (renderJob && renderJobId) setRendering({ pct: Math.round(renderJob.pct || 0), step: renderJob.step || "Rendering" })
  }, [renderJob?.pct, renderJob?.step, renderJobId])  // eslint-disable-line react-hooks/exhaustive-deps
  // Mounted while a render of this project is already running (a reload, or
  // back from another page): re-attach to it. The job id used to live only in
  // this component's state, so a remount showed "Rendering 0%" for the whole
  // render and never the finished-video card.
  useEffect(() => {
    const running = project?.render_job_id
    if (running && !renderJobId && !renderingRef.current) {
      renderingRef.current = true
      setRenderJobId(running)
    }
  }, [project?.render_job_id])  // eslint-disable-line react-hooks/exhaustive-deps
  const confirmRender = async () => {
    if (renderingRef.current) return
    renderingRef.current = true
    setRenderOpen(false)
    setPlaying(false)
    setRendering({ pct: 0, step: "Starting" })
    let started = false
    try {
      const res = await render()
      if (!res) return
      if (res.blocked) { showSnackbar(res.blocked[0], "warning"); return }
      // Follow the render job until it finishes (useJobWatch).
      started = true
      setRenderJobId(res.jobId)
    } catch (e) {
      showSnackbar(e.message || "Lost track of the render — check the Activity panel.", "error")
    } finally {
      if (!started) finishRender()
    }
  }

  // Refs the keyboard handler and callbacks read.
  const timelineRef = useRef(timeline); timelineRef.current = timeline
  const playingRef = useRef(playing); playingRef.current = playing
  // True only while the playhead is inside the selected scene — changes at
  // scene boundaries, not on every tick, so the Scene panel isn't re-rendered 60×/s.
  const scenePlaying = usePlayhead(clock, (t) => playing && !!selectedUid && sceneAt(timeline?.scenes || [], t)?.uid === selectedUid)
  const selectedRef = useRef(selected); selectedRef.current = selected
  const previewRef = useRef(preview); previewRef.current = preview
  const completedRef = useRef(completed); completedRef.current = completed
  const actions = useRef({})
  const toastUndo = useCallback((msg) => showSnackbar(msg, "info", { label: "Undo", onClick: () => actions.current.undo() }), [showSnackbar])
  actions.current = { togglePlay, stepScene, removeScene, splitScene, mergeScene, undo, redo, nextAlternate, toastUndo }

  useEffect(() => {
    const onKey = (e) => {
      const el = e.target
      const tag = el?.tagName
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el?.isContentEditable) return
      // Keys belong to an open menu or dialog (MUI keeps focus inside it). A
      // popover that is still animating closed doesn't hold focus, so it no
      // longer swallows the next key.
      if (el?.closest?.(".MuiModal-root")) return
      // The finished-video card sits over the editor without being a modal:
      // Space there is its own <video>'s, and S/M/N/Delete must not edit the
      // project hidden behind it. Media controls own their keys everywhere.
      if (completedRef.current || el?.closest?.("video, audio")) return
      const a = actions.current
      const meta = e.metaKey || e.ctrlKey
      if (meta && e.key.toLowerCase() === "z") {
        e.preventDefault()
        if (e.shiftKey) a.redo(); else a.undo()
        return
      }
      if (meta || e.altKey) return
      // Structural keys act once per press, and never while a button has focus
      // (Backspace after clicking "Use" must not delete the scene).
      if (e.repeat && ["Delete", "Backspace", "s", "S", "m", "M", "n", "N"].includes(e.key)) return
      const sel = selectedRef.current?.uid
      // Real buttons AND focusable tiles (role="button" divs: thumbnails,
      // alternates, stock tiles) — Backspace on a focused tile is not "delete
      // the scene".
      const onButton = tag === "BUTTON" || !!el?.closest?.('[role="button"]')
      switch (e.key) {
        case " ":
          // Space on a focused tile activates the tile, not the player.
          if (onButton) return
          e.preventDefault(); a.togglePlay(); break
        case "ArrowLeft": e.preventDefault(); a.stepScene(-1); break
        case "ArrowRight": e.preventDefault(); a.stepScene(1); break
        // A key press that restructures the video says what it did, with
        // Undo — Backspace on a selected block used to remove a scene
        // without a word (2026-10-13 audit).
        case "Delete":
        case "Backspace":
          if (sel && !onButton && timelineRef.current.scenes.length > 1) {
            e.preventDefault()
            const n = timelineRef.current.scenes.findIndex((x) => x.uid === sel) + 1
            a.removeScene(sel).then(() => a.toastUndo(`Scene ${n} deleted`))
          }
          break
        case "s": case "S":
          if (sel) { const n = timelineRef.current.scenes.findIndex((x) => x.uid === sel) + 1; a.splitScene(sel).then(() => a.toastUndo(`Scene ${n} split in two`)) }
          break
        case "m": case "M":
          if (sel) { const n = timelineRef.current.scenes.findIndex((x) => x.uid === sel) + 1; a.mergeScene(sel).then(() => a.toastUndo(`Scenes ${n} and ${n + 1} merged`)) }
          break
        case "n": case "N": if (sel) a.nextAlternate(sel); break
        case "Escape":
          // In fullscreen, Esc belongs to the browser — it exits. Closing the
          // scene panel at the same time would be a second, unasked-for change
          // the user can't see happening behind the fullscreen picture.
          if (document.fullscreenElement) break
          // An open preview closes first; then the scene panel; then the
          // folded Project drawer.
          if (previewRef.current) setPreview(null)
          else if (selectedRef.current) setSelectedUid(null)
          else if (projectOpenRef.current) setProjectOpen(false)
          break
        default:
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  // Arriving from Home: the server is already planning (and, for a first
  // draft, voicing). Say so once.
  const arrivalRef = useRef(false)
  useEffect(() => {
    if (!project || arrivalRef.current || !location.state) return
    arrivalRef.current = true
    const { firstDraft, planned, planJobId } = location.state
    navigate(location.pathname, { replace: true, state: null })
    if (planJobId && (firstDraft || planned)) {
      setWatchJob({ id: planJobId, label: firstDraft ? "Making your first draft" : "Finding footage for each scene", kind: firstDraft ? "first_draft" : "plan" })
    }
  }, [project, location.state, location.pathname, navigate])

  // Opened (or reloaded) while a plan runs: show its progress like an arrival.
  const planJobOnLoad = project?.plan_job_id || null
  useEffect(() => {
    // Functional: the arrival effect above may have just set the same job as a
    // FIRST DRAFT in this commit — overwriting it as a plain plan hid the
    // "draft ready" moment (caught by the harness).
    if (planJobOnLoad) setWatchJob((w) => w || { id: planJobOnLoad, label: "Finding footage for each scene", kind: "plan" })
  }, [planJobOnLoad])  // eslint-disable-line react-hooks/exhaustive-deps

  // Warm every bound stock preview in the background, in order, so the first
  // playthrough doesn't stall at each boundary waiting for a download. The
  // server caches per clip and single-flights, so this is idempotent.
  const warmedRef = useRef(new Set())
  const boundKeys = (project?.doc.scenes || [])
    .map((s) => (s.visual?.kind === "stock" && s.visual.asset?.provider_id ? `${s.visual.asset.provider}:${s.visual.asset.provider_id}` : null))
    .filter(Boolean).join(",")
  useEffect(() => {
    if (!project || !boundKeys) return undefined
    let alive = true
    const run = async () => {
      for (const s of project.doc.scenes) {
        const a = s.visual?.kind === "stock" ? s.visual.asset : null
        const key = a?.provider_id ? `${a.provider}:${a.provider_id}` : null
        if (!key || warmedRef.current.has(key)) continue
        warmedRef.current.add(key)
        try { await studioApi.previewCandidate(project.id, a) } catch { warmedRef.current.delete(key) }
        if (!alive) return
      }
    }
    run()
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.id, boundKeys])

  // The selected scene's panel, built ONCE and placed either beside the player
  // or (a row too narrow for both) in the Project column — same props either way.
  const sceneFindings = useMemo(() => (selectedUid ? findings.filter((f) => f.uid === selectedUid) : []), [findings, selectedUid])
  const scenePanel = useMemo(() => (selected && project ? (
    <ScenePanel
      drawer={sceneAsDrawer} scene={selected} timeline={timeline} project={project} findings={sceneFindings}
      onClose={closeScene}
      onApply={applyOps} onCommitLine={commitLine} onVoice={voiceScenes}
      onPreview={openPreview} onShowMedia={showMedia} onSearchStock={searchStock}
      onSplit={splitScene} onMerge={mergeScene} onInsertAfter={insertAfter} onRemove={removeScene}
      onPlayScene={playScene} scenePlaying={scenePlaying} onPausePlayback={pausePlayback}
      onFix={fixFinding} onNextClip={nextAlternate} onReplanScene={replanScene}
    />
  ) : null), [selected, project, sceneAsDrawer, timeline, sceneFindings, closeScene,
    applyOps, commitLine, voiceScenes, openPreview, showMedia, searchStock, splitScene, mergeScene,
    insertAfter, removeScene, playScene, scenePlaying, pausePlayback, fixFinding, nextAlternate,
    replanScene])

  if (studio.loading && !project) {
    return <Stack alignItems="center" justifyContent="center" sx={{ height: "100%" }}><CircularProgress /></Stack>
  }
  if (!project && studio.loadError) {
    return (
      <Stack alignItems="center" justifyContent="center" spacing={1.5} sx={{ height: "100%", px: 3, textAlign: "center" }}>
        <Typography variant="h6" sx={{ fontWeight: 800 }}>Couldn't load this project</Typography>
        <Typography variant="body2" sx={{ color: "text.secondary", maxWidth: 420 }}>{studio.loadError}</Typography>
        <Stack direction="row" spacing={1}>
          <Button variant="contained" onClick={studio.reload}>Try again</Button>
          <Button onClick={() => navigate("/stock")}>All projects</Button>
        </Stack>
      </Stack>
    )
  }
  if (studio.notFound || !project) {
    return (
      <Stack alignItems="center" justifyContent="center" spacing={1.5} sx={{ height: "100%" }}>
        <Typography variant="h6" sx={{ fontWeight: 800 }}>This project doesn't exist</Typography>
        <Button variant="contained" onClick={() => navigate("/stock")}>All projects</Button>
      </Stack>
    )
  }

  return (
    <StudioRenderingContext.Provider value={!!(rendering || renderInProgress(project))}>
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column", overflow: "hidden", bgcolor: "background.default" }}>
      <TopBar
        project={project} timeline={timeline} saving={studio.saving} saveError={studio.saveError}
        canUndo={studio.canUndo} canRedo={studio.canRedo} onUndo={undo} onRedo={redo}
        onRename={rename}
        onAspect={changeAspect}
        onRender={openRender} rendering={rendering || (renderInProgress(project) ? { pct: 0, step: "Rendering" } : null)}
        onOpenLast={!completed && lastVideo ? () => setCompleted(lastVideo) : null}
        onRetrySave={studio.retrySave}
        onScript={openScript}
        onBack={goBack}
      />
      {banner && (
        <Box data-testid="draft-banner" role="status" sx={(t) => ({ px: 2, py: 0.5, bgcolor: alpha(t.palette.primary.main, t.palette.mode === "dark" ? 0.12 : 0.08), borderTop: 1, borderColor: "divider" })}>
          <Stack direction="row" alignItems="center" spacing={1.5}>
            <CircularProgress size={12} />
            <Typography variant="caption" sx={{ fontWeight: 700, flex: 1 }}>{banner.text}</Typography>
            {!canPlay && <Typography variant="caption" sx={{ color: "text.secondary" }}>Play unlocks with the first voiced scene</Typography>}
          </Stack>
          <LinearProgress variant="determinate" value={Math.min(100, banner.pct)} sx={{ height: 2, mt: 0.5 }} />
        </Box>
      )}

      {/* What the render is doing right now, and that editing is still safe —
          the TopBar alone said "Rendering 40%" (2026-10-13 audit). */}
      {(rendering || renderInProgress(project)) && (
        <Box data-testid="render-status" role="status" sx={(t) => ({ px: 2, py: 0.5, bgcolor: alpha(t.palette.secondary.main, t.palette.mode === "dark" ? 0.12 : 0.07), borderTop: 1, borderColor: "divider" })}>
          <Stack direction="row" alignItems="center" spacing={1.5}>
            <CircularProgress size={12} />
            <Typography variant="caption" sx={{ fontWeight: 700 }}>
              Rendering{rendering?.step && rendering.step !== "Rendering" ? ` · ${rendering.step}` : ""}{rendering?.pct ? ` · ${rendering.pct}%` : ""}
            </Typography>
            <Typography variant="caption" sx={{ color: "text.secondary" }}>
              Usually a minute or two. Keep editing — changes go into your next render.
            </Typography>
          </Stack>
        </Box>
      )}

      {draftReady && !banner && (
        <Box data-testid="draft-ready" role="status" sx={(t) => ({ px: 2, py: 0.75, bgcolor: alpha(t.palette.success.main, t.palette.mode === "dark" ? 0.14 : 0.09), borderTop: 1, borderColor: "divider" })}>
          <Stack direction="row" alignItems="center" spacing={1.5} useFlexGap flexWrap="wrap">
            <Typography variant="body2" sx={{ fontWeight: 800 }}>Your draft is ready.</Typography>
            <Typography variant="body2" sx={{ color: "text.secondary", flex: 1, minWidth: 200 }}>
              Watch it, click any scene to change its line or footage (the ✨ chips suggest rewrites), then Render when you're happy.
            </Typography>
            <Button size="small" variant="contained" startIcon={<PlayArrowRoundedIcon />}
              onClick={() => { setDraftReady(false); actions.current?.togglePlay?.() }} sx={{ textTransform: "none", fontWeight: 700 }}>
              Watch it
            </Button>
            <IconButton size="small" aria-label="Dismiss" onClick={() => setDraftReady(false)}><CloseIcon fontSize="small" /></IconButton>
          </Stack>
        </Box>
      )}

      <Box ref={rowRef} sx={{ flex: 1, minHeight: 0, display: "flex", overflow: "hidden", borderTop: 1, borderColor: "divider" }}>
        <MediaBin
          selected={selected} scenes={scenes} onPreview={openPreview} onUseStock={useStock} onUseMine={useMine}
          onUpload={openUpload} onSearch={runSearch} mine={mine}
          onMatchClips={matchClips} onRemoveClip={removeClip} request={binRequest}
          width={columns.bin} resize={binResize}
          folded={binAsDrawer} drawerW={columns.drawerW} open={binShown} onOpenChange={setBinOpen}
        />
        <Box sx={{
          flex: "1 1 0", display: "flex", flexDirection: "column", position: "relative",
          minWidth: columns.centerMin,
        }}>
          {completed && (
            <RenderDoneCard
              video={completed} making={making}
              onClose={() => setCompleted(null)}
              onOpenLibrary={() => navigate("/videos")}
              onMakeAnother={makeAnother}
            />
          )}
          <Box sx={{ flex: 1, minHeight: 0, display: "flex" }}>
            <Box sx={{ flex: "1 1 auto", minWidth: Math.min(playerMinWidth(project.doc.aspect_ratio), columns.centerMin), display: "flex", flexDirection: "column" }}>
              <SequencePlayer
                projectId={project.id}
                timeline={timeline} settings={project.doc.settings} aspectRatio={project.doc.aspect_ratio}
                playhead={clock} playing={playing} onPlayingChange={setPlaying} onSeek={seek}
                preview={preview} onClosePreview={closePreview}
                onUsePreview={preview?.useAs ? usePreview : null} selectedIndex={selected?.index ?? null}
                voiceMuted={voiceMuted} onToggleVoice={toggleVoice}
                musicMuted={musicMuted} onToggleMusic={toggleMusic}
                loopScene={loopScene} onToggleLoop={toggleLoop}
                onPrev={prevScene} onNext={nextScene} onTogglePlay={togglePlay} playDisabled={!canPlay}
              />
            </Box>
            {!sceneAsDrawer && scenePanel}
          </Box>
        </Box>
        <Inspector
          project={project} timeline={timeline} findings={findings}
          onApply={applyOps}
          onVoiceAll={voiceAll}
          onShowMedia={showMedia}
          mine={mine}
          onReplan={replan} onFix={fixFinding} onShuffle={shuffleFootage} onPausePlayback={pausePlayback}
          width={columns.project} resize={projectResize}
          drawer={sceneAsDrawer && !binShown ? scenePanel : null}
          folded={projectAsDrawer} drawerW={columns.drawerW} open={projectOpen && !binShown}
          onOpenChange={(next) => { setBinOpen(false); onProjectOpen(next) }}
        />
      </Box>

      <StudioTimeline
        timeline={timeline} settings={project.doc.settings} playhead={clock} playing={playing}
        selectedUid={selectedUid} onSelect={selectScene} onSeek={seek} onReorder={reorder}
        onAddScene={addSceneAtEnd}
      />

      <input
        ref={uploadRef} type="file" hidden multiple aria-label="Upload clips or images"
        accept="video/mp4,video/quicktime,video/webm,.mkv,.m4v,image/*"
        onChange={(e) => { uploadFiles(e.target.files); e.target.value = "" }}
      />

      <ScriptView
        open={scriptOpen} onClose={() => setScriptOpen(false)} timeline={timeline}
        onCommitLine={commitLine} onSplit={splitScene} onMerge={mergeScene} onRemove={removeScene} onAppend={appendLine}
      />

      <RenderDialog
        open={renderOpen} onClose={() => setRenderOpen(false)} onConfirm={confirmRender}
        rendering={rendering} project={project} timeline={timeline}
      />
    </Box>
    </StudioRenderingContext.Provider>
  )
}
