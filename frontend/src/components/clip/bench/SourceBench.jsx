// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  Box, Button, Checkbox, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Divider, FormControlLabel, IconButton, MenuItem,
  Popover, Stack, TextField, Tooltip, Typography,
} from "@mui/material"
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesomeOutlined"
import ContentCutIcon from "@mui/icons-material/ContentCut"
import PlayArrowIcon from "@mui/icons-material/PlayArrow"
import PauseIcon from "@mui/icons-material/Pause"
import KeyboardIcon from "@mui/icons-material/KeyboardOutlined"
import RecordVoiceOverIcon from "@mui/icons-material/RecordVoiceOverOutlined"
import ContentPasteIcon from "@mui/icons-material/ContentPasteGoOutlined"
import MagnetIcon from "@mui/icons-material/PushPinOutlined"
import Timeline from "./Timeline"
import InOutFrames from "./InOutFrames"
import RangeRail from "./RangeRail"
import { MAX_RANGES, MIN_LEN_SEC } from "./useBenchRanges"
import { parseRangeText } from "./parseRanges"
import { formatTime } from "../clipFormat"
import { CAPTION_STYLES } from "../../tools/captionOptions"
import http from "../../../api/http"
import { storageKey } from "../../../utils/storage"
import useAppStore from "../../../store/appStore"

/* ── The cutting bench ─────────────────────────────────────────
   What used to be an empty panel when you selected a source video.

   There are no modes here. The bench holds ONE list of pending cuts, and
   four things put a cut into it — all producing the same editable block:

     drag on the filmstrip · N (or +) at the playhead · paste timestamps ·
     Ask AI

   Ask AI is the one worth explaining. It PROPOSES (POST /suggest-clips,
   which cuts nothing) and its picks arrive as ordinary blocks you can drag,
   retime or delete. That is the point of routing the model through here
   rather than leaving it in the modal: the AI's choice of in/out point used
   to be final and invisible until the clips already existed, and now it is a
   starting position on a timeline.

   It was briefly a "My ranges | AI picks" toggle in the header. Once the
   pending cuts moved to the rail that stopped being true — "My ranges"
   rendered nothing, and the strip stayed draggable either way, because
   dragging was never what the toggle controlled. A control that says
   "you are in AI mode now" over a surface with no modes is a lie about
   the product, so it is a button.

   Nothing on this surface produces a file. The one act that does is the Cut
   button, and it cuts exactly the blocks that are on screen. The hero's
   Auto-cut is the other door: same model, but it cuts immediately, with no
   review and no cap.
*/

const CAPTION_OPTIONS = [{ value: "none", label: "Off" }, ...CAPTION_STYLES]
const EMOJI_OPTIONS = [
  { v: "none", label: "Off" }, { v: "minimal", label: "Minimal" },
  { v: "moderate", label: "Moderate" }, { v: "heavy", label: "Heavy" },
]
const WHISPER_QUALITIES = [
  { v: "fast", label: "Fast (base)" },
  { v: "balanced", label: "Balanced (small)" },
  { v: "accurate", label: "Accurate (medium)" },
  { v: "best", label: "Best (large-v3)" },
]
const PLATFORMS = [
  { v: "", label: "Any platform" },
  { v: "tiktok", label: "TikTok" },
  { v: "youtube_shorts", label: "YouTube Shorts" },
  { v: "reels", label: "Instagram Reels" },
  { v: "linkedin", label: "LinkedIn" },
  { v: "twitter", label: "Twitter / X" },
]
const GENRES = [
  { v: "", label: "Auto-detect genre" },
  { v: "podcast", label: "Podcast" }, { v: "interview", label: "Interview" },
  { v: "qa", label: "Q&A / AMA" }, { v: "vlog", label: "Vlog" },
  { v: "tutorial", label: "Tutorial" }, { v: "gaming", label: "Gaming" },
  { v: "reaction", label: "Reaction" }, { v: "lecture", label: "Lecture" },
]

const SHORTCUTS = [
  ["Space", "Play / pause"],
  ["I / O", "Set the selected clip's IN / OUT to the playhead"],
  ["← →", "Nudge the playhead one frame (⇧ for one second)"],
  ["J / L", "Jump 10s back / forward"],
  ["N", "New range at the playhead"],
  ["⌫", "Delete the selected range"],
  ["Alt-drag", "Ignore speech snapping"],
]

export default function SourceBench({
  source,            // DownloadedVideo row
  existingClips,     // clips already cut from this source (for the ghost lane)
  onOpenDialog,      // escape hatch to Auto-cut (>10 clips, no review)
  ranges: R,         // useBenchRanges(), owned by ClipStudio — the Cut button
                     // that spends this list lives in the page hero
  settings,          // shared per-clip settings (useClipSettings, owned by
  onSettings,        // ClipStudio) — the dialog reads the SAME object
}) {
  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const videoRef = useRef(null)
  const rootRef = useRef(null)

  const sourceId = source?.id || null
  const duration = source?.duration_seconds || 0
  const hasTranscript = !!source?.has_transcript_segments
  const src = sourceId ? `/api/downloaded/${sourceId}/stream` : null

  const [playhead, setPlayhead] = useState(0)
  // The filmstrip sprite, handed up by Timeline. The IN/OUT panes reuse it
  // as their instant proxy, so it has to be the SAME url the browser
  // already has cached — not a second one built to different dimensions.
  const [strip, setStrip] = useState(null)
  // A SECOND, denser sprite used only as the IN/OUT panes' instant proxy.
  // The timeline's own strip is sized to fit the track without squashing
  // its cells, which on a 9-minute source is ~46 seconds per cell — too
  // coarse for the preview to visibly move while you drag. This one is
  // built for time resolution instead. It costs ~5s of ffmpeg the first
  // time, so it is fetched in the BACKGROUND and swapped in when ready:
  // the first drag uses the coarse strip, every drag after is smooth, and
  // nothing ever waits on it.
  const [fineStrip, setFineStrip] = useState(null)
  // Scoped playback: with a range selected, pressing play should preview
  // THAT clip, not the source from wherever the playhead happens to be.
  // Reported as the bench's first bug: "I cannot play just the selected
  // clip — it always plays the entire video."
  const [scoped, setScoped] = useState(true)
  const [playing, setPlaying] = useState(false)
  const [aspect, setAspect] = useState(16 / 9)
  const [segments, setSegments] = useState([])
  const [snapEnabled, setSnapEnabled] = useState(true)
  const [helpAnchor, setHelpAnchor] = useState(null)
  const [aiAnchor, setAiAnchor] = useState(null)
  const [txAnchor, setTxAnchor] = useState(null)
  const [pasteOpen, setPasteOpen] = useState(false)


  // AI request knobs. The count is capped at MAX_RANGES because every
  // proposal is cut back through manual mode; Auto-cut covers more.
  const [ai, setAi] = useState({
    max_clips: 5, min_duration: "", max_duration: "",
    user_query: "", target_platform: "", genre: "",
  })
  // State, not a ref: the job id is a subscription key for the selectors
  // below, and a ref read inside a zustand selector only re-evaluates when
  // something else happens to touch the store.
  // Restored from sessionStorage per source: SourceBench unmounts whenever
  // the user peeks at a finished clip (centerMode flips) — without this, a
  // running Ask AI was silently orphaned and its proposals never adopted. On
  // remount the collector below re-attaches to the job row.
  const suggestKey = storageKey("clipper", `suggest:${source?.id}`)
  const [suggestJobId, setSuggestJobId] = useState(() => {
    try { return sessionStorage.getItem(suggestKey) || null } catch { return null }
  })
  useEffect(() => {
    try {
      if (suggestJobId) sessionStorage.setItem(suggestKey, suggestJobId)
      else sessionStorage.removeItem(suggestKey)
    } catch { /* private mode — degradation is the pre-fix behaviour */ }
  }, [suggestJobId, suggestKey])
  const suggesting = suggestJobId != null

  // The timeupdate guard runs on the media element and must see the CURRENT
  // range without re-subscribing on every drag frame.
  const activeRef = useRef(null)
  activeRef.current = R.active

  // ── Source-scoped data ─────────────────────────────────────
  useEffect(() => {
    setPlayhead(0)
    setAspect(16 / 9)
    setSegments([])
    setSuggestJobId(null)
    setStrip(null)
    setFineStrip(null)
    setPlaying(false)
    if (!sourceId) return
    let cancelled = false
    // limit=5000 (the endpoint's ceiling): the default 1500 stopped snapping
    // past segment 1500 on long podcasts.
    http.get(`/api/downloaded/${sourceId}/segments?limit=5000`)
      .then(({ data }) => { if (!cancelled) setSegments(data?.segments || []) })
      // A source with no transcript is ordinary — the timeline just loses
      // its speech lane and its snapping. Never surface this as an error.
      .catch(() => { if (!cancelled) setSegments([]) })
    return () => { cancelled = true }
  }, [sourceId])

  // Warm the fine proxy once the page is settled. Short sources already
  // get fine-enough cells from the timeline's own strip.
  useEffect(() => {
    if (!sourceId || !duration || duration < 60) return
    let cancelled = false
    const cells = Math.max(24, Math.min(96, Math.round(duration / 5 / 4) * 4))
    const url = `/api/downloaded/${sourceId}/filmstrip?n=${cells}&h=48`
    const t = setTimeout(() => {
      const img = new Image()
      img.onload = () => {
        if (cancelled) return
        // Measure, don't assume: the endpoint caps density by duration, so
        // a short source returns fewer cells than asked and the proxy would
        // offset into the wrong ones.
        const cw = img.naturalHeight * (aspect || 16 / 9)
        const real = cw && img.naturalWidth
          ? Math.max(1, Math.round(img.naturalWidth / cw))
          : cells
        setFineStrip({ url, cells: real })
      }
      // No onerror handling needed: staying on the coarse proxy IS the
      // fallback, and it is already on screen.
      img.src = url
    }, 900)
    return () => { cancelled = true; clearTimeout(t) }
  }, [sourceId, duration, aspect])

  // Clips already cut from this source, as spans for the ghost lane.
  const ghosts = useMemo(() => (existingClips || [])
    .filter((c) => c.clip_start_seconds != null && c.clip_end_seconds != null)
    .map((c) => ({ start: c.clip_start_seconds, end: c.clip_end_seconds })),
  [existingClips])

  // ── Player wiring ──────────────────────────────────────────
  const seek = useCallback((t) => {
    const v = videoRef.current
    const clamped = Math.max(0, Math.min(t, duration || t))
    setPlayhead(clamped)
    if (v && Number.isFinite(clamped)) v.currentTime = clamped
  }, [duration])

  const nudgePlayhead = useCallback((d) => seek(playhead + d), [seek, playhead])

  /** Play — scoped to the selected range when one is active.
   *  Entering from outside the range (or from its very end) restarts at its
   *  IN point, because "play this clip" should always show the hook. */
  const togglePlay = useCallback(() => {
    const v = videoRef.current
    if (!v) return
    if (!v.paused) { v.pause(); return }
    const a = activeRef.current
    if (scoped && a) {
      const t = v.currentTime
      if (t < a.start - 0.05 || t > a.end - 0.05) v.currentTime = a.start
    }
    v.play().catch(() => {})
  }, [scoped])

  /** The boundary guard. Fires on timeupdate (~4/s), so a clip can overrun
   *  its OUT point by a frame or two — pausing exactly on the boundary
   *  would need a rAF loop for a preview nobody frame-inspects. */
  const onTime = useCallback((e) => {
    const v = e.currentTarget
    setPlayhead(v.currentTime)
    const a = activeRef.current
    if (scoped && a && !v.paused && v.currentTime >= a.end) {
      v.pause()
      v.currentTime = a.start   // parked ready to replay the same clip
    }
  }, [scoped])

  const addAtPlayhead = useCallback(() => {
    if (R.atCap) {
      showSnackbar(`The bench holds ${MAX_RANGES} ranges — cut or remove some first`, "warning")
      return
    }
    if (!R.addAt(playhead)) {
      showSnackbar("No free space here — move the playhead, or drag on the strip", "warning")
    }
  }, [R, playhead, showSnackbar])

  // ── AI picks ───────────────────────────────────────────────
  // The WS status is a HINT that the search has landed; the job row is the
  // truth, because `completeJob` records a status and never the payload.
  const suggestJobStatus = useAppStore(
    (s) => (suggestJobId ? s.activeJobs[suggestJobId]?.status : null),
  )

  // Held in refs so the collector below can depend on the JOB alone. `R` is a
  // fresh object every render, and having it in the dep array is what made
  // this effect tear itself down mid-flight.
  const collectDeps = useRef(null)
  collectDeps.current = { addMany: R.addMany, showSnackbar }

  /* Collect the proposals once the search lands.
   *
   * Two failures this has to survive, both of which the first version did not:
   *
   *   * Its own state change. It called `setSuggestJobId(null)` up front and
   *     returned a cleanup that cancelled the in-flight fetch — so the
   *     re-render that clearing caused ABORTED the request that was going to
   *     read the results. Measured on the live page: adoption worked about
   *     one run in two, and the failing half silently dropped windows the
   *     model had already been asked to choose.
   *   * A missed `job_complete`. `activeJobs` is WS-fed and evicts, so a
   *     dropped frame left `suggestJobId` set forever: "Reading the video…"
   *     spinning for the rest of the session over a finished search.
   *
   * So the job row is POLLED, and the WS status only decides how soon the
   * first look happens. The id is cleared after the row is read, not before,
   * which also means the button stops spinning exactly when there is
   * something to show for it.
   */
  useEffect(() => {
    const jobId = suggestJobId
    if (!jobId) return
    const wsSaysDone = suggestJobStatus && suggestJobStatus !== "running" && suggestJobStatus !== "pending"
    let alive = true
    let timer = 0

    const finish = (msg, severity) => {
      setSuggestJobId(null)
      if (msg) collectDeps.current.showSnackbar(msg, severity)
    }

    const look = async () => {
      if (!alive) return
      let data
      try {
        ({ data } = await http.get(`/api/jobs/${jobId}`))
      } catch {
        // A blip is not a verdict — the job is still out there.
        timer = setTimeout(look, 5000)
        return
      }
      if (!alive) return
      const status = data?.status
      if (status !== "success" && status !== "failed" && status !== "cancelled") {
        timer = setTimeout(look, 3000)
        return
      }
      if (status !== "success") {
        finish(data?.error_message || "Could not find moments in this video", "error")
        return
      }
      let out = data.output || null
      if (!out && data.output_json) {
        try { out = JSON.parse(data.output_json) } catch { out = null }
      }
      const found = out?.suggestions || []
      if (!found.length) {
        finish("The AI found no clip-worthy moments — try a wider length range", "warning")
        return
      }
      const accepted = collectDeps.current.addMany(found.map((f) => ({
        start: f.start, end: f.end,
        meta: { title: f.title, score: f.score, hook_score: f.hook_score, reason: f.reason },
      })))
      finish(
        accepted < found.length
          // Two reasons a proposal can be dropped now — the cap, and an
          // overlap with something already down. Naming only the cap was
          // wrong the moment the overlap check landed.
          ? `Added ${accepted} of ${found.length} moments — the rest overlap cuts already on the bench, or pass the ${MAX_RANGES}-range cap. Adjust them, then cut.`
          : `Added ${accepted} moment${accepted !== 1 ? "s" : ""} — adjust them, then cut.`,
        accepted < found.length ? "warning" : "success",
      )
    }

    // Whisper can run here, so an unheard job is minutes of polling at worst.
    timer = setTimeout(look, wsSaysDone ? 0 : 4000)
    return () => { alive = false; clearTimeout(timer) }
    // Deliberately NOT depending on R / showSnackbar — see collectDeps.
  }, [suggestJobId, suggestJobStatus])

  const findMoments = async () => {
    if (!sourceId || suggesting) return
    try {
      const body = { max_clips: Math.max(1, Math.min(MAX_RANGES, Number(ai.max_clips) || 5)) }
      if (ai.min_duration) body.min_duration = Number(ai.min_duration)
      if (ai.max_duration) body.max_duration = Number(ai.max_duration)
      if (ai.user_query.trim()) body.user_query = ai.user_query.trim()
      if (ai.target_platform) body.target_platform = ai.target_platform
      if (ai.genre) body.genre = ai.genre
      body.whisper_quality = settings.whisper_quality
      if (settings.force_retranscribe) body.force_retranscribe = true
      const { data } = await http.post(`/api/downloaded/${sourceId}/suggest-clips`, body)
      setSuggestJobId(data.job_id)
      showSnackbar("Reading the video for its strongest moments…", "info")
    } catch (e) {
      showSnackbar(e.response?.data?.detail || "Could not start the search", "error")
    }
  }

  // ── Keyboard ───────────────────────────────────────────────
  // The root carries tabIndex={-1}, so a click anywhere inside the bench
  // focuses it (the browser walks up to the nearest focusable ancestor) and
  // the handler below starts hearing keys. Selecting a source on the left
  // rail is NOT such a click — focus stays on the sidebar card — so every
  // shortcut in the help popover silently did nothing until you happened to
  // click the bench first. Claim focus when a source opens.
  useEffect(() => {
    if (sourceId) rootRef.current?.focus({ preventScroll: true })
  }, [sourceId])

  // Scoped to the bench (not window) and skipped while a field has focus,
  // so typing "5" into the clip-count box can't seek the video.
  useEffect(() => {
    const el = rootRef.current
    if (!el) return
    const onKey = (e) => {
      const tag = e.target?.tagName
      if (tag === "INPUT" || tag === "TEXTAREA" || e.target?.isContentEditable) return
      // The native player owns its own keys. `<video controls>` is focusable,
      // so after clicking it Space reached BOTH the browser's play/pause and
      // togglePlay() below — two toggles, one press, and the video visibly
      // refused to start. Media controls win wherever they have focus.
      if (tag === "VIDEO" || tag === "AUDIO") return
      const step = e.shiftKey ? 1 : 1 / 30
      const a = R.active
      switch (e.key) {
        case " ":
          e.preventDefault()
          togglePlay()
          break
        case "ArrowLeft": e.preventDefault(); nudgePlayhead(-step); break
        case "ArrowRight": e.preventDefault(); nudgePlayhead(step); break
        case "j": case "J": e.preventDefault(); nudgePlayhead(-10); break
        case "l": case "L": e.preventDefault(); nudgePlayhead(10); break
        case "i": case "I":
          if (a) { e.preventDefault(); R.update(a.id, { start: playhead }); R.settle() }
          break
        case "o": case "O":
          if (a) { e.preventDefault(); R.update(a.id, { end: playhead }); R.settle() }
          break
        case "n": case "N": e.preventDefault(); addAtPlayhead(); break
        case "Backspace": case "Delete":
          if (a) { e.preventDefault(); R.remove(a.id) }
          break
        default: break
      }
    }
    el.addEventListener("keydown", onKey)
    return () => el.removeEventListener("keydown", onKey)
  }, [R, playhead, nudgePlayhead, addAtPlayhead, togglePlay])

  // Declared up here, not after the `!source` guard: `askAi` below reads it,
  // and a const referenced above its declaration is a TDZ ReferenceError at
  // render — which `vite build` compiles without a word, the same way it
  // compiles a JSX identifier that was never imported.
  const atCap = R.atCap

  /* ── Ask AI ─────────────────────────────────────────────────
     A BUTTON, not a mode. This was a "My ranges | AI picks" toggle in the
     header, and once the pending cuts moved to the rail it stopped being a
     mode at all: "My ranges" rendered nothing, and the strip stayed
     draggable in both — because dragging was never what the toggle
     controlled. It read as "you are in AI mode now", which was a promise
     the bench does not make.

     Asking the AI is a fourth way to put a block on the timeline, beside
     dragging, N, and paste. So it lives with them, in the rail header, and
     it hands back the same editable blocks they do.

     The targeting fields used to be a popover INSIDE this popover. There
     are three of them; they are just fields now. */
  const askAi = (
    <>
      <Tooltip title={atCap
        ? `The bench holds ${MAX_RANGES} ranges — cut or remove some first`
        : "Let the AI read the transcript and propose the strongest moments"}>
        <span>
          <Button
            size="small" disabled={suggesting || atCap}
            onClick={(e) => setAiAnchor(e.currentTarget)}
            startIcon={suggesting
              ? <CircularProgress size={12} color="inherit" />
              : <AutoAwesomeIcon sx={{ fontSize: 15 }} />}
            sx={{ textTransform: "none", fontSize: "0.7rem", fontWeight: 700, py: 0.1, px: 0.75, minWidth: 0 }}
          >
            {suggesting ? "Reading…" : "Ask AI"}
          </Button>
        </span>
      </Tooltip>

      <Popover
        open={Boolean(aiAnchor)} anchorEl={aiAnchor} onClose={() => setAiAnchor(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
        transformOrigin={{ vertical: "top", horizontal: "right" }}
      >
        <Stack spacing={1.5} sx={{ p: 2, width: 360 }}>
          <Typography variant="caption" sx={{ color: "text.secondary" }}>
            The AI reads the transcript and proposes the strongest moments.
            They arrive as blocks you can drag, retime or delete —{" "}
            <strong>nothing is cut</strong> until you press Cut.
          </Typography>
          <Stack direction="row" spacing={1.25}>
            <TextField
              label="How many" type="number" size="small" sx={{ flex: 1 }}
              value={ai.max_clips}
              slotProps={{ htmlInput: { min: 1, max: MAX_RANGES } }}
              onChange={(e) => setAi((p) => ({ ...p, max_clips: e.target.value }))}
            />
            <TextField
              label="Min (s)" type="number" size="small" sx={{ flex: 1 }}
              value={ai.min_duration}
              slotProps={{ htmlInput: { min: 10, max: 120 } }}
              onChange={(e) => setAi((p) => ({ ...p, min_duration: e.target.value }))}
            />
            <TextField
              label="Max (s)" type="number" size="small" sx={{ flex: 1 }}
              value={ai.max_duration}
              slotProps={{ htmlInput: { min: 15, max: 180 } }}
              onChange={(e) => setAi((p) => ({ ...p, max_duration: e.target.value }))}
            />
          </Stack>
          <TextField
            label="Find specific moments" size="small" fullWidth
            placeholder={'e.g. "every joke that landed"'}
            value={ai.user_query}
            slotProps={{ htmlInput: { maxLength: 500 } }}
            onChange={(e) => setAi((p) => ({ ...p, user_query: e.target.value }))}
          />
          <Stack direction="row" spacing={1.25}>
            <TextField select label="Target platform" size="small" sx={{ flex: 1 }}
              value={ai.target_platform}
              onChange={(e) => setAi((p) => ({ ...p, target_platform: e.target.value }))}>
              {PLATFORMS.map((o) => <MenuItem key={o.v} value={o.v}>{o.label}</MenuItem>)}
            </TextField>
            <TextField select label="Content genre" size="small" sx={{ flex: 1 }}
              value={ai.genre}
              onChange={(e) => setAi((p) => ({ ...p, genre: e.target.value }))}>
              {GENRES.map((o) => <MenuItem key={o.v} value={o.v}>{o.label}</MenuItem>)}
            </TextField>
          </Stack>
          <Typography variant="caption" sx={{ color: "text.disabled" }}>
            Up to {MAX_RANGES} proposals — every one is cut through the same path
            as a hand-drawn range.{" "}
            <Box component="span" onClick={() => { setAiAnchor(null); onOpenDialog() }}
              sx={{ color: "primary.main", cursor: "pointer", textDecoration: "underline" }}>
              Need more? Auto-cut instead →
            </Box>
          </Typography>
          <Button
            variant="contained" color="secondary" fullWidth
            disabled={suggesting || atCap}
            startIcon={suggesting
              ? <CircularProgress size={14} color="inherit" />
              : <AutoAwesomeIcon sx={{ fontSize: 17 }} />}
            onClick={() => { setAiAnchor(null); findMoments() }}
            sx={{ textTransform: "none", fontWeight: 700 }}
          >
            {suggesting ? "Reading the video…" : "Find moments"}
          </Button>
        </Stack>
      </Popover>
    </>
  )

  if (!source) return null

  const tooShort = duration > 0 && duration < MIN_LEN_SEC

  return (
    <Box
      ref={rootRef}
      tabIndex={-1}
      /* The bench shares the centre column with the clip filmstrip below
         it. Without minHeight:0 + overflow:hidden here, the bench's own
         content sets a floor on its height and the footer slides UNDER
         the filmstrip's toolbar — which is exactly what it did. Only the
         stage flexes; every other band is fixed and gets its space first. */
      sx={{
        flex: 1, minHeight: 0, overflow: "hidden",
        display: "flex", flexDirection: "column",
        p: 2, pb: 1.25, gap: 1, outline: "none",
      }}
    >
      {/* ── Header ─────────────────────────────────────────── */}
      <Stack direction="row" alignItems="center" spacing={1} sx={{ flexShrink: 0 }}>
        <ContentCutIcon sx={{ fontSize: 17, color: "primary.main" }} />
        <Typography variant="subtitle2" sx={{ fontWeight: 700, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {source.title || "Untitled"}
        </Typography>
        <Typography variant="caption" sx={{ color: "text.secondary", flexShrink: 0 }}>
          {formatTime(duration)}
        </Typography>
        <Box sx={{ flex: 1 }} />

        <Tooltip title={segments.length
          ? (snapEnabled ? "Handles snap to sentence boundaries — click to free them (or hold Alt while dragging)"
                         : "Handles move freely — click to snap them to sentence boundaries")
          : "This video has no transcript, so there are no sentence boundaries to snap to"}>
          <span>
            <IconButton size="small" disabled={!segments.length}
              aria-label={snapEnabled ? "Snapping to sentences is on" : "Snapping to sentences is off"}
              aria-pressed={snapEnabled && segments.length > 0}
              onClick={() => setSnapEnabled((v) => !v)}
              sx={{ color: snapEnabled && segments.length ? "success.main" : "text.disabled" }}>
              <MagnetIcon sx={{ fontSize: 17 }} />
            </IconButton>
          </span>
        </Tooltip>
        <Tooltip title="Keyboard shortcuts">
          <IconButton size="small" aria-label="Keyboard shortcuts"
            onClick={(e) => setHelpAnchor(e.currentTarget)} sx={{ color: "text.secondary" }}>
            <KeyboardIcon sx={{ fontSize: 17 }} />
          </IconButton>
        </Tooltip>
      </Stack>

      <Popover
        open={Boolean(helpAnchor)} anchorEl={helpAnchor} onClose={() => setHelpAnchor(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
        transformOrigin={{ vertical: "top", horizontal: "right" }}
      >
        <Stack spacing={0.5} sx={{ p: 1.5, minWidth: 260 }}>
          {SHORTCUTS.map(([k, d]) => (
            <Stack key={k} direction="row" spacing={1.5} alignItems="center">
              <Box sx={{
                minWidth: 62, px: 0.75, py: 0.15, borderRadius: 1,
                border: "1px solid", borderColor: "divider",
                fontFamily: "ui-monospace, monospace", fontSize: "0.68rem",
                textAlign: "center", flexShrink: 0,
              }}>{k}</Box>
              <Typography variant="caption" sx={{ color: "text.secondary" }}>{d}</Typography>
            </Stack>
          ))}
        </Stack>
      </Popover>

      {/* ── IN | player | OUT | pending cuts ───────────────
          The range list used to be a full-width band below the timeline: one
          1200px bar per range carrying ~200px of content, four of them
          costing ~180px of height taken from the video. It is a column here
          instead, so the list grows into space this row already had and the
          player's height stops depending on how many cuts are pending. */}
      <Box sx={{
        flex: "1 1 auto", minHeight: 132, overflow: "hidden",
        display: "flex", gap: 1.5, alignItems: "stretch",
      }}>
        <Box sx={{ flex: 1, minWidth: 0, minHeight: 0 }}>
        <InOutFrames
          sourceId={sourceId}
          range={R.active}
          playhead={playhead}
          duration={duration}
          aspect={aspect}
          strip={fineStrip || strip}
          onChange={(patch) => { if (R.active) { R.update(R.active.id, patch); R.settle() } }}
        >
          <Box sx={(t) => ({
            position: "relative",
            height: "100%", minHeight: 0,
            aspectRatio: String(aspect),
            maxWidth: "100%",
            borderRadius: 2, overflow: "hidden", bgcolor: "#000",
            border: 1, borderColor: "divider",
            boxShadow: t.palette.mode === "dark"
              ? "0 10px 30px rgba(0,0,0,0.5)" : "0 10px 30px rgba(80,90,140,0.18)",
          })}>
            <Box
              component="video"
              ref={videoRef}
              key={sourceId}
              src={src}
              controls
              preload="metadata"
              onLoadedMetadata={(e) => {
                const { videoWidth: w, videoHeight: h } = e.currentTarget
                // The filmstrip sizes its cells from this, so a portrait
                // source gets narrow cells instead of stretched ones.
                if (w && h) setAspect(w / h)
              }}
              onTimeUpdate={onTime}
              onSeeked={(e) => setPlayhead(e.currentTarget.currentTime)}
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              sx={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
            />
          </Box>
        </InOutFrames>
        </Box>

        <RangeRail
          ranges={R.ranges}
          activeId={R.activeId}
          atCap={atCap}
          onSelect={(id) => {
            R.setActiveId(id)
            const r = R.ranges.find((x) => x.id === id)
            if (r && scoped) seek(r.start)
          }}
          onRemove={R.remove}
          // A typed bound goes through the SAME pair the drag handles use, so
          // the block moves on the timeline, the IN/OUT frames re-cue and
          // scoped playback re-scopes — one model, two ways in.
          onEdit={(id, patch) => { R.setActiveId(id); R.update(id, patch); R.settle() }}
          onAdd={addAtPlayhead}
          onPaste={() => setPasteOpen(true)}
          onClear={R.clear}
          aiSlot={askAi}
        />
      </Box>

      {/* ── Transport ──────────────────────────────────────
          The native controls scrub the whole SOURCE, which is right — the
          timeline below is the source's scrubber too. This row is about
          the selected CLIP: play it from its own IN point and stop at its
          OUT point, which the source's controls cannot express. */}
      <Stack direction="row" spacing={1} alignItems="center"
        justifyContent="center" sx={{ flexShrink: 0 }}>
        <Button
          size="small" variant="contained" color="primary"
          disabled={!R.active}
          startIcon={playing ? <PauseIcon sx={{ fontSize: 17 }} /> : <PlayArrowIcon sx={{ fontSize: 17 }} />}
          onClick={togglePlay}
          sx={{ textTransform: "none", fontWeight: 700, minWidth: 128 }}
        >
          {playing ? "Pause" : R.active ? `Play clip ${R.ranges.indexOf(R.active) + 1}` : "Play clip"}
        </Button>
        <Tooltip title={scoped
          ? "Playback is limited to the selected clip — it starts at IN and stops at OUT"
          : "Playback runs through the whole source, ignoring the selection"}>
          <Chip
            size="small"
            label={scoped ? "Selection only" : "Whole video"}
            color={scoped ? "primary" : "default"}
            variant={scoped ? "filled" : "outlined"}
            onClick={() => setScoped((v) => !v)}
          />
        </Tooltip>
        {R.active && (
          <Typography variant="caption" sx={{ color: "text.secondary", fontFamily: "ui-monospace, monospace" }}>
            {formatTime(R.active.start)}–{formatTime(R.active.end)} · {Math.round(R.active.end - R.active.start)}s
          </Typography>
        )}
      </Stack>

      {/* ── Timeline ───────────────────────────────────────── */}
      <Box sx={{ flexShrink: 0 }}>
        <Timeline
          sourceId={sourceId}
          duration={duration}
          aspect={aspect}
          segments={segments}
          ranges={R.ranges}
          activeId={R.activeId}
          ghosts={ghosts}
          playhead={playhead}
          snapEnabled={snapEnabled}
          onSeek={seek}
          onRangeSelect={(id) => {
            R.setActiveId(id)
            // Selecting a clip should also cue it up — otherwise pressing
            // play after clicking a block starts wherever you last were.
            const r = R.ranges.find((x) => x.id === id)
            if (r && scoped) seek(r.start)
          }}
          onRangeChange={R.update}
          onRangeCommit={R.settle}
          onRangeAdd={(a, b) => {
            if (atCap) { showSnackbar(`The bench holds ${MAX_RANGES} ranges — cut or remove some first`, "warning"); return }
            if (R.add(a, b) == null) {
              // add() refused: the span was clamped away by a pending cut,
              // or is shorter than the 1s floor. Say so — a drag that lands
              // nothing with no word reads as a broken timeline.
              showSnackbar("No room there — that stretch is under a pending cut or shorter than 1s", "info")
            }
          }}
          onRangeRemove={R.remove}
          onStrip={setStrip}
        />
        <Stack direction="row" spacing={1.5} sx={{ mt: 0.75, px: 0.25 }} alignItems="center" flexWrap="wrap" useFlexGap>
          <Typography variant="caption" sx={{ color: "text.disabled" }}>
            Drag on the strip to make a clip · drag its edges to trim
          </Typography>
          <Box sx={{ flex: 1 }} />
          {segments.length > 0 && (
            <LegendDot color="success.main" label="speech" />
          )}
          {ghosts.length > 0 && <LegendDot color="text.disabled" label="already cut" />}
        </Stack>
      </Box>

      {/* ── Footer: the per-clip settings ──────────────────── */}
      <Divider sx={{ flexShrink: 0 }} />
      <Stack direction="row" spacing={1.25} alignItems="center" flexWrap="wrap" useFlexGap sx={{ flexShrink: 0 }}>
        <TextField
          select size="small" label="Captions" sx={{ width: 132 }}
          value={settings.caption_style}
          onChange={(e) => onSettings({ caption_style: e.target.value })}
        >
          {CAPTION_OPTIONS.map((o) => <MenuItem key={o.value} value={o.value}>{o.label}</MenuItem>)}
        </TextField>
        {settings.caption_style !== "none" && (
          <TextField
            select size="small" label="Emoji" sx={{ width: 112 }}
            value={settings.emoji_style}
            onChange={(e) => onSettings({ emoji_style: e.target.value })}
          >
            {EMOJI_OPTIONS.map((o) => <MenuItem key={o.v} value={o.v}>{o.label}</MenuItem>)}
          </TextField>
        )}
        <Tooltip title="Convert a landscape source to 9:16 with a blurred fill">
          <Chip
            label="Vertical 9:16" size="small"
            color={settings.force_vertical ? "primary" : "default"}
            variant={settings.force_vertical ? "filled" : "outlined"}
            onClick={() => onSettings({ force_vertical: !settings.force_vertical })}
          />
        </Tooltip>
        <Tooltip title="Trim silent gaps and filler words INSIDE each cut — your in/out points don't move">
          <Chip
            label="Trim silence" size="small"
            color={settings.remove_silence ? "primary" : "default"}
            variant={settings.remove_silence ? "filled" : "outlined"}
            onClick={() => onSettings({ remove_silence: !settings.remove_silence })}
          />
        </Tooltip>
        {/* Transcription used to be reachable only through the dialog,
            which meant one setting forced you onto the other surface. */}
        <Tooltip title={source.has_transcript_segments
          ? "This video already has a transcript — Whisper is skipped unless you ask for a fresh one"
          : "No transcript yet: Whisper runs before captions, silence trimming and Ask AI"}>
          <Chip
            size="small" variant="outlined"
            icon={<RecordVoiceOverIcon sx={{ fontSize: 14 }} />}
            label={source.has_transcript_segments && !settings.force_retranscribe
              ? "Transcript cached"
              : `Whisper: ${settings.whisper_quality}`}
            onClick={(e) => setTxAnchor(e.currentTarget)}
          />
        </Tooltip>
        <Popover
          open={Boolean(txAnchor)} anchorEl={txAnchor} onClose={() => setTxAnchor(null)}
          anchorOrigin={{ vertical: "top", horizontal: "left" }}
          transformOrigin={{ vertical: "bottom", horizontal: "left" }}
        >
          <Stack spacing={1.25} sx={{ p: 2, width: 300 }}>
            <TextField
              select size="small" fullWidth label="Whisper quality"
              value={settings.whisper_quality}
              onChange={(e) => onSettings({ whisper_quality: e.target.value })}
            >
              {WHISPER_QUALITIES.map((q) => (
                <MenuItem key={q.v} value={q.v}>{q.label}</MenuItem>
              ))}
            </TextField>
            <FormControlLabel
              control={<Checkbox size="small"
                checked={!source.has_transcript_segments || settings.force_retranscribe}
                disabled={!source.has_transcript_segments}
                onChange={(e) => onSettings({ force_retranscribe: e.target.checked })} />}
              label={<Typography variant="caption">
                Re-transcribe this video{source.has_transcript_segments ? "" : " (required — none cached)"}
              </Typography>}
            />
          </Stack>
        </Popover>

        <Box sx={{ flex: 1 }} />

        {tooShort && (
          <Typography variant="caption" sx={{ color: "warning.main" }}>
            This video is shorter than one second
          </Typography>
        )}
        {/* Cut lives in the page hero (top-right, before Auto-cut) — the
            app's convention for a primary action, and it puts the two ways of
            cutting side by side instead of at opposite corners. This row is
            purely the per-clip settings that shape whatever it cuts. */}
      </Stack>
      <PasteRangesDialog
        open={pasteOpen}
        onClose={() => setPasteOpen(false)}
        duration={duration}
        room={MAX_RANGES - R.ranges.length}
        onAdd={(items) => {
          const added = R.addMany(items)
          showSnackbar(
            added === items.length
              ? `Added ${added} range${added !== 1 ? "s" : ""}`
              : `Added ${added} of ${items.length} — the rest overlap ranges already on the bench, or pass the ${MAX_RANGES}-range cap`,
            added === items.length ? "success" : "warning",
          )
          setPasteOpen(false)
        }}
      />
    </Box>
  )
}

/* Paste-a-list-of-times. Parses as you type so the outcome is visible
   before you commit, and reports bad lines individually — a paste of
   twelve ranges with one typo should add the eleven good ones. */
function PasteRangesDialog({ open, onClose, duration, room, onAdd }) {
  const [text, setText] = useState("")
  useEffect(() => { if (open) setText("") }, [open])
  const { ranges, errors } = useMemo(
    () => parseRangeText(text, duration, MIN_LEN_SEC),
    [text, duration],
  )
  const willAdd = Math.max(0, Math.min(ranges.length, room))

  return (
    <Dialog open={open} onClose={onClose} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <ContentPasteIcon color="primary" /> Paste timestamps
      </DialogTitle>
      <DialogContent>
        <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mb: 1.5 }}>
          One range per line, or separated by commas. <code>0:42-1:05</code>,{" "}
          <code>2:10 → 2:38</code>, <code>1:02:00 to 1:02:30</code> all work.
        </Typography>
        <TextField
          autoFocus multiline minRows={5} maxRows={12} fullWidth size="small"
          placeholder={"0:42-1:05\n2:10 - 2:38\n4:02.1 → 4:31.9"}
          value={text}
          onChange={(e) => setText(e.target.value)}
          slotProps={{ htmlInput: { style: { fontFamily: "ui-monospace, monospace", fontSize: "0.82rem" } } }}
        />
        <Stack spacing={0.5} sx={{ mt: 1.25 }}>
          {ranges.length > 0 && (
            <Typography variant="caption" sx={{ color: "success.main", fontWeight: 700 }}>
              {ranges.length} range{ranges.length !== 1 ? "s" : ""} read
              {willAdd < ranges.length && ` · only ${willAdd} will fit`}
            </Typography>
          )}
          {errors.map((e, i) => (
            <Typography key={i} variant="caption" sx={{ color: "warning.main" }}>
              {e}
            </Typography>
          ))}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={willAdd === 0}
          onClick={() => onAdd(ranges.slice(0, willAdd))}>
          Add {willAdd || ""} to timeline
        </Button>
      </DialogActions>
    </Dialog>
  )
}

function LegendDot({ color, label }) {
  return (
    <Stack direction="row" spacing={0.5} alignItems="center">
      <Box sx={{ width: 8, height: 8, borderRadius: "2px", bgcolor: color, opacity: 0.6 }} />
      <Typography variant="caption" sx={{ color: "text.disabled" }}>{label}</Typography>
    </Stack>
  )
}

