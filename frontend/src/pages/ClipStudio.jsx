import { useState, useEffect, useCallback, useMemo, useRef } from "react"
import { useNavigate } from "react-router-dom"
import {
  Box, Typography, Stack, Chip, IconButton, Button, Divider,
  TextField, Tooltip, CircularProgress, Menu, MenuItem,
  ListItemText, ListItemIcon, Checkbox,
  Dialog, DialogContent, ToggleButton, ToggleButtonGroup, alpha,
} from "@mui/material"
import ContentCutIcon from "@mui/icons-material/ContentCut"
import AutoFixHighIcon from "@mui/icons-material/AutoFixHighOutlined"
import ContentCutOutlinedIcon from "@mui/icons-material/ContentCutOutlined"
import WhatshotIcon from "@mui/icons-material/Whatshot"
import AccessTimeIcon from "@mui/icons-material/AccessTime"
import EditIcon from "@mui/icons-material/Edit"
import SaveIcon from "@mui/icons-material/Save"
import DeleteIcon from "@mui/icons-material/Delete"
import PhotoCameraIcon from "@mui/icons-material/PhotoCamera"
import CloudDownloadIcon from "@mui/icons-material/CloudDownloadOutlined"
import FileUploadIcon from "@mui/icons-material/FileUploadOutlined"
import FolderZipIcon from "@mui/icons-material/FolderZip"
import AspectRatioIcon from "@mui/icons-material/AspectRatio"
import WarningAmberIcon from "@mui/icons-material/WarningAmber"
import SearchIcon from "@mui/icons-material/Search"
import SortIcon from "@mui/icons-material/Sort"
import CloseIcon from "@mui/icons-material/Close"
import CheckCircleIcon from "@mui/icons-material/CheckCircle"
import FolderOpenIcon from "@mui/icons-material/FolderOpen"
import UnfoldMoreIcon from "@mui/icons-material/UnfoldMore"
import UnfoldLessIcon from "@mui/icons-material/UnfoldLess"
import MovieFilterOutlinedIcon from "@mui/icons-material/MovieFilterOutlined"
import SlideshowOutlinedIcon from "@mui/icons-material/SlideshowOutlined"
import UploadIcon from "@mui/icons-material/Upload"
import RefreshIcon from "@mui/icons-material/Refresh"
import ExpandMoreIcon from "@mui/icons-material/ExpandMore"
import http from "../api/http"
import useAppStore from "../store/appStore"
import useDocumentTitle from "../hooks/useDocumentTitle"
import useConfirm from "../hooks/useConfirm"
import PageHero from "../components/PageHero"
import EmptyState from "../components/EmptyState"
import ScoreBreakdownPanel from "../components/clip/ScoreBreakdownPanel"
import SourceVideoCard, { MissingSourceRow } from "../components/clip/SourceVideoCard"
import ActiveJobsBanner from "../components/create/ActiveJobsBanner"
import ChannelNewest from "../components/clip/ChannelNewest"
import ClipCard from "../components/clip/ClipCard"
import ExtractDialog from "../components/clip/ExtractDialog"
import SourceBench, { DISABLED_CONTAINED } from "../components/clip/bench/SourceBench"
import useClipSettings from "../components/clip/useClipSettings"
import useBenchRanges, { MIN_LEN_SEC } from "../components/clip/bench/useBenchRanges"
import useResizableWidth from "../hooks/useResizableWidth"
import { formatTime, viralityColor, viralityLabel, hookTypeLabel, clipAspectCss } from "../components/clip/clipFormat"
import { visibleClips } from "../components/clip/clipOrder"
import { GlassPanel, GlassCard } from "../utils/glassFx"
import { scrollOnHover } from "../utils/scrollbar"
import usePersistentFlag from "../hooks/usePersistentFlag"
import { storageKey } from "../utils/storage"
import AppSkeleton from "../components/AppSkeleton"

const SOURCE_RAIL_WIDTH = {
  storageKey: "vm_clipper_sources_w", initial: 200, min: 170, max: 420,
  label: "the source list", anchor: "left",
}

/* ══════════════════════════════════════════════════════════════
   MAIN COMPONENT: Clip Studio
   ══════════════════════════════════════════════════════════════ */

// Below this page width the hero's actions go compact (search → icon,
// Import / Refresh → icon). At 1100 a 1280px window (page ~1040px) goes
// compact too, which keeps the title on one line.
const COMPACT_HERO_BELOW = 1100

// Width of an element that may mount late (the page renders a skeleton
// first), via a callback ref.
function useElementWidth() {
  const [el, setEl] = useState(null)
  const [w, setW] = useState(0)
  useEffect(() => {
    if (!el) return undefined
    setW(el.getBoundingClientRect().width)
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width))
    ro.observe(el)
    return () => ro.disconnect()
  }, [el])
  return [setEl, w]
}

export default function ClipStudio() {
  useDocumentTitle("Clipper")
  const navigate = useNavigate()
  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const activeJobs = useAppStore((s) => s.activeJobs)

  // Track clip extraction jobs. Keyed ONLY on the job's input payload: the
  // extract-clips endpoint stamps `type: "clip_extraction"` into both the WS
  // job_started event (live) and the job's input_json (restored on reload),
  // so both paths are covered. A previous `message.includes("clip")` fallback
  // also matched merge-clips / extend-clip / ai-video-clip jobs, whose
  // progress steps read "Stitching 3 clips…" — those spun this page's Extract
  // button and fired a bogus "clips extracted" refresh.
  const isClipJob = (j) => j.inputData?.type === "clip_extraction"
  const clipJobs = Object.values(activeJobs).filter(isClipJob)
  // A clip extraction is a background Job that outlives the POST. Keep the
  // Extract button in a "working" state while one is running (it otherwise
  // un-spun the instant the request returned, looking like nothing happened —
  // the actual extraction takes minutes). Global progress still shows in the
  // sidebar jobs pill; this is the on-page affordance.
  const clipJobFilter = (j) => j.status === "running" && isClipJob(j)
  const clipJobRunning = clipJobs.some(clipJobFilter)
  const justCompletedRef = useRef(new Set())

  // Data
  const [sources, setSources] = useState([])
  const [missingSources, setMissingSources] = useState([])
  const [missingOpen, setMissingOpen] = useState(false)
  const [missingBusy, setMissingBusy] = useState(null)   // id being removed / re-downloaded
  const [clips, setClips] = useState([])
  // The list endpoint caps at 100; total says how many exist so the header
  // doesn't silently undercount a heavy user's library.
  const [clipTotal, setClipTotal] = useState(null)
  const [loading, setLoading] = useState(true)
  const [selectedSourceId, setSelectedSourceId] = useState(null) // "all" or video id
  const [selectedClip, setSelectedClip] = useState(null)
  const [previewVideo, setPreviewVideo] = useState(null) // source video for play popup
  const [sourceFilter, setSourceFilter] = useState("")
  const [sourceVisibleCount, setSourceVisibleCount] = useState(8)
  const SOURCE_LOAD_MORE = 10
  const [searchQuery, setSearchQuery] = useState("")
  const [searchOpen, setSearchOpen] = useState(false)
  // The page's own width (the app sidebar collapses, so a window breakpoint
  // mis-fires): below this the hero's actions go compact.
  const [pageRef, pageW] = useElementWidth()
  const compactHero = pageW > 0 && pageW < COMPACT_HERO_BELOW
  const [sortBy, setSortBy] = useState("virality") // virality | newest | duration
  const [sortAnchor, setSortAnchor] = useState(null)

  // Extract dialog
  const [extractDialogOpen, setExtractDialogOpen] = useState(false)
  const [extractTarget, setExtractTarget] = useState(null)
  const [extracting, setExtracting] = useState(false)

  // Edit mode
  const [editing, setEditing] = useState(false)
  const [editDraft, setEditDraft] = useState({})
  const [saving, setSaving] = useState(false)

  // Regen thumbnail
  const [regenThumb, setRegenThumb] = useState(false)

  // Export selection
  const [checkedClips, setCheckedClips] = useState(new Set())
  const [zipping, setZipping] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [deleting, setDeleting] = useState(false)

  // Delete confirmation
  const { confirm: openConfirm, ConfirmDialog } = useConfirm()

  // Filmstrip collapsed state — persisted to localStorage so the user's
  // choice survives reloads. Default expanded; users with small detail
  // panels can collapse to ~52px-tall rows. Reads inside the lazy
  // initializer (run once on mount) so we don't hammer storage on every
  // render. The `try/catch` covers the rare browser where localStorage
  // is disabled (private mode, sandboxed iframe).
  const [filmstripCollapsed, setFilmstripCollapsed] = usePersistentFlag(
    storageKey("clipper", "filmstripCollapsed"),
    false,
    // Migrated from the pre-namespace key so the user's collapse choice
    // survives the rename.
    { legacyKey: "clipstudio_filmstrip_collapsed" },
  )

  // Which surface the centre column shows.
  //   "bench" — work ON the selected source: scrub it, drag ranges over a
  //             filmstrip, watch the IN/OUT frames, then cut.
  //   "clip"  — inspect one finished clip (the original behaviour).
  // Selecting a source used to leave the centre blank whenever that source
  // had no clips yet, which is the moment the user most needs a workspace.
  const [centerMode, setCenterMode] = useState("bench")

  // Per-clip settings (captions / emoji / silence / vertical /
  // transcription) are owned HERE and handed to both surfaces. They used
  // to be duplicated inside the bench and the dialog, so a caption style
  // picked in one did nothing in the other and whichever you finished in
  // decided the render.
  const clipSettings = useClipSettings()

  // Video player ref
  const videoRef = useRef(null)

  // Pause every player on the page whenever the source-video preview opens,
  // so two videos aren't playing (and audible) at the same time — the bench
  // player is inside SourceBench, so a ref to the inspector alone missed it.
  useEffect(() => {
    if (previewVideo) document.querySelectorAll("video").forEach(v => { try { v.pause() } catch { /* detached */ } })
  }, [previewVideo])

  // Import a local file. Routes through /api/downloaded/import — the same
  // endpoint the Library "Downloaded" tab uses — so it lands in VIDEOS_DIR and
  // shows up as a Downloaded source here AND in the Library.
  const [importing, setImporting] = useState(false)
  const fileInputRef = useRef(null)

  // ── Load data ────────────────────────────────────────────────
  const fetchData = useCallback(async () => {
    try {
      const [srcRes, clipRes] = await Promise.all([
        http.get("/api/downloaded", { params: { limit: 200 } }),
        // Filter server-side. Fetching the whole Library and filtering here
        // meant the page showed only the clips that happened to land in the
        // newest N rows — with enough recent Smart Videos / AI images ahead
        // of them, Clipper rendered "No clips yet" while the clips existed.
        // 100 is /api/videos' max limit.
        http.get("/api/videos", { params: { limit: 100, source_type: "clip_extraction" } }),
      ])
      // Clip Studio is video-only — extract-clips requires a real video file.
      // Rows whose video is NOT on disk are split out rather than dropped or
      // shown as ordinary sources: an ordinary card opened a bench whose
      // player, filmstrip and frames all failed with nothing said ("I
      // selected a clip but it cannot be played"). They go to the "File
      // missing" list, where the user can remove them or download them again.
      // Audio-only rows (an mp3, no video file ever) are neither — they
      // simply aren't clippable, so they don't appear at all.
      const rows = (srcRes.data?.videos || srcRes.data || [])
        .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))
      // An older backend sends no video_exists: treat the row as present.
      setSources(rows.filter(v => v.video_exists !== false))
      setMissingSources(rows.filter(v => v.video_exists === false && v.video_path))

      // Belt-and-braces: the request already filters by source_type.
      const clipVideos = (clipRes.data.videos || []).filter(v => v.source_type === "clip_extraction")
      setClips(clipVideos)
      setClipTotal(typeof clipRes.data.total === "number" ? clipRes.data.total : null)

      // Don't auto-select — let user choose from filmstrip or sidebar
    } catch (e) {
      console.error("Failed to load clip studio data:", e)
      showSnackbar("Failed to load clip data", "error")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchData() }, [fetchData])

  const handleImportVideo = async (event) => {
    const file = event.target.files?.[0]
    if (!file) return
    event.target.value = ""
    // Clip Studio is video-only. The file picker's accept= is just a hint and
    // can be bypassed; guard here so an audio file can't be imported only to
    // silently never appear (it has no video to clip).
    const isVideo = file.type.startsWith("video/") || /\.(mp4|mov|avi|mkv|webm|m4v)$/i.test(file.name)
    if (!isVideo) {
      showSnackbar("Clipper works with video files only — audio can't be clipped.", "warning")
      return
    }
    setImporting(true)
    try {
      const formData = new FormData()
      formData.append("file", file)
      formData.append("title", file.name.replace(/\.[^.]+$/, ""))
      const res = await http.post("/api/downloaded/import", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      })
      showSnackbar(res.data.message || "Video imported to your library", "success")
      await fetchData()  // surface the new source immediately
    } catch (err) {
      showSnackbar(err.response?.data?.detail || "Import failed", "error")
    } finally {
      setImporting(false)
    }
  }

  // ── Sources whose file is gone ───────────────────────────────
  // Remove = the same DELETE the Library uses (the row, and whatever files it
  // still has). Download again = the same batch-download door the Channels
  // page uses, from the URL the row remembers; the old row is left alone (the
  // new download is a new source) so the user decides what goes.
  const removeMissing = (video) => openConfirm(
    "Remove this video?",
    `"${video.title || "Untitled"}" is no longer on disk. This removes it from your sources; clips already cut from it stay.`,
    async () => {
      setMissingBusy(video.id)
      try {
        await http.delete(`/api/downloaded/${video.id}`)
        setMissingSources(prev => prev.filter(v => v.id !== video.id))
        showSnackbar("Removed", "success")
      } catch (e) {
        showSnackbar(`Remove failed: ${e.response?.data?.detail || e.message}`, "error")
      } finally {
        setMissingBusy(null)
      }
    },
  )
  const redownloadMissing = async (video) => {
    setMissingBusy(video.id)
    try {
      await http.post("/api/downloaded/batch-download", {
        urls: [{ url: video.source_url, title: video.title || "" }],
      })
      showSnackbar("Downloading again — it will appear in your sources when it lands", "success")
    } catch (e) {
      showSnackbar(`Download failed: ${e.response?.data?.detail || e.message}`, "error")
    } finally {
      setMissingBusy(null)
    }
  }
  // The bench found the file gone after the list was read (deleted while the
  // page was open): re-read, which files it under "File missing", and leave
  // the bench — there is nothing on it to cut.
  const onSourceMissing = useCallback(() => {
    setMissingOpen(true)
    fetchData()
  }, [fetchData])

  // A clip's player failed. Ask the stream why (one byte) — a <video> error
  // carries no status — so the inspector can say "the file is gone" rather
  // than sit black.
  const [clipPlayError, setClipPlayError] = useState(null)
  useEffect(() => { setClipPlayError(null) }, [selectedClip?.id])
  const onClipVideoError = async () => {
    const id = selectedClip?.id
    if (!id) return
    let status = 0
    try {
      const r = await fetch(`/api/videos/${id}/stream`, { headers: { Range: "bytes=0-0" } })
      status = r.status
    } catch { /* unreachable — generic sentence */ }
    if (selectedClip?.id !== id) return
    setClipPlayError(status === 410
      ? "This clip's file is no longer on disk. Delete it to tidy the list."
      : status === 200 || status === 206
        ? "The browser can't play this file — its format or codec isn't supported. Download it to play it elsewhere."
        : `This clip couldn't be loaded${status ? ` (HTTP ${status})` : ""}.`)
  }

  // Upload straight to a connected YouTube / TikTok account (open-source
  // build — the hosted app has no auto-upload).
  const handleUpload = async (platform) => {
    if (!selectedClip) return
    try {
      await http.post(`/api/videos/${selectedClip.id}/upload`, { platforms: [platform] })
      showSnackbar(`Uploading to ${platform}...`, "success")
    } catch (e) {
      showSnackbar(`Upload failed: ${e.response?.data?.detail || e.message}`, "error")
    }
  }

  // Auto-refresh when a clip extraction OR suggestion job completes. The
  // suggestion half matters for a non-obvious reason: Ask AI may run Whisper
  // and back-fill the source row's transcript, and useClipSettings derives
  // force_retranscribe from has_transcript_segments — left stale, the next
  // Cut re-ran the whole Whisper pass it had just paid minutes for.
  const refreshJobs = useMemo(
    () => Object.values(activeJobs).filter(
      j => j.inputData?.type === "clip_extraction" || j.inputData?.type === "clip_suggestion"),
    [activeJobs])
  useEffect(() => {
    for (const job of refreshJobs) {
      if ((job.status === "success" || job.status === "failed") && !justCompletedRef.current.has(job.jobId)) {
        justCompletedRef.current.add(job.jobId)
        // Prevent unbounded growth — keep only the last 50 entries
        if (justCompletedRef.current.size > 50) {
          const entries = [...justCompletedRef.current]
          justCompletedRef.current = new Set(entries.slice(-25))
        }
        if (job.status === "success") {
          // Delay slightly so backend has time to persist clips
          setTimeout(() => fetchData(), 1500)
        }
      }
    }
  }, [refreshJobs, fetchData])

  // ── Derived data ─────────────────────────────────────────────
  // Memoized: dragging a bench handle updates range state owned by this page,
  // so every pointermove re-renders ClipStudio — these must not rebuild (and
  // the 120-card filmstrip must not re-sort) per mouse event.
  const clipCountBySource = useMemo(() => {
    const counts = {}
    clips.forEach(c => {
      const sid = c.source_downloaded_video_id
      if (sid) counts[sid] = (counts[sid] || 0) + 1
    })
    return counts
  }, [clips])

  // The source the bench works on. "all" is a review filter, not a video,
  // so it has no bench. `sources` is already filtered to rows whose file
  // exists on disk — the bench can't scrub what isn't there.
  const benchSource = (selectedSourceId && selectedSourceId !== "all")
    ? sources.find(v => v.id === selectedSourceId) || null
    : null

  const onBench = !!benchSource && centerMode === "bench"

  // The bench's pending cuts are owned HERE, like clipSettings above, because
  // the button that spends them lives in the page hero next to Auto-cut —
  // the app's convention is that the primary action sits top-right, and Cut
  // is the primary action of this page. The bench renders the timeline and
  // the rail off this same object, so there is still exactly one list.
  const benchRanges = useBenchRanges(
    benchSource?.id || null, benchSource?.duration_seconds || 0)
  const sourceRail = useResizableWidth(SOURCE_RAIL_WIDTH)

  // A source shorter than one clip can't be cut at all.
  const benchTooShort = !!benchSource
    && (benchSource.duration_seconds || 0) > 0
    && (benchSource.duration_seconds || 0) < MIN_LEN_SEC

  const cutCount = benchRanges.ranges.length


  // Only show clips when a source is selected (or "all" explicitly chosen)
  const clipStripRef = useRef(null)
  const filteredClips = useMemo(
    () => visibleClips(clips, { sourceId: selectedSourceId, query: searchQuery, sortBy }),
    [clips, selectedSourceId, searchQuery, sortBy])

  // ── Handlers ─────────────────────────────────────────────────

  const handleExtract = async (videoId, opts) => {
    setExtracting(true)
    try {
      const isManual = opts.mode === "manual"
      // Shared options come through in both modes; AI-only knobs
      // (min/max duration, user_query, target_platform, genre) are
      // skipped in manual mode because the backend ignores them there
      // and sending unused fields just noises up the request shape.
      const payload = { caption_style: opts.caption_style }
      if (opts.force_retranscribe) {
        payload.whisper_quality = opts.whisper_quality
        payload.force_retranscribe = true
      }
      // remove_silence applies in BOTH modes. It was gated out of manual
      // mode on the theory that trimming would "shift the user's chosen
      // timing" — but the backend removes silence INSIDE each already-cut
      // clip (extract → desilence, see _process_clips_parallel), so the
      // picked range is untouched; the clip just gets tighter. The gate
      // only made hand-picked clips the one place pacing couldn't be fixed.
      if (opts.remove_silence) payload.remove_silence = true
      if (opts.force_vertical) payload.force_vertical = true
      if (opts.emoji_style && opts.emoji_style !== "moderate") payload.emoji_style = opts.emoji_style

      if (isManual) {
        payload.mode = "manual"
        payload.time_ranges = opts.time_ranges
      } else {
        if (opts.min_duration) payload.min_duration = opts.min_duration
        if (opts.max_duration) payload.max_duration = opts.max_duration
        // Cap on clip count (backend clamps 1..99, auto-scales down when the
        // content can't support that many; omitted = auto ~1 per 30s).
        if (opts.max_clips) payload.max_clips = opts.max_clips
        if (opts.user_query && opts.user_query.trim()) payload.user_query = opts.user_query.trim()
        if (opts.target_platform) payload.target_platform = opts.target_platform
        if (opts.genre) payload.genre = opts.genre
      }

      await http.post(`/api/downloaded/${videoId}/extract-clips`, payload)
      showSnackbar(
        isManual
          ? `Cutting ${opts.time_ranges?.length || "your"} clip${opts.time_ranges?.length !== 1 ? "s" : ""} at the times you picked`
          : "Extracting viral clips — AI will find the best moments",
        "success",
      )
      return true
    } catch (e) {
      showSnackbar(`Extract failed: ${e.response?.data?.detail || e.message}`, "error")
      return false
    } finally {
      setExtracting(false)
    }
  }

  /* The bench's one act that produces files. Lives here rather than in
     SourceBench because its button is in the hero — see benchRanges above. */
  const doCut = async () => {
    if (!benchSource || !cutCount || extracting || clipJobRunning) return
    const ok = await handleExtract(benchSource.id, {
      ...clipSettings.toPayload({ hasTranscript: !!benchSource.has_transcript_segments }),
      mode: "manual",
      time_ranges: benchRanges.ranges.map(({ start, end }) => ({ start, end })),
    })
    // Spent ranges must not stay armed. The button re-enables the moment the
    // job finishes, and it re-rendered the SAME "Cut N clips" over the SAME
    // blocks — a second click re-cut clips the user already had. (The ghost lane draws them, but nothing stopped the cut.)
    // Only on success: a failed submit should leave the user's work intact so
    // they can fix the problem and retry.
    if (ok) benchRanges.clear()
  }

  const handleDelete = async () => {
    if (!selectedClip) return
    try {
      await http.delete(`/api/videos/${selectedClip.id}`)
      showSnackbar("Clip deleted", "success")
      setClips(prev => prev.filter(c => c.id !== selectedClip.id))
      setSelectedClip(null)
    } catch (e) {
      showSnackbar(`Delete failed: ${e.response?.data?.detail || e.message}`, "error")
    }
  }

  // The list row carries no `script` (payload weight), so the inspector's
  // Transcript section rendered for no clip, ever. Fetch the detail row once
  // per selected clip and merge; the guard set stops a null script refetching.
  const scriptFetchedRef = useRef(new Set())
  useEffect(() => {
    const id = selectedClip?.id
    if (!id || selectedClip.script !== undefined || scriptFetchedRef.current.has(id)) return
    scriptFetchedRef.current.add(id)
    let alive = true
    http.get(`/api/videos/${id}`).then(({ data }) => {
      if (!alive || !data) return
      const patch = { script: data.script ?? null }
      setSelectedClip(prev => (prev?.id === id ? { ...prev, ...patch } : prev))
      setClips(prev => prev.map(c => (c.id === id ? { ...c, ...patch } : c)))
    }).catch(() => { /* detail fetch is best-effort; the section just stays hidden */ })
    return () => { alive = false }
  }, [selectedClip])

  const handleRegenThumbnail = async () => {
    if (!selectedClip) return
    setRegenThumb(true)
    try {
      const res = await http.post(`/api/videos/${selectedClip.id}/regenerate-thumbnail`)
      showSnackbar("Thumbnail regenerated!", "success")
      // thumb_v busts the immutable thumbnail cache — the URL is the version
      // signal (?v=), and without a new value the regenerated image never
      // appears: same URL, so React doesn't reload the <img> and the browser
      // serves the cached bytes.
      const thumbV = Date.now()
      setSelectedClip(prev => ({ ...prev, thumbnail_path: res.data.thumbnail_path, thumb_v: thumbV }))
      setClips(prev => prev.map(c => c.id === selectedClip.id ? { ...c, thumbnail_path: res.data.thumbnail_path, thumb_v: thumbV } : c))
    } catch (e) {
      showSnackbar(`Thumbnail regen failed: ${e.response?.data?.detail || e.message}`, "error")
    } finally {
      setRegenThumb(false)
    }
  }

  const toggleClipCheck = (clipId) => {
    setCheckedClips(prev => {
      const next = new Set(prev)
      if (next.has(clipId)) next.delete(clipId)
      else next.add(clipId)
      return next
    })
  }

  // Save the checked clips into a folder the user picks (ported from the hosted repo, 2026-11-12):
  // a ZIP download always lands in the browser's download folder. The native picker blocks until
  // the user chooses, so it must not ride the global axios timeout (the backend bounds it at 300 s).
  const handleExportClips = async () => {
    if (checkedClips.size === 0) return
    setExporting(true)
    try {
      const { data: pick } = await http.post("/api/videos/pick-folder", null, { timeout: 0 })
      if (!pick.ok || !pick.path) {
        // A cancel is silent; a picker FAILURE must not be (it reads as "the button does nothing").
        if (pick.error) showSnackbar(`Couldn't open the folder picker: ${pick.error}`, "error")
        return
      }
      const { data } = await http.post("/api/videos/save-clips", {
        clip_ids: [...checkedClips],
        destination: pick.path,
      })
      showSnackbar(`Saved ${data.count} clip${data.count !== 1 ? "s" : ""} to ${pick.path}`, "success")
      setCheckedClips(new Set())
    } catch (e) {
      showSnackbar(`Save failed: ${e.response?.data?.detail || e.message}`, "error")
    } finally {
      setExporting(false)
    }
  }

  const handleZipChecked = async () => {
    if (checkedClips.size === 0) return
    if (checkedClips.size > 50) {
      showSnackbar("Select at most 50 clips for a ZIP download", "warning")
      return
    }
    setZipping(true)
    try {
      const res = await http.post("/api/videos/bulk-export", {
        video_ids: [...checkedClips],
      }, { responseType: "blob" })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement("a")
      a.href = url
      a.download = `viralmint-clips-${checkedClips.size}.zip`
      a.click()
      URL.revokeObjectURL(url)
      showSnackbar(`Downloaded ${checkedClips.size} clip${checkedClips.size !== 1 ? "s" : ""} as ZIP`, "success")
    } catch (e) {
      showSnackbar(`ZIP download failed: ${e.response?.data?.detail || e.message}`, "error")
    } finally {
      setZipping(false)
    }
  }

  const handleDeleteChecked = async () => {
    if (checkedClips.size === 0) return
    setDeleting(true)
    try {
      const { data } = await http.post("/api/videos/bulk-delete", {
        clip_ids: [...checkedClips],
      })
      showSnackbar(`Deleted ${data.deleted} clip${data.deleted !== 1 ? "s" : ""}`, "success")
      // Remove deleted clips from local state
      setClips(prev => prev.filter(c => !checkedClips.has(c.id)))
      if (selectedClip && checkedClips.has(selectedClip.id)) setSelectedClip(null)
      setCheckedClips(new Set())
    } catch (e) {
      showSnackbar(`Delete failed: ${e.response?.data?.detail || e.message}`, "error")
    } finally {
      setDeleting(false)
    }
  }

  const startEditing = () => {
    if (!selectedClip) return
    setEditDraft({
      title: selectedClip.title || "",
      youtube_title: selectedClip.youtube_title || "",
      youtube_description: selectedClip.youtube_description || "",
      youtube_tags: (selectedClip.youtube_tags || []).join(", "),
      tiktok_title: selectedClip.tiktok_title || "",
    })
    setEditing(true)
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const res = await http.patch(`/api/videos/${selectedClip.id}`, editDraft)
      showSnackbar("Clip metadata updated", "success")
      const updated = { ...selectedClip, ...res.data, youtube_tags: res.data.youtube_tags }
      setSelectedClip(updated)
      setClips(prev => prev.map(c => c.id === updated.id ? { ...c, ...updated } : c))
      setEditing(false)
    } catch (e) {
      showSnackbar(`Save failed: ${e.response?.data?.detail || e.message}`, "error")
    } finally {
      setSaving(false)
    }
  }

  const selectAllClips = () => {
    setSelectedSourceId("all")
    // "All clips" is a review view — there is no single source to
    // put on the bench, so it always shows the clip inspector.
    setCenterMode("clip")
    // Open on the strip's FIRST card (its sort + search), which
    // is then highlighted — not clips[0], the unsorted fetch order.
    const first = visibleClips(clips, { sourceId: "all", query: searchQuery, sortBy })[0]
    if (first) {
      setSelectedClip(first)
      requestAnimationFrame(() => clipStripRef.current?.scrollTo?.({ left: 0, behavior: "smooth" }))
    }
  }

  // ── Render ───────────────────────────────────────────────────

  if (loading) return <AppSkeleton variant="studio" />

  return (
    <Box ref={pageRef} sx={{ height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" }}>

      <PageHero
        icon={<ContentCutOutlinedIcon sx={{ fontSize: 22 }} />}
        title="Clipper"
        dense={compactHero}
        // "118 clips · 37 source videos" — not "from 37 videos": clips can
        // come from sources no longer on the rail, so the rail's per-card
        // counts needn't add up to it.
        subtitle={`${clipTotal ?? clips.length} clip${(clipTotal ?? clips.length) !== 1 ? "s" : ""} · ${sources.length} source video${sources.length !== 1 ? "s" : ""}${clipTotal > clips.length ? ` · showing latest ${clips.length}` : ""}`}
        actions={
          // One row, always. At a 1024px window the five controls took the
          // row and squeezed the title column to one word per line (hero
          // ~190px tall, "<$0…" / "$…" chips). Narrow, search folds to an
          // icon that opens the field and Import drops its label — the hero
          // stays one short band and the chips keep their width.
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="nowrap" useFlexGap>
            {compactHero && !searchOpen && !searchQuery ? (
              <Tooltip title="Search clips">
                <IconButton size="small" aria-label="Search clips" onClick={() => setSearchOpen(true)}
                  sx={{ border: 1, borderColor: "divider", borderRadius: 1.5, width: 36, height: 36 }}>
                  <SearchIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            ) : (
              <TextField
                placeholder="Search clips..."
                size="small"
                value={searchQuery}
                autoFocus={compactHero && searchOpen}
                onChange={e => setSearchQuery(e.target.value)}
                onBlur={() => setSearchOpen(false)}
                onKeyDown={e => { if (e.key === "Escape") { setSearchQuery(""); setSearchOpen(false) } }}
                slotProps={{
                  input: { startAdornment: <SearchIcon sx={{ mr: 0.5, fontSize: 18, color: "text.secondary" }} /> },
                  htmlInput: { "aria-label": "Search clips" },
                }}
                sx={{ width: compactHero ? 170 : 200 }}
              />
            )}
            <Tooltip title="Open clips folder">
              <Button size="small" variant="outlined" sx={{ minWidth: 0, px: 1 }}
                onClick={() => http.post("/api/settings/open-folder", { folder: "generated" }).catch(() => showSnackbar("Could not open folder", "error"))}>
                <FolderOpenIcon fontSize="small" />
              </Button>
            </Tooltip>
            <Tooltip title="Refresh sources & clips">
              <Button size="small" variant="outlined"
                aria-label="Refresh sources and clips"
                onClick={() => fetchData()}
                startIcon={<RefreshIcon fontSize="small" />}
                sx={{ whiteSpace: "nowrap", ...(compactHero && { minWidth: 0, px: 1, "& .MuiButton-startIcon": { mr: 0 } }) }}>
                {compactHero ? null : "Refresh"}
              </Button>
            </Tooltip>
            <input
              ref={fileInputRef}
              type="file"
              hidden
              accept="video/*,.mp4,.mov,.avi,.mkv,.webm"
              onChange={handleImportVideo}
            />
            <Tooltip title="Bring a video file in to clip (mp4, mov, mkv, webm)">
              <span>
                <Button
                  // Outlined, not contained: Cut and Auto-cut are the two
                  // primary acts and deserve the weight. Three contained buttons
                  // in one row left the hero with no focal point.
                  variant="outlined" color="secondary" size="small"
                  disabled={importing}
                  onClick={() => fileInputRef.current?.click()}
                  startIcon={importing
                    ? <CircularProgress size={14} color="inherit" />
                    : <FileUploadIcon fontSize="small" />}
                  aria-label={compactHero ? (importing ? "Importing…" : "Import") : undefined}
                  sx={{ whiteSpace: "nowrap", ...(compactHero && { minWidth: 0, px: 1, "& .MuiButton-startIcon": { mr: 0 } }) }}
                >
                  {compactHero ? null : importing ? "Importing…" : "Import"}
                </Button>
              </span>
            </Tooltip>
            {/* Primary action lives top-right (app convention), shown once a
                specific source is selected — mirrors the old filmstrip-toolbar
                button but in the conventional corner.

                `describeChild` on the tooltip: without it MUI promotes the
                title to the button's accessible NAME, so this announced
                itself as a paragraph about Ask AI rather than as "Auto-cut"
                (and collided with the bench's actual Ask AI button). */}
            {/* Cut, then Auto-cut. Same treatment — they are two ways of
                doing one thing and neither is a lesser button — so the ICON
                carries the difference: scissors for the cuts you chose, a
                wand for the ones the AI chooses. They used to share the
                scissors, which is what made them read as duplicates. */}
            {onBench && (
              <Tooltip describeChild title={benchTooShort
                ? "This video is shorter than one second — there is nothing to cut"
                : cutCount
                  ? "Cut exactly the ranges on the timeline"
                  : "Drag on the filmstrip, press N, or Ask AI to add a range first"}>
                <span>
                  <Button
                    variant="contained" color="primary"
                    disabled={!cutCount || extracting || clipJobRunning || benchTooShort}
                    startIcon={(extracting || clipJobRunning)
                      ? <CircularProgress size={14} color="inherit" />
                      : <ContentCutIcon fontSize="small" />}
                    onClick={doCut}
                    sx={{ whiteSpace: "nowrap", fontWeight: 700, ...DISABLED_CONTAINED }}
                  >
                    {clipJobRunning
                      ? "Cutting…"
                      : cutCount
                        ? `Cut ${cutCount} clip${cutCount !== 1 ? "s" : ""}`
                        : "Cut clips"}
                  </Button>
                </span>
              </Tooltip>
            )}
            {selectedSourceId && selectedSourceId !== "all" && (
              <Tooltip describeChild title="AI picks the moments and cuts them straight away — no review step, any number of clips. To see the picks first and adjust them, use Ask AI on the bench.">
                <Button
                  variant="contained" color="primary"
                  disabled={extracting || clipJobRunning}
                  startIcon={(extracting || clipJobRunning)
                    ? <CircularProgress size={14} color="inherit" />
                    : <AutoFixHighIcon fontSize="small" />}
                  onClick={() => {
                    const src = sources.find(s => s.id === selectedSourceId)
                    if (src) { setExtractTarget(src); setExtractDialogOpen(true) }
                  }}
                  sx={{ whiteSpace: "nowrap", ...DISABLED_CONTAINED }}>
                  {clipJobRunning ? "Cutting…" : "Auto-cut"}
                </Button>
              </Tooltip>
            )}
          </Stack>
        }
      />

      {/* ── Active jobs progress ─────────────────────────────── */}
      <ActiveJobsBanner filter={clipJobFilter} fallbackLabel="Extracting clips…" />

      {/* ── Main Layout ─────────────────────────────────────── */}
      <Box sx={{ flex: 1, display: "flex", overflow: "hidden" }}>

        {/* ── Left: Source Videos ──────────────────────────────
            Drag its right edge to resize; the width is remembered. It was a
            fixed 200px while every other column in the app (Smart Video's
            media bin and Project panel) gives — so long source titles
            ellipsised with no way out. Same hook, same handle, same
            arrow-key affordance. */}
        <Box sx={{
          width: sourceRail.width, flexShrink: 0, position: "relative",
          borderRight: 1, borderColor: "divider", display: "flex", minHeight: 0,
        }}>
          {/* The handle sits on this NON-scrolling wrapper. On the scroll box
              itself an absolutely-positioned strip at right:-3 is clipped by
              the overflow and rides the scroll. */}
          <Box
            {...sourceRail.handleProps}
            sx={(t) => ({
              position: "absolute", right: -3, top: 0, bottom: 0, width: 6, zIndex: 2,
              cursor: "col-resize", touchAction: "none",
              bgcolor: sourceRail.dragging ? alpha(t.palette.primary.main, 0.5) : "transparent",
              transition: "background-color .12s",
              "&:hover, &:focus-visible": { bgcolor: alpha(t.palette.primary.main, 0.35), outline: "none" },
            })}
          />
          <Box sx={{
            flex: 1, minWidth: 0, overflow: "auto", ...scrollOnHover,
            p: 1.5, display: "flex", flexDirection: "column", gap: 0.5,
          }}>
          <Typography variant="overline" sx={{ color: "text.secondary", px: 0.5, fontSize: "0.65rem" }}>
            Source Videos
          </Typography>

          <TextField
            size="small"
            placeholder="Filter..."
            value={sourceFilter}
            onChange={e => setSourceFilter(e.target.value)}
            slotProps={{ input: { startAdornment: <SearchIcon sx={{ fontSize: 14, color: "text.disabled", mr: 0.5 }} /> } }}
            sx={{ "& .MuiInputBase-root": { fontSize: "0.75rem", height: 28, px: 0.5 } }}
          />

          {/* "All" filter */}
          <GlassCard
            onClick={selectAllClips}
            selected={selectedSourceId === "all"}
            sx={{ p: 1, cursor: "pointer", borderRadius: 2 }}
          >
            <Stack direction="row" spacing={1} alignItems="center">
              <ContentCutIcon sx={{ fontSize: 16, color: "primary.main" }} />
              <Typography variant="body2" sx={{ fontWeight: 600, fontSize: "0.8rem" }}>
                All Clips ({clips.length})
              </Typography>
            </Stack>
          </GlassCard>

          <Divider sx={{ my: 0.5 }} />

          {/* The weekly loop (2026-10-15): each connected channel's newest
              upload, one click from the bench. Downloads through the same
              batch-download door as the Channels page; nothing billed. */}
          <ChannelNewest
            sources={sources}
            onRefresh={fetchData}
            onOpenSource={(id) => {
              setSelectedSourceId(id)
              setCheckedClips(new Set())
              setCenterMode("bench")
            }}
          />

          {(() => {
            const filtered = sources.filter(v => !sourceFilter || (v.title || "").toLowerCase().includes(sourceFilter.toLowerCase()))
            const isFiltering = !!sourceFilter
            const visible = isFiltering ? filtered : filtered.slice(0, sourceVisibleCount)
            const hiddenCount = filtered.length - visible.length
            return (
              <>
                {visible.map(v => (
                  <SourceVideoCard
                    key={v.id}
                    video={v}
                    clipCount={clipCountBySource[v.id] || 0}
                    isSelected={selectedSourceId === v.id}
                    onPreview={setPreviewVideo}
                    onClick={() => {
                      const newId = selectedSourceId === v.id ? null : v.id
                      setSelectedSourceId(newId)
                      // Selection belongs to the view: carrying checks across
                      // a source switch let "Delete N" act on clips that were
                      // no longer on screen.
                      setCheckedClips(new Set())
                      // Picking a source means "I want to work on this
                      // video", so land on the bench — not on whichever
                      // clip happens to already exist, and never on the
                      // blank panel that used to appear when none did.
                      setCenterMode(newId ? "bench" : "clip")
                      if (newId) {
                        const sourceClips = clips.filter(c => c.source_downloaded_video_id === newId)
                        setSelectedClip(sourceClips[0] || null)
                      } else {
                        setSelectedClip(null)
                      }
                    }}
                  />
                ))}
                {hiddenCount > 0 && (
                  <Button
                    size="small"
                    onClick={() => setSourceVisibleCount(prev => prev + SOURCE_LOAD_MORE)}
                    sx={{ fontSize: "0.7rem", textTransform: "none", mt: 0.5, color: "text.secondary" }}
                  >
                    Show more ({hiddenCount} remaining)
                  </Button>
                )}
              </>
            )
          })()}

          {/* Sources whose file is gone — listed (a read never deletes a row),
              collapsed by default, never openable on the bench. */}
          {missingSources.length > 0 && (
            <Box sx={{ mt: 1 }}>
              <Button size="small" fullWidth onClick={() => setMissingOpen(o => !o)}
                aria-expanded={missingOpen}
                endIcon={<ExpandMoreIcon sx={{ transform: missingOpen ? "rotate(180deg)" : "none", transition: "transform .15s" }} />}
                startIcon={<WarningAmberIcon sx={{ color: "warning.main" }} />}
                sx={{ justifyContent: "flex-start", textTransform: "none", fontSize: "0.72rem", color: "text.secondary", px: 0.5 }}>
                File missing ({missingSources.length})
              </Button>
              {missingOpen && (
                <Stack spacing={0.5} sx={{ mt: 0.5 }}>
                  {missingSources.map(v => (
                    <MissingSourceRow
                      key={v.id}
                      video={v}
                      busy={missingBusy === v.id}
                      onRemove={removeMissing}
                      onRedownload={/^https?:\/\//i.test(v.source_url || "") ? redownloadMissing : null}
                    />
                  ))}
                </Stack>
              )}
            </Box>
          )}

          {/* Only when the centre ISN'T already saying it: with no clips AND no
              sources the centre's empty state carries the same sentence plus
              the two buttons, and the panel repeated it word for word. */}
          {sources.length === 0 && clips.length > 0 && (
            <Box sx={{ p: 2, textAlign: "center" }}>
              <Typography variant="caption" sx={{ color: "text.secondary" }}>
                No source videos yet. Download a video or import one.
              </Typography>
            </Box>
          )}
          </Box>
        </Box>

        {/* ── Center: Cutting bench | Clip inspector ─────────── */}
        <Box sx={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>

          {/* Mode switch — only meaningful when both surfaces have
              something to show, so it stays hidden until they do. */}
          {benchSource && selectedClip && (
            <Stack direction="row" alignItems="center" spacing={1}
              sx={{ px: 2, pt: 1.5, pb: 0.5, flexShrink: 0 }}>
              <ToggleButtonGroup
                size="small" exclusive value={centerMode}
                onChange={(_, v) => v && setCenterMode(v)}
                sx={{ "& .MuiToggleButton-root": { textTransform: "none", py: 0.25, px: 1.25, fontSize: "0.75rem", fontWeight: 700 } }}
              >
                <ToggleButton value="bench">
                  <MovieFilterOutlinedIcon sx={{ fontSize: 15, mr: 0.5 }} />Bench
                </ToggleButton>
                <ToggleButton value="clip">
                  <SlideshowOutlinedIcon sx={{ fontSize: 15, mr: 0.5 }} />Clip
                </ToggleButton>
              </ToggleButtonGroup>
              <Typography variant="caption" sx={{ color: "text.secondary" }}>
                {centerMode === "bench"
                  ? "Cut new clips from the source"
                  : "Review and edit a finished clip"}
              </Typography>
            </Stack>
          )}

          {benchSource && centerMode === "bench" ? (
            <SourceBench
              key={benchSource.id}
              source={benchSource}
              ranges={benchRanges}
              existingClips={clips.filter(c => c.source_downloaded_video_id === benchSource.id)}
              settings={clipSettings.settings}
              onSettings={clipSettings.update}
              onOpenDialog={() => { setExtractTarget(benchSource); setExtractDialogOpen(true) }}
              onSourceMissing={onSourceMissing}
            />
          ) : selectedClip ? (
            <Box sx={{ flex: 1, overflow: "auto", p: 3 }}>
              <Box sx={{ display: "flex", gap: 3, maxWidth: 1200, mx: "auto" }}>

                {/* Video Player — the frame is locked to the CLIP's aspect
                    (the row's probed `aspect_ratio`; 9:16 only when unknown) and
                    its height tracks the space the filmstrip leaves: taller when
                    the filmstrip is collapsed, shorter when it's expanded. Width
                    follows the height, so the whole player grows/shrinks. A
                    hardcoded 9:16 here squeezed landscape cuts into a portrait
                    frame — extraction only reframes when "vertical" is on. */}
                {/* Shrinks (the frame caps at 100% and letterboxes) so the
                    details column keeps a readable 300px — a 16:9 clip at full
                    height left the scoreboard ~170px wide. */}
                <Box sx={{ flexShrink: 1, minWidth: 0, alignSelf: "flex-start" }}>
                  {/* The video player frame keeps a solid black surface +
                      crisp 1-px border rather than the glass treatment — a
                      blurred translucent frame around video chrome would
                      bleed the page colors into the clip preview and weaken
                      the "this is the final pixel output" signal. */}
                  <Box
                    sx={(t) => ({
                      borderRadius: 3, overflow: "hidden",
                      bgcolor: "#000", position: "relative",
                      border: 1, borderColor: "divider",
                      // Aspect-locked frame; height driven by the room the
                      // filmstrip leaves (collapsed → taller). Width follows.
                      // Width-led: the frame's width is the target height ×
                      // the clip's aspect, capped at the column — and the
                      // HEIGHT follows the aspect. Height-led, a squeezed
                      // column kept the full height and letterboxed a 16:9
                      // clip inside a portrait-shaped black box.
                      aspectRatio: clipAspectCss(selectedClip.aspect_ratio),
                      width: `calc(${filmstripCollapsed
                        ? "max(280px, min(640px, 100vh - 250px))"
                        : "max(280px, min(560px, 100vh - 470px))"} * (${clipAspectCss(selectedClip.aspect_ratio)}))`,
                      maxWidth: "100%", height: "auto",
                      boxShadow: t.palette.mode === "dark"
                        ? `0 12px 36px ${alpha(t.palette.common.black, 0.45)}`
                        : `0 12px 36px ${alpha(t.palette.common.black, 0.14)}`,
                    })}
                  >
                    <Box
                      ref={videoRef}
                      component="video"
                      controls
                      autoPlay={false}
                      key={selectedClip.id}
                      sx={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
                      src={`/api/videos/${selectedClip.id}/stream`}
                      onError={onClipVideoError}
                    />
                    {clipPlayError && (
                      <Stack role="alert" data-testid="clip-play-error" spacing={1}
                        alignItems="center" justifyContent="center"
                        sx={{ position: "absolute", inset: 0, p: 2, textAlign: "center", bgcolor: "rgba(0,0,0,0.78)", color: "#fff" }}>
                        <WarningAmberIcon sx={{ color: "warning.main" }} />
                        <Typography variant="body2" sx={{ lineHeight: 1.5 }}>{clipPlayError}</Typography>
                      </Stack>
                    )}
                  </Box>

                  {/* Source context */}
                  {selectedClip.clip_start_seconds != null && (
                    <GlassPanel sx={{ mt: 1.5, p: 1.5, borderRadius: 2 }}>
                      <Stack direction="row" spacing={1} alignItems="center">
                        <AccessTimeIcon sx={{ fontSize: 16, color: "text.secondary" }} />
                        <Typography variant="caption" sx={{ color: "text.secondary" }}>
                          {formatTime(selectedClip.clip_start_seconds)} — {formatTime(selectedClip.clip_end_seconds)} in source
                        </Typography>
                      </Stack>
                    </GlassPanel>
                  )}

                  {/* Quick stats */}
                  <Stack direction="row" spacing={1} sx={{ mt: 1.5 }} flexWrap="wrap" useFlexGap>
                    {selectedClip.clip_virality_score != null && (
                      <Chip
                        icon={<WhatshotIcon />}
                        label={`${selectedClip.clip_virality_score.toFixed(1)} — ${viralityLabel(selectedClip.clip_virality_score)}`}
                        size="small" variant="filled"
                        color={viralityColor(selectedClip.clip_virality_score)}
                      />
                    )}
                    {selectedClip.clip_hook_score != null && (
                      <Chip
                        label={`Hook ${selectedClip.clip_hook_score.toFixed(1)}/10`}
                        size="small" variant="outlined"
                        color={selectedClip.clip_hook_score >= 8 ? "success" : selectedClip.clip_hook_score >= 5 ? "warning" : "error"}
                      />
                    )}
                    {hookTypeLabel(selectedClip.clip_hook_type) && (
                      <Chip
                        label={hookTypeLabel(selectedClip.clip_hook_type)}
                        size="small" variant="outlined" color="primary"
                      />
                    )}
                    <Chip label={`${formatTime(selectedClip.duration_seconds)}`} icon={<AccessTimeIcon />} size="small" variant="outlined" />
                    <Chip label={selectedClip.aspect_ratio || "9:16"} size="small" variant="outlined" />
                    {selectedClip.caption_status === "applied" && (
                      <Chip icon={<CheckCircleIcon />} label="Captions" size="small" color="success" variant="outlined" />
                    )}
                    {selectedClip.caption_status === "failed" && (
                      <Chip icon={<WarningAmberIcon />} label="Captions failed" size="small" color="warning" variant="filled" />
                    )}
                    {selectedClip.metadata_status === "fallback" && (
                      <Chip icon={<WarningAmberIcon />} label="AI meta failed" size="small" color="warning" variant="outlined" />
                    )}
                  </Stack>
                </Box>

                {/* Metadata + Actions */}
                <Box sx={{ flex: "1 0 300px", minWidth: 300 }}>
                  {/* Action bar */}
                  <Stack direction="row" spacing={1} sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
                    {!editing ? (
                      <>
                        <Button size="small" variant="outlined" startIcon={<EditIcon />} onClick={startEditing}>
                          Edit
                        </Button>
                        <Button size="small" variant="outlined"
                          startIcon={regenThumb ? <CircularProgress size={14} /> : <PhotoCameraIcon />}
                          disabled={regenThumb} onClick={handleRegenThumbnail}>
                          {regenThumb ? "Generating..." : "Regen Thumbnail"}
                        </Button>
                        <Button size="small" variant="contained" color="error" startIcon={<UploadIcon />}
                          onClick={() => handleUpload("youtube")}>
                          YouTube
                        </Button>
                        <Button size="small" variant="contained" color="info" startIcon={<UploadIcon />}
                          onClick={() => handleUpload("tiktok")}>
                          TikTok
                        </Button>
                        <Button size="small" variant="outlined" color="inherit" startIcon={<DeleteIcon />}
                          onClick={() => openConfirm(
                            "Delete this clip?",
                            "This will permanently remove the clip and its files from disk.",
                            handleDelete,
                          )}>
                          Delete
                        </Button>
                      </>
                    ) : (
                      <>
                        <Button size="small" variant="contained" startIcon={saving ? <CircularProgress size={14} /> : <SaveIcon />}
                          disabled={saving} onClick={handleSave}>
                          Save
                        </Button>
                        <Button size="small" variant="outlined" onClick={() => setEditing(false)} disabled={saving}>
                          Cancel
                        </Button>
                      </>
                    )}
                  </Stack>

                  {editing ? (
                    <Stack spacing={2}>
                      <TextField label="Clip Title" size="small" fullWidth
                        value={editDraft.title} onChange={e => setEditDraft(p => ({ ...p, title: e.target.value }))} />
                      <Divider />
                      <Typography variant="overline" sx={{ color: "text.secondary" }}>YouTube Shorts</Typography>
                      <TextField label="Title" size="small" fullWidth
                        value={editDraft.youtube_title} onChange={e => setEditDraft(p => ({ ...p, youtube_title: e.target.value }))} />
                      <TextField label="Description" size="small" fullWidth multiline minRows={2} maxRows={4}
                        value={editDraft.youtube_description} onChange={e => setEditDraft(p => ({ ...p, youtube_description: e.target.value }))} />
                      <TextField label="Tags (comma-separated)" size="small" fullWidth
                        value={editDraft.youtube_tags} onChange={e => setEditDraft(p => ({ ...p, youtube_tags: e.target.value }))} />
                      <Divider />
                      <Typography variant="overline" sx={{ color: "text.secondary" }}>TikTok</Typography>
                      <TextField label="Caption" size="small" fullWidth
                        value={editDraft.tiktok_title} onChange={e => setEditDraft(p => ({ ...p, tiktok_title: e.target.value }))} />
                    </Stack>
                  ) : (
                    <Stack spacing={2}>
                      {/* Title */}
                      <Box>
                        <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.3 }}>
                          {selectedClip.title}
                        </Typography>
                      </Box>

                      {/* 5-factor virality scoreboard — shows Hook / Flow /
                          Value / Trend / Shareability bars + the AI's
                          1-2 sentence "why" reasoning. Hides itself for
                          legacy clips with neither hook_score nor breakdown. */}
                      <ScoreBreakdownPanel clip={selectedClip} />

                      {/* YouTube */}
                      {selectedClip.youtube_title && (
                        <GlassPanel sx={{ p: 2, borderRadius: 2 }}>
                          <Typography variant="overline" sx={{ color: "error.main", fontWeight: 700, fontSize: "0.65rem" }}>
                            YouTube Shorts
                          </Typography>
                          <Typography variant="body2" sx={{ fontWeight: 600, mt: 0.5 }}>
                            {selectedClip.youtube_title}
                          </Typography>
                          {selectedClip.youtube_description && (
                            <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mt: 0.5, lineHeight: 1.5 }}>
                              {selectedClip.youtube_description}
                            </Typography>
                          )}
                          {selectedClip.youtube_tags?.length > 0 && (
                            <Stack direction="row" spacing={0.5} sx={{ mt: 1 }} flexWrap="wrap" useFlexGap>
                              {selectedClip.youtube_tags.map((tag, i) => (
                                <Chip key={i} label={tag} size="small" variant="outlined" sx={{ height: 20, fontSize: "0.6rem" }} />
                              ))}
                            </Stack>
                          )}
                        </GlassPanel>
                      )}

                      {/* TikTok */}
                      {selectedClip.tiktok_title && (
                        <GlassPanel sx={{ p: 2, borderRadius: 2 }}>
                          <Typography variant="overline" sx={{ color: "info.main", fontWeight: 700, fontSize: "0.65rem" }}>
                            TikTok
                          </Typography>
                          <Typography variant="body2" sx={{ fontWeight: 600, mt: 0.5 }}>
                            {selectedClip.tiktok_title}
                          </Typography>
                        </GlassPanel>
                      )}

                      {/* Transcript */}
                      {selectedClip.script && (
                        <Box>
                          <Typography variant="overline" sx={{ color: "text.secondary", fontSize: "0.65rem" }}>
                            Transcript
                          </Typography>
                          <GlassPanel sx={{
                            p: 1.5, maxHeight: 200, overflowY: "auto",
                            fontSize: "0.8rem", color: "text.secondary", lineHeight: 1.6,
                            whiteSpace: "pre-wrap", borderRadius: 2,
                          }}>
                            {selectedClip.script}
                          </GlassPanel>
                        </Box>
                      )}
                    </Stack>
                  )}
                </Box>
              </Box>
            </Box>
          ) : (
            /* Empty state — guide the user to the next action so they never
               hit a dead end. Three cases:
               - no clips AND no sources → get a source (download / import)
               - no clips but have sources → extract from the first one
               - clips exist, none selected → pick one from the filmstrip */
            <Box sx={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
              {clips.length === 0 && sources.length === 0 ? (
                <EmptyState
                  icon={<ContentCutIcon />}
                  title="No clips yet"
                  description="Clipper turns a full video into viral shorts. Add a video first — download one or import your own — then extract the best moments."
                  primaryAction={{
                    label: "Download a video",
                    startIcon: <CloudDownloadIcon />,
                    onClick: () => navigate("/tools/video-download"),
                  }}
                  secondaryAction={{
                    label: "Import your own",
                    onClick: () => fileInputRef.current?.click(),
                  }}
                />
              ) : clips.length === 0 ? (
                <EmptyState
                  icon={<ContentCutIcon />}
                  title="No clips yet"
                  description="Pick a source video and extract clips — AI finds the most viral moments for you."
                  primaryAction={{
                    label: `Auto-cut from ${sources[0].title?.slice(0, 30) || "first video"}`,
                    startIcon: <ContentCutIcon />,
                    onClick: () => { setExtractTarget(sources[0]); setExtractDialogOpen(true) },
                  }}
                />
              ) : (
                /* Reachable by de-selecting a source (clicking the
                   highlighted card again), so it has to name BOTH exits —
                   it used to mention only the filmstrip, which made it the
                   same dead end the bench was built to remove. */
                <EmptyState
                  icon={<ContentCutIcon />}
                  title="Pick something to work on"
                  description="Choose a source video on the left to open the cutting bench and cut new clips — or click a finished clip in the filmstrip below to preview it."
                  primaryAction={sources.length ? {
                    label: "Open the bench",
                    startIcon: <ContentCutIcon />,
                    onClick: () => { setSelectedSourceId(sources[0].id); setCenterMode("bench") },
                  } : undefined}
                  compact
                />
              )}
            </Box>
          )}

          {/* ── Bottom: Clip Filmstrip ────────────────────────── */}
          <Box sx={{
            flexShrink: 0,
            borderTop: 1, borderColor: "divider",
            bgcolor: (t) => t.palette.mode === "dark" ? "rgba(0,0,0,0.2)" : "rgba(0,0,0,0.02)",
          }}>
            {/* The whole row toggles the filmstrip (not just the button). The
                interactive controls below stopPropagation so they still act on
                their own click. */}
            <Stack direction="row" spacing={0.5} alignItems="center"
              onClick={() => setFilmstripCollapsed(p => !p)}
              sx={{
                px: 2, pt: 1.5, pb: 0.5, cursor: "pointer", userSelect: "none",
                transition: "background-color .12s ease",
                "&:hover": { bgcolor: "action.hover" },
              }}>
              <Tooltip title={filmstripCollapsed ? "Click to expand the filmstrip back to full cards" : "Click to collapse the filmstrip — gives the preview area more room"}>
                <Button
                  size="small"
                  variant="outlined"
                  onClick={(e) => { e.stopPropagation(); setFilmstripCollapsed(p => !p) }}
                  startIcon={filmstripCollapsed
                    ? <UnfoldMoreIcon sx={{ fontSize: 18 }} />
                    : <UnfoldLessIcon sx={{ fontSize: 18 }} />
                  }
                  sx={{
                    flexShrink: 0,
                    height: 28,
                    px: 1.25,
                    fontSize: "0.78rem",
                    fontWeight: 700,
                    textTransform: "none",
                    borderRadius: 2,
                    letterSpacing: 0.2,
                  }}
                >
                  {/* The ACTION, not the state — a button reading "Expanded"
                      that collapses is a coin flip. */}
                  {filmstripCollapsed ? "Expand" : "Collapse"}
                </Button>
              </Tooltip>
              {/* No count before a source is picked: "CLIPS (0)" under a hero
                  that says "118 clips" read as a contradiction. */}
              <Typography variant="overline" sx={{ color: "text.secondary", fontSize: "0.65rem", flexShrink: 0 }}>
                {selectedSourceId ? `Clips (${filteredClips.length})` : "Clips"}
              </Typography>

              {/* The sort lives with the list it sorts. It was in the page
                  hero, three panels away from the only thing it affects —
                  `filteredClips` feeds this strip and nothing else — where it
                  read as a page-level control over the whole Clipper. */}
              {filteredClips.length > 1 && (
                <>
                  <Button
                    size="small" variant="text"
                    onClick={(e) => { e.stopPropagation(); setSortAnchor(e.currentTarget) }}
                    startIcon={<SortIcon sx={{ fontSize: 16 }} />}
                    sx={{
                      flexShrink: 0, height: 24, px: 0.75, ml: 0.5,
                      fontSize: "0.72rem", fontWeight: 700, textTransform: "none",
                      color: "text.secondary",
                    }}
                  >
                    {sortBy === "virality" ? "Top Viral" : sortBy === "newest" ? "Newest" : "Longest"}
                  </Button>
                  <Menu anchorEl={sortAnchor} open={Boolean(sortAnchor)}
                    onClose={() => setSortAnchor(null)}>
                    {[
                      { key: "virality", label: "Top Viral", icon: <WhatshotIcon fontSize="small" /> },
                      { key: "newest", label: "Newest First", icon: <AccessTimeIcon fontSize="small" /> },
                      { key: "duration", label: "Longest First", icon: <AspectRatioIcon fontSize="small" /> },
                    ].map(s => (
                      <MenuItem key={s.key} selected={sortBy === s.key}
                        onClick={() => { setSortBy(s.key); setSortAnchor(null) }}>
                        <ListItemIcon>{s.icon}</ListItemIcon>
                        <ListItemText>{s.label}</ListItemText>
                      </MenuItem>
                    ))}
                  </Menu>
                </>
              )}

              {/* Export controls — shown when clips exist */}
              {filteredClips.length > 0 && (
                <>
                  <Checkbox
                    size="small"
                    checked={filteredClips.length > 0 && filteredClips.every(c => checkedClips.has(c.id))}
                    indeterminate={checkedClips.size > 0 && !filteredClips.every(c => checkedClips.has(c.id))}
                    onClick={e => e.stopPropagation()}
                    onChange={e => {
                      if (e.target.checked) setCheckedClips(new Set(filteredClips.map(c => c.id)))
                      else setCheckedClips(new Set())
                    }}
                    sx={{ p: 0.25, ml: 1 }}
                  />
                  <Typography variant="caption" sx={{ color: "text.secondary", fontSize: "0.7rem", flexShrink: 0 }}>
                    {checkedClips.size > 0 ? `${checkedClips.size} selected` : "Select all"}
                  </Typography>
                  {checkedClips.size > 0 && (
                    <>
                      <Button
                        size="small" variant="contained"
                        startIcon={exporting ? <CircularProgress size={14} color="inherit" /> : <FolderOpenIcon />}
                        disabled={exporting || deleting || zipping}
                        onClick={(e) => { e.stopPropagation(); handleExportClips() }}
                        sx={{ textTransform: "none", height: 28, fontSize: "0.75rem", fontWeight: 700, px: 1.5, borderRadius: 2, ml: 0.5 }}
                      >
                        {exporting ? "Saving..." : `Save ${checkedClips.size} clip${checkedClips.size !== 1 ? "s" : ""}`}
                      </Button>
                      <Button
                        size="small" variant="outlined" color="secondary"
                        startIcon={zipping ? <CircularProgress size={14} color="inherit" /> : <FolderZipIcon />}
                        disabled={zipping || deleting || exporting}
                        onClick={(e) => { e.stopPropagation(); handleZipChecked() }}
                        sx={{ textTransform: "none", height: 28, fontSize: "0.75rem", fontWeight: 700, px: 1.5, borderRadius: 2, ml: 0.5 }}
                      >
                        {zipping ? "Zipping..." : "Download ZIP"}
                      </Button>
                      <Button
                        size="small" variant="outlined" color="error"
                        startIcon={deleting ? <CircularProgress size={14} color="inherit" /> : <DeleteIcon />}
                        disabled={deleting || zipping || exporting}
                        onClick={(e) => { e.stopPropagation(); openConfirm(
                          `Delete ${checkedClips.size} clip${checkedClips.size !== 1 ? "s" : ""}?`,
                          "This will permanently remove the selected clips and their files from disk.",
                          handleDeleteChecked,
                        ) }}
                        sx={{ textTransform: "none", height: 28, fontSize: "0.75rem", fontWeight: 700, px: 1.5, borderRadius: 2 }}
                      >
                        {deleting ? "Deleting..." : `Delete ${checkedClips.size}`}
                      </Button>
                    </>
                  )}
                </>
              )}

              <Box sx={{ flex: 1 }} />
              {/* Extract Clips moved to the PageHero top-right (app convention). */}
            </Stack>
            <Box ref={clipStripRef} sx={(theme) => ({
              display: "flex", flexWrap: "nowrap", alignItems: "flex-start",
              // Tighter padding + gap in collapsed mode — the rows are short
              // enough that anything more wastes vertical space.
              gap: filmstripCollapsed ? 0.75 : 1.5,
              px: 2,
              pb: filmstripCollapsed ? 1 : 2,
              pt: 0.5,
              overflowX: "auto", overflowY: "hidden",
              // A visible, grabbable horizontal scrollbar. macOS users scroll the
              // filmstrip with a trackpad gesture, but mouse users on Windows /
              // Linux need an obvious bar to drag — the old 6px divider-coloured
              // bar was too thin + faint to grab. Firefox uses the scrollbar-*
              // props; Chromium / Safari use the ::-webkit pseudos.
              scrollbarWidth: "thin",
              scrollbarColor: `${theme.palette.action.active} ${theme.palette.action.hover}`,
              "&::-webkit-scrollbar": { height: 12 },
              "&::-webkit-scrollbar-track": {
                backgroundColor: theme.palette.action.hover,
                borderRadius: 6,
              },
              "&::-webkit-scrollbar-thumb": {
                backgroundColor: theme.palette.action.active,
                borderRadius: 6,
                // Transparent border + padding-box insets the thumb from the
                // track edge (clean look) while keeping the full 12px hit area.
                border: "3px solid transparent",
                backgroundClip: "padding-box",
                minWidth: 40,
              },
              "&::-webkit-scrollbar-thumb:hover": {
                backgroundColor: theme.palette.text.secondary,
              },
            })}>
              {filteredClips.length === 0 ? (
                <Stack direction="row" spacing={1} alignItems="center" justifyContent="center"
                  sx={{ py: 3, px: 4, width: "100%" }}>
                  <Typography variant="caption" sx={{ color: "text.secondary" }}>
                    {selectedSourceId === "all"
                      ? (searchQuery ? `No clips match “${searchQuery}”` : "No clips yet")
                      : selectedSourceId
                        ? (searchQuery ? `No clips from this video match “${searchQuery}”` : "No clips from this video yet")
                        : "Pick a source video on the left to see its clips"}
                  </Typography>
                  {!selectedSourceId && clips.length > 0 && (
                    <Button size="small" variant="text"
                      onClick={(e) => { e.stopPropagation(); selectAllClips() }}
                      sx={{ textTransform: "none", fontWeight: 700 }}>
                      Show all {clips.length}
                    </Button>
                  )}
                </Stack>
              ) : (
                filteredClips.map(clip => (
                  <ClipCard
                    key={clip.id}
                    clip={clip}
                    isSelected={selectedClip?.id === clip.id}
                    onClick={() => { setSelectedClip(clip); setCenterMode("clip") }}
                    checked={checkedClips.has(clip.id)}
                    onCheck={toggleClipCheck}
                    collapsed={filmstripCollapsed}
                  />
                ))
              )}
            </Box>
          </Box>
        </Box>
      </Box>

      {/* Extract Dialog */}
      <ExtractDialog
        open={extractDialogOpen}
        onClose={() => setExtractDialogOpen(false)}
        video={extractTarget}
        onExtract={handleExtract}
        settings={clipSettings.settings}
        onSettings={clipSettings.update}
      />

      {/* Source video preview dialog — quick play to see what the
          source is about, without committing to selecting it. */}
      <Dialog
        open={!!previewVideo}
        onClose={() => setPreviewVideo(null)}
        maxWidth={false}
        slotProps={{ paper: { sx: { bgcolor: "#000", width: "auto", maxWidth: "90vw", m: 2 } } }}
      >
        <DialogContent sx={{ p: 0, position: "relative", overflow: "hidden", bgcolor: "#000", lineHeight: 0 }}>
          {previewVideo && (
            <Box component="video"
              src={`/api/downloaded/${previewVideo.id}/stream`}
              controls
              autoPlay
              // The dialog Paper shrinks to the video (width:auto, no breakpoint
              // cap), and the video sizes to its TRUE aspect ratio capped at
              // 90vw / 85vh. So a 9:16 clip gets a narrow portrait player and a
              // 16:9 clip a wide one — no black side-bars, no letterboxing, and
              // (unlike the old viewport-relative 80vw that overflowed the
              // fixed-width dialog) no horizontal scrollbar for either shape.
              sx={{ display: "block", maxWidth: "90vw", maxHeight: "85vh" }}
            />
          )}
          <IconButton
            onClick={() => setPreviewVideo(null)}
            aria-label="Close preview"
            sx={{ position: "absolute", top: 8, right: 8, color: "#fff", bgcolor: "rgba(0,0,0,0.5)", "&:hover": { bgcolor: "rgba(0,0,0,0.7)" } }}
          >
            <CloseIcon />
          </IconButton>
        </DialogContent>
      </Dialog>

      <ConfirmDialog />
    </Box>
  )
}
