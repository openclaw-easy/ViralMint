import { useState, useEffect, useCallback, useMemo, useRef, memo } from "react"
import {
  Box, Typography, Paper, Stack, Chip, IconButton, Button, Divider,
  TextField, Tooltip, CircularProgress, Menu, MenuItem,
  ListItemText, ListItemIcon, Skeleton, Badge, alpha,
  Dialog, DialogContent,
  LinearProgress, ToggleButton, ToggleButtonGroup,
} from "@mui/material"
import ContentCutIcon from "@mui/icons-material/ContentCut"
import AutoFixHighIcon from "@mui/icons-material/AutoFixHighOutlined"
import MovieFilterOutlinedIcon from "@mui/icons-material/MovieFilterOutlined"
import SlideshowOutlinedIcon from "@mui/icons-material/SlideshowOutlined"
import WhatshotIcon from "@mui/icons-material/Whatshot"
import AccessTimeIcon from "@mui/icons-material/AccessTime"
import PlayCircleOutlineIcon from "@mui/icons-material/PlayCircleOutline"
import UploadIcon from "@mui/icons-material/Upload"
import EditIcon from "@mui/icons-material/Edit"
import SaveIcon from "@mui/icons-material/Save"
import DeleteIcon from "@mui/icons-material/Delete"
import PhotoCameraIcon from "@mui/icons-material/PhotoCamera"
import DownloadIcon from "@mui/icons-material/Download"
import AspectRatioIcon from "@mui/icons-material/AspectRatio"
import WarningAmberIcon from "@mui/icons-material/WarningAmber"
import MovieCreationIcon from "@mui/icons-material/MovieCreation"
import SearchIcon from "@mui/icons-material/Search"
import RefreshIcon from "@mui/icons-material/Refresh"
import SortIcon from "@mui/icons-material/Sort"
import CloseIcon from "@mui/icons-material/Close"
import CheckCircleIcon from "@mui/icons-material/CheckCircle"
import VideocamIcon from "@mui/icons-material/Videocam"
import FolderOpenIcon from "@mui/icons-material/FolderOpen"
import AddIcon from "@mui/icons-material/Add"
import http from "../api/http"
import useAppStore from "../store/appStore"
import ActiveJobsBanner from "../components/create/ActiveJobsBanner"
import ExtractDialog from "../components/clip/ExtractDialog"
import SourceBench from "../components/clip/bench/SourceBench"
import useClipSettings from "../components/clip/useClipSettings"
import useBenchRanges, { MIN_LEN_SEC } from "../components/clip/bench/useBenchRanges"
import {
  formatTime, hookTypeLabel, viralityColor, viralityLabel, clipAspectCss,
} from "../components/clip/clipFormat"

// clip_score_breakdown_json arrives as a JSON string ({flow, value, trend,
// shareability} each 1-10). Parse defensively — a legacy clip has no field,
// a malformed one shouldn't throw. Returns the parsed object or null so the
// scoreboard hides itself when there's nothing to show.
function parseScoreBreakdown(clip) {
  const raw = clip?.clip_score_breakdown_json
  if (!raw) return null
  if (typeof raw === "object") return raw
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === "object" ? parsed : null
  } catch {
    return null
  }
}

/* ── Score breakdown scoreboard ─────────────────────────────── */

const SCORE_FACTOR_META = {
  hook:         { label: "Hook",  blurb: "Does the opening 2-3 seconds stop the scroll? Scored on the first sentence only." },
  flow:         { label: "Flow",  blurb: "Logical narrative arc with a satisfying close — no dangling thoughts." },
  value:        { label: "Value", blurb: "Emotional or practical resonance — a payoff, actionable takeaway, or gut reaction." },
  trend:        { label: "Trend", blurb: "Alignment with what audiences are clicking on right now in this niche." },
  shareability: { label: "Share", blurb: "Would a viewer quote this, screenshot it, or send it to a friend?" },
}

function scoreFactorColor(score) {
  if (score == null) return "text.disabled"
  if (score >= 8) return "success.main"
  if (score >= 6) return "warning.main"
  return "text.disabled"
}

function ScoreBar({ factor, score }) {
  const meta = SCORE_FACTOR_META[factor]
  if (!meta) return null
  const pct = score == null ? 0 : Math.max(0, Math.min(score, 10)) * 10
  const color = scoreFactorColor(score)
  return (
    <Tooltip title={meta.blurb} arrow placement="top">
      <Box sx={{ cursor: "default" }}>
        <Stack direction="row" justifyContent="space-between" sx={{ mb: 0.25 }}>
          <Typography variant="caption" sx={{ fontWeight: 600, fontSize: "0.7rem" }}>{meta.label}</Typography>
          <Typography variant="caption" sx={{ fontWeight: 700, fontSize: "0.7rem", color }}>
            {score == null ? "—" : score.toFixed(1)}
          </Typography>
        </Stack>
        <LinearProgress
          variant="determinate"
          value={pct}
          sx={{
            height: 5, borderRadius: 3,
            bgcolor: (t) => t.palette.mode === "dark" ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.06)",
            "& .MuiLinearProgress-bar": { bgcolor: color, borderRadius: 3 },
          }}
        />
      </Box>
    </Tooltip>
  )
}

// 5-factor virality scoreboard (Hook / Flow / Value / Trend / Share) shown in
// the clip detail panel. Renders nothing on legacy clips that have neither a
// hook score nor a parsed breakdown — keeps the panel backward-compatible.
function ScoreBreakdownPanel({ clip }) {
  if (!clip) return null
  const hook = clip.clip_hook_score
  const breakdown = parseScoreBreakdown(clip)
  if (hook == null && !breakdown) return null

  const factors = [
    { key: "hook", score: hook },
    { key: "flow", score: breakdown?.flow },
    { key: "value", score: breakdown?.value },
    { key: "trend", score: breakdown?.trend },
    { key: "shareability", score: breakdown?.shareability },
  ]

  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
        <WhatshotIcon sx={{ fontSize: 16, color: "primary.main" }} />
        <Typography variant="overline" sx={{ color: "text.secondary", fontWeight: 700, fontSize: "0.65rem" }}>
          Virality scoreboard
        </Typography>
        {clip.clip_virality_score != null && (
          <>
            <Box sx={{ flex: 1 }} />
            <Typography variant="caption" sx={{ color: "text.secondary", fontSize: "0.7rem" }}>
              Overall: <strong>{clip.clip_virality_score.toFixed(1)}</strong>/10
            </Typography>
          </>
        )}
      </Stack>
      <Box sx={{
        display: "grid",
        gridTemplateColumns: { xs: "repeat(2, 1fr)", sm: "repeat(3, 1fr)", md: "repeat(5, 1fr)" },
        gap: 1.5,
      }}>
        {factors.map(({ key, score }) => (
          <ScoreBar key={key} factor={key} score={score} />
        ))}
      </Box>
    </Paper>
  )
}

/* ── Source Video Sidebar Item ──────────────────────────────── */

function SourceVideoCard({ video, clipCount, isSelected, onClick, onPreview }) {
  return (
    <Paper
      elevation={0}
      onClick={onClick}
      sx={{
        p: 1.5, cursor: "pointer",
        border: 2,
        borderColor: isSelected ? "primary.main" : "transparent",
        borderRadius: 2.5,
        bgcolor: isSelected ? "action.selected" : "transparent",
        transition: "all 0.2s ease",
        "&:hover": {
          bgcolor: isSelected ? "action.selected" : "action.hover",
          borderColor: isSelected ? "primary.main" : "divider",
        },
      }}
    >
      {/* Thumbnail */}
      <Box sx={{
        width: "100%", aspectRatio: "16/9", borderRadius: 2, overflow: "hidden",
        bgcolor: "action.hover", mb: 1, position: "relative",
      }}>
        {(video.video_path || video.thumbnail_url) ? (
          <Box component="img"
            src={video.video_path ? `/api/downloaded/${video.id}/thumbnail` : video.thumbnail_url}
            alt=""
            loading="lazy" decoding="async"
            sx={{ width: "100%", height: "100%", objectFit: "cover" }}
            onError={e => { e.target.style.display = "none" }} />
        ) : (
          <Box sx={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
            <VideocamIcon sx={{ color: "text.disabled", fontSize: 28 }} />
          </Box>
        )}
        {/* Duration badge */}
        {video.duration_seconds > 0 && (
          <Chip
            label={formatTime(video.duration_seconds)}
            size="small"
            sx={{
              position: "absolute", bottom: 4, right: 4,
              height: 20, fontSize: "0.65rem", fontWeight: 700,
              bgcolor: "rgba(0,0,0,0.75)", color: "#fff",
              "& .MuiChip-label": { px: 0.75 },
            }}
          />
        )}
        {/* Preview button — stopPropagation so it doesn't also select the card.
            Only for locally-downloaded (streamable) sources. */}
        {onPreview && video.video_path && (
          <Tooltip title="Preview video">
            <IconButton
              size="small"
              onClick={(e) => { e.stopPropagation(); onPreview(video) }}
              sx={{
                position: "absolute", top: 4, right: 4,
                bgcolor: "rgba(0,0,0,0.6)", color: "#fff",
                "&:hover": { bgcolor: "rgba(0,0,0,0.8)" }, p: 0.5,
              }}
            >
              <PlayCircleOutlineIcon sx={{ fontSize: 20 }} />
            </IconButton>
          </Tooltip>
        )}
      </Box>

      <Typography variant="body2" sx={{ fontWeight: 600, fontSize: "0.8rem", lineHeight: 1.3 }} noWrap>
        {video.title || "Untitled"}
      </Typography>
      <Stack direction="row" spacing={0.5} alignItems="center" sx={{ mt: 0.5 }}>
        <ContentCutIcon sx={{ fontSize: 13, color: clipCount > 0 ? "primary.main" : "text.disabled" }} />
        <Typography variant="caption" sx={{ color: clipCount > 0 ? "primary.main" : "text.secondary", fontWeight: clipCount > 0 ? 700 : 400 }}>
          {clipCount} clip{clipCount !== 1 ? "s" : ""}
        </Typography>
      </Stack>
    </Paper>
  )
}

/* ── Clip Filmstrip Card ───────────────────────────────────── */

function ClipCardImpl({ clip, isSelected, onSelect }) {
  const score = clip.clip_virality_score
  return (
    <Paper
      elevation={0}
      onClick={() => onSelect(clip)}
      sx={{
        width: 140, minWidth: 140, flexShrink: 0,
        cursor: "pointer",
        border: 2,
        borderColor: isSelected ? "primary.main" : "transparent",
        borderRadius: 2.5,
        overflow: "hidden",
        transition: "all 0.2s ease",
        transform: isSelected ? "translateY(-2px)" : "none",
        boxShadow: isSelected ? (t) => `0 4px 16px ${alpha(t.palette.primary.main, 0.25)}` : "none",
        "&:hover": {
          borderColor: isSelected ? "primary.main" : "divider",
          transform: "translateY(-2px)",
          boxShadow: (t) => `0 4px 12px ${alpha(t.palette.common.black, 0.1)}`,
        },
      }}
    >
      {/* Thumbnail */}
      {/* The frame follows the clip's probed aspect — extraction only reframes
          when "vertical" is on, so a 16:9 cut is a real 16:9 file and a
          hardcoded 9:16 centre-cropped it to a sliver. ?v= is the cache-bust
          signal: without a new value a regenerated thumbnail never appears
          (same URL, so the browser keeps the bytes it has). */}
      <Box sx={{ width: "100%", aspectRatio: clipAspectCss(clip.aspect_ratio), position: "relative", bgcolor: "#000" }}>
        {clip.thumbnail_path ? (
          <Box component="img" src={`/api/videos/${clip.id}/thumbnail?v=${encodeURIComponent(clip.thumb_v || clip.created_at || "1")}`} alt=""
            sx={{ width: "100%", height: "100%", objectFit: "cover" }} />
        ) : (
          <Box sx={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
            <ContentCutIcon sx={{ color: "rgba(255,255,255,0.3)", fontSize: 28 }} />
          </Box>
        )}
        {/* Virality badge */}
        {score != null && (
          <Chip
            icon={<WhatshotIcon sx={{ fontSize: "14px !important" }} />}
            label={score.toFixed(1)}
            size="small"
            color={viralityColor(score)}
            sx={{
              position: "absolute", top: 4, left: 4,
              height: 22, fontWeight: 700, fontSize: "0.7rem",
              "& .MuiChip-icon": { ml: 0.3 },
            }}
          />
        )}
        {/* Duration badge */}
        <Chip
          label={formatTime(clip.duration_seconds)}
          size="small"
          sx={{
            position: "absolute", bottom: 4, right: 4,
            height: 18, fontSize: "0.6rem", fontWeight: 700,
            bgcolor: "rgba(0,0,0,0.75)", color: "#fff",
            "& .MuiChip-label": { px: 0.5 },
          }}
        />
        {/* Caption warning */}
        {clip.caption_status === "failed" && (
          <Tooltip title="Captions failed to apply">
            <WarningAmberIcon sx={{ position: "absolute", top: 4, right: 4, fontSize: 18, color: "warning.main" }} />
          </Tooltip>
        )}
        {/* Play overlay */}
        <Box sx={{
          position: "absolute", inset: 0,
          display: "flex", alignItems: "center", justifyContent: "center",
          opacity: 0, transition: "opacity 0.2s",
          bgcolor: "rgba(0,0,0,0.3)",
          "&:hover": { opacity: 1 },
        }}>
          <PlayCircleOutlineIcon sx={{ fontSize: 36, color: "#fff" }} />
        </Box>
      </Box>

      {/* Title */}
      <Box sx={{ p: 1 }}>
        <Typography variant="caption" sx={{ fontWeight: 600, fontSize: "0.7rem", lineHeight: 1.2, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
          {clip.title || "Untitled Clip"}
        </Typography>
      </Box>
    </Paper>
  )
}

/* ══════════════════════════════════════════════════════════════
   MAIN COMPONENT: Clip Studio
   ══════════════════════════════════════════════════════════════ */

// Memoized: the page re-renders per pointermove while a bench handle drags,
// and a filmstrip of these re-rendering per mouse event is the difference
// between a smooth drag and a stutter. Props are primitives plus a stable
// setter — a fresh `onClick` arrow per render would defeat the memo, which
// is why the card takes `onSelect(clip)` instead.
const ClipCard = memo(ClipCardImpl)

export default function ClipStudio() {
  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const activeJobs = useAppStore((s) => s.activeJobs)

  // Track clip extraction jobs (restored from API on page load via useWebSocket)
  const isClipJob = (j) =>
    (j.message && j.message.toLowerCase().includes("clip"))
    || (j.inputData && j.inputData.type === "clip_extraction")
  const clipJobs = Object.values(activeJobs).filter(isClipJob)
  const clipJobFilter = (j) => j.status === "running" && isClipJob(j)
  // Both Cut buttons disable while a cut is in flight. A suggestion search is
  // NOT a cut, so it deliberately does not gate them — the bench stays usable
  // while the AI reads.
  const clipJobRunning = clipJobs.some(clipJobFilter)
  const justCompletedRef = useRef(new Set())

  // Data
  const [sources, setSources] = useState([])
  const [clips, setClips] = useState([])
  // The list endpoint pages; `total` says how many exist so the header
  // doesn't silently undercount a heavy user's library.
  const [clipTotal, setClipTotal] = useState(null)
  const [loading, setLoading] = useState(true)
  const [selectedSourceId, setSelectedSourceId] = useState(null) // "all" or video id
  const [selectedClip, setSelectedClip] = useState(null)
  const [sourceFilter, setSourceFilter] = useState("")
  const [previewVideo, setPreviewVideo] = useState(null) // source video for the play popup
  const [searchQuery, setSearchQuery] = useState("")
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

  // Which surface the centre column shows.
  //   "bench" — work ON the selected source: scrub it, drag ranges over a
  //             filmstrip, watch the IN/OUT frames, then cut.
  //   "clip"  — inspect one finished clip (the original behaviour).
  // Selecting a source used to leave the centre blank whenever that source had
  // no clips yet, which is exactly the moment a workspace is most useful.
  const [centerMode, setCenterMode] = useState("bench")

  // Per-clip settings (captions / emoji / silence / vertical / transcription)
  // are owned HERE and handed to both surfaces. They used to be duplicated
  // inside the dialog and would have been duplicated again in the bench, so a
  // caption style picked in one did nothing in the other and whichever surface
  // you happened to finish in decided the render.
  const clipSettings = useClipSettings()

  // Video player ref
  const videoRef = useRef(null)

  // Pause every player on the page whenever the source-video preview opens,
  // so two videos aren't playing — and both audible — at the same time. The
  // bench player lives inside SourceBench, so a ref to the inspector alone
  // missed it and the bench kept talking under the preview.
  useEffect(() => {
    if (previewVideo) document.querySelectorAll("video").forEach(v => { try { v.pause() } catch { /* detached */ } })
  }, [previewVideo])

  // ── Load data ────────────────────────────────────────────────
  const fetchData = useCallback(async () => {
    try {
      const [srcRes, clipRes] = await Promise.all([
        http.get("/api/downloaded", { params: { limit: 200 } }),
        // Filter server-side: an unfiltered page of 100 could be entirely
        // recent non-clip rows, rendering "No clips yet" over a library full
        // of clips.
        http.get("/api/videos", { params: { limit: 100, source_type: "clip_extraction" } }),
      ])
      // Show all downloaded videos (sorted longest first — best for clipping)
      const downloadedVideos = (srcRes.data?.videos || srcRes.data || [])
        .sort((a, b) => (b.duration_seconds || 0) - (a.duration_seconds || 0))
      setSources(downloadedVideos)

      // Only show clip_extraction videos
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

  // Auto-refresh when a clip extraction OR suggestion job completes. The
  // suggestion half matters for a non-obvious reason: Ask AI may run Whisper
  // and back-fill the source row's transcript, and useClipSettings derives
  // force_retranscribe from has_transcript_segments — left stale, the next
  // Cut re-ran the whole Whisper pass Ask AI had just spent minutes on.
  const refreshJobs = useMemo(
    () => Object.values(activeJobs).filter(
      j => isClipJob(j) || j.inputData?.type === "clip_suggestion"),
    [activeJobs])  // eslint-disable-line react-hooks/exhaustive-deps
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
  // the filmstrip must not re-sort) per mouse event.
  const clipCountBySource = useMemo(() => {
    const counts = {}
    clips.forEach(c => {
      const sid = c.source_downloaded_video_id
      if (sid) counts[sid] = (counts[sid] || 0) + 1
    })
    return counts
  }, [clips])

  // The source the bench works on. "all" is a review filter, not a video, so
  // it has no bench.
  const benchSource = (selectedSourceId && selectedSourceId !== "all")
    ? sources.find(v => v.id === selectedSourceId) || null
    : null

  const onBench = !!benchSource && centerMode === "bench"

  // The bench's pending cuts are owned HERE, like clipSettings above, because
  // the button that spends them lives in the page hero next to Auto-cut — the
  // app's convention is that the primary action sits top-right, and Cut is the
  // primary action of this page. The bench renders the timeline and the rail
  // off this same object, so there is still exactly one list.
  const benchRanges = useBenchRanges(
    benchSource?.id || null, benchSource?.duration_seconds || 0)

  // A source shorter than one clip can't be cut at all.
  const benchTooShort = !!benchSource
    && (benchSource.duration_seconds || 0) > 0
    && (benchSource.duration_seconds || 0) < MIN_LEN_SEC

  const cutCount = benchRanges.ranges.length

  // Only show clips when a source is selected (or "all" explicitly chosen)
  const showAllClips = selectedSourceId === "all"
  const filteredClips = useMemo(() => clips
    .filter(c => {
      if (!selectedSourceId && !showAllClips) return false  // nothing selected → show nothing
      if (selectedSourceId && selectedSourceId !== "all") {
        if (c.source_downloaded_video_id !== selectedSourceId) return false
      }
      if (searchQuery) {
        const q = searchQuery.toLowerCase()
        return (c.title || "").toLowerCase().includes(q) ||
          (c.youtube_title || "").toLowerCase().includes(q)
      }
      return true
    })
    .sort((a, b) => {
      if (sortBy === "virality") return (b.clip_virality_score || 0) - (a.clip_virality_score || 0)
      if (sortBy === "newest") return new Date(b.created_at || 0) - new Date(a.created_at || 0)
      if (sortBy === "duration") return (b.duration_seconds || 0) - (a.duration_seconds || 0)
      return 0
    }), [clips, selectedSourceId, showAllClips, searchQuery, sortBy])

  // ── Handlers ─────────────────────────────────────────────────

  const handleExtract = async (videoId, opts) => {
    setExtracting(true)
    try {
      const isManual = opts.mode === "manual"
      // Only send a knob when the user actually set it — the default request
      // shape (and thus behavior) stays identical to before this feature.
      const payload = { caption_style: opts.caption_style }
      if (opts.force_retranscribe) {
        payload.whisper_quality = opts.whisper_quality
        payload.force_retranscribe = true
      }
      // emoji_style default is "moderate" — only send when overridden.
      if (opts.emoji_style && opts.emoji_style !== "moderate") payload.emoji_style = opts.emoji_style
      // remove_silence applies in BOTH modes. It was gated out of manual mode
      // on the theory that trimming would "shift the user's chosen timing" —
      // but the backend removes silence INSIDE each already-cut clip (extract
      // → desilence, see _process_clips_parallel), so the picked range
      // boundaries are untouched; the clip just gets tighter. The gate only
      // made hand-picked clips the one place pacing could NOT be fixed.
      if (opts.remove_silence) payload.remove_silence = true
      if (opts.force_vertical) payload.force_vertical = true

      if (isManual) {
        payload.mode = "manual"
        payload.time_ranges = opts.time_ranges
      } else {
        if (opts.min_duration) payload.min_duration = opts.min_duration
        if (opts.max_duration) payload.max_duration = opts.max_duration
        // Cap on clip count (the backend clamps 1..99 and auto-scales down
        // when the content can't support that many; omitted = ~1 per 30s).
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

  /* The bench's one destructive act. It lives here rather than in SourceBench
     because its button is in the hero — see benchRanges above. */
  const doCut = async () => {
    if (!benchSource || !cutCount || extracting || clipJobRunning) return
    const ok = await handleExtract(benchSource.id, {
      ...clipSettings.toPayload({ hasTranscript: !!benchSource.has_transcript_segments }),
      mode: "manual",
      time_ranges: benchRanges.ranges.map(({ start, end }) => ({ start, end })),
    })
    // Spent ranges must not stay armed. The button re-enables the moment the
    // job finishes and it re-rendered the SAME "Cut N clips" over the SAME
    // blocks, so a second click re-cut clips the user already had. (The ghost
    // lane drew them, but nothing stopped the cut.) Only on success: a failed
    // submit should leave the user's work intact so they can fix it and retry.
    if (ok) benchRanges.clear()
  }

  const handleUpload = async (platform) => {
    if (!selectedClip) return
    try {
      await http.post(`/api/videos/${selectedClip.id}/upload`, { platforms: [platform] })
      showSnackbar(`Uploading to ${platform}...`, "success")
    } catch (e) {
      showSnackbar(`Upload failed: ${e.response?.data?.detail || e.message}`, "error")
    }
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
      // thumb_v busts the thumbnail URL — without a new value the regenerated
      // image never appears: same URL, so React doesn't reload the <img> and
      // the browser serves the bytes it already has.
      const thumbV = Date.now()
      setSelectedClip(prev => ({ ...prev, thumbnail_path: res.data.thumbnail_path, thumb_v: thumbV }))
      setClips(prev => prev.map(c => c.id === selectedClip.id ? { ...c, thumbnail_path: res.data.thumbnail_path, thumb_v: thumbV } : c))
    } catch (e) {
      showSnackbar(`Thumbnail regen failed: ${e.response?.data?.detail || e.message}`, "error")
    } finally {
      setRegenThumb(false)
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

  // ── Render ───────────────────────────────────────────────────

  if (loading) {
    return (
      <Box sx={{ p: 3, display: "flex", flexDirection: "column", gap: 2 }}>
        <Skeleton variant="rounded" height={40} width={300} />
        <Stack direction="row" spacing={2}>
          <Skeleton variant="rounded" width={200} height={400} />
          <Skeleton variant="rounded" sx={{ flex: 1 }} height={400} />
        </Stack>
      </Box>
    )
  }

  return (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" }}>

      {/* ── Header ──────────────────────────────────────────── */}
      <Box sx={{
        px: 3, py: 2, flexShrink: 0,
        borderBottom: 1, borderColor: "divider",
        display: "flex", alignItems: "center", justifyContent: "space-between",
        background: (t) => t.palette.mode === "dark"
          ? "linear-gradient(135deg, rgba(201,100,66,0.08) 0%, rgba(30,28,26,1) 100%)"
          : "linear-gradient(135deg, rgba(201,100,66,0.06) 0%, rgba(255,255,255,1) 100%)",
      }}>
        <Stack direction="row" spacing={1.5} alignItems="center">
          <ContentCutIcon sx={{ color: "primary.main", fontSize: 26 }} />
          <Box>
            <Typography variant="h5" sx={{ fontWeight: 700, letterSpacing: -0.3 }}>
              Clip Studio
            </Typography>
            <Typography variant="caption" sx={{ color: "text.secondary" }}>
              {clipTotal ?? clips.length} clip{(clipTotal ?? clips.length) !== 1 ? "s" : ""} from {sources.length} video{sources.length !== 1 ? "s" : ""}{clipTotal > clips.length ? ` · showing latest ${clips.length}` : ""}
            </Typography>
          </Box>
        </Stack>

        <Stack direction="row" spacing={1}>
          {/* Search */}
          <TextField
            placeholder="Search clips..."
            size="small"
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            slotProps={{ input: { startAdornment: <SearchIcon sx={{ mr: 0.5, fontSize: 18, color: "text.secondary" }} /> } }}
            sx={{ width: 200 }}
          />

          {/* Sort */}
          <Button size="small" variant="outlined" startIcon={<SortIcon />}
            onClick={e => setSortAnchor(e.currentTarget)} sx={{ textTransform: "none" }}>
            {sortBy === "virality" ? "Top Viral" : sortBy === "newest" ? "Newest" : "Longest"}
          </Button>
          <Menu anchorEl={sortAnchor} open={Boolean(sortAnchor)} onClose={() => setSortAnchor(null)}>
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

          {/* Open Folder */}
          <Tooltip title="Open clips folder">
            <Button size="small" variant="outlined" sx={{ minWidth: 0, px: 1 }}
              onClick={() => http.post("/api/settings/open-folder", { folder: "generated" }).catch(() => showSnackbar("Could not open folder", "error"))}>
              <FolderOpenIcon fontSize="small" />
            </Button>
          </Tooltip>

          {/* Refresh */}
          <Tooltip title="Refresh sources & clips">
            <Button size="small" variant="outlined" onClick={() => { setLoading(true); fetchData() }}
              startIcon={<RefreshIcon fontSize="small" />}
              sx={{ textTransform: "none" }}>
              Refresh
            </Button>
          </Tooltip>

          {/* Cut, then Auto-cut. Same treatment — they are two ways of doing
              one thing and neither is a lesser button — so the ICON carries
              the difference: scissors for the cuts you chose, a wand for the
              ones the AI chooses. They used to share the scissors, which is
              what made them read as duplicates.

              `describeChild` on the tooltips: without it MUI promotes the
              title to the button's accessible NAME, so Auto-cut announced
              itself as a paragraph about Ask AI rather than as "Auto-cut". */}
          {onBench && (
            <Tooltip describeChild title={benchTooShort
              ? "This video is shorter than one second — there is nothing to cut"
              : cutCount
                ? "Cut exactly the ranges on the timeline"
                : "Drag on the filmstrip, press N, or Ask AI to add a range first"}>
              <span>
                <Button
                  size="small" variant="contained" color="primary"
                  disabled={!cutCount || extracting || clipJobRunning || benchTooShort}
                  startIcon={(extracting || clipJobRunning)
                    ? <CircularProgress size={14} color="inherit" />
                    : <ContentCutIcon fontSize="small" />}
                  onClick={doCut}
                  sx={{ textTransform: "none", whiteSpace: "nowrap", fontWeight: 700 }}
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
              <span>
                <Button
                  size="small" variant="contained" color="primary"
                  disabled={extracting || clipJobRunning}
                  startIcon={(extracting || clipJobRunning)
                    ? <CircularProgress size={14} color="inherit" />
                    : <AutoFixHighIcon fontSize="small" />}
                  onClick={() => {
                    const src = sources.find(v => v.id === selectedSourceId)
                    if (src) { setExtractTarget(src); setExtractDialogOpen(true) }
                  }}
                  sx={{ textTransform: "none", whiteSpace: "nowrap" }}>
                  {clipJobRunning ? "Cutting…" : "Auto-cut"}
                </Button>
              </span>
            </Tooltip>
          )}
        </Stack>
      </Box>

      {/* ── Active Jobs Progress ──────────────────────────────── */}
      <ActiveJobsBanner filter={clipJobFilter} fallbackLabel="Extracting clips…" />

      {/* ── Main Layout ─────────────────────────────────────── */}
      <Box sx={{ flex: 1, display: "flex", overflow: "hidden" }}>

        {/* ── Left: Source Videos ────────────────────────────── */}
        <Box sx={{
          width: 200, flexShrink: 0, overflow: "auto",
          borderRight: 1, borderColor: "divider",
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
          <Paper
            elevation={0}
            onClick={() => {
              setSelectedSourceId("all")
              // Auto-select first clip overall
              if (clips.length > 0) setSelectedClip(clips[0])
            }}
            sx={{
              p: 1, cursor: "pointer", borderRadius: 2,
              border: 2, borderColor: selectedSourceId === "all" ? "primary.main" : "transparent",
              bgcolor: selectedSourceId === "all" ? "action.selected" : "transparent",
              "&:hover": { bgcolor: "action.hover" },
              transition: "all 0.15s",
            }}
          >
            <Stack direction="row" spacing={1} alignItems="center">
              <ContentCutIcon sx={{ fontSize: 16, color: "primary.main" }} />
              <Typography variant="body2" sx={{ fontWeight: 600, fontSize: "0.8rem" }}>
                All Clips ({clips.length})
              </Typography>
            </Stack>
          </Paper>

          <Divider sx={{ my: 0.5 }} />

          {sources.filter(v => !sourceFilter || (v.title || "").toLowerCase().includes(sourceFilter.toLowerCase())).map(v => (
            <SourceVideoCard
              key={v.id}
              video={v}
              clipCount={clipCountBySource[v.id] || 0}
              isSelected={selectedSourceId === v.id}
              onPreview={setPreviewVideo}
              onClick={() => {
                const newId = selectedSourceId === v.id ? null : v.id
                setSelectedSourceId(newId)
                // Auto-select first clip from this source (or first overall if deselecting)
                if (newId) {
                  const sourceClips = clips.filter(c => c.source_downloaded_video_id === newId)
                  if (sourceClips.length > 0) setSelectedClip(sourceClips[0])
                  else setSelectedClip(null)
                } else {
                  setSelectedClip(null)
                }
              }}
            />
          ))}

          {sources.length === 0 && (
            <Box sx={{ p: 2, textAlign: "center" }}>
              <Typography variant="caption" sx={{ color: "text.disabled" }}>
                No source videos yet. Download some videos from the Library first.
              </Typography>
            </Box>
          )}
        </Box>

        {/* ── Center: Cutting bench | Clip inspector ─────────── */}
        <Box sx={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>

          {/* Mode switch — only meaningful when both surfaces have something
              to show, so it stays hidden until they do. */}
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
              <Typography variant="caption" sx={{ color: "text.disabled" }}>
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
            />
          ) : selectedClip ? (
            <Box sx={{ flex: 1, overflow: "auto", p: 3 }}>
              <Box sx={{ display: "flex", gap: 3, maxWidth: 1200, mx: "auto" }}>

                {/* Video Player */}
                <Box sx={{ width: 320, flexShrink: 0 }}>
                  <Paper
                    elevation={0}
                    sx={{
                      borderRadius: 3, overflow: "hidden",
                      bgcolor: "#000", position: "relative",
                      border: 1, borderColor: "divider",
                    }}
                  >
                    <Box
                      ref={videoRef}
                      component="video"
                      controls
                      autoPlay={false}
                      key={selectedClip.id}
                      sx={{ width: "100%", display: "block", maxHeight: 560 }}
                      src={`/api/videos/${selectedClip.id}/stream`}
                    />
                  </Paper>

                  {/* Source context */}
                  {selectedClip.clip_start_seconds != null && (
                    <Paper variant="outlined" sx={{ mt: 1.5, p: 1.5, borderRadius: 2 }}>
                      <Stack direction="row" spacing={1} alignItems="center">
                        <AccessTimeIcon sx={{ fontSize: 16, color: "text.secondary" }} />
                        <Typography variant="caption" sx={{ color: "text.secondary" }}>
                          {formatTime(selectedClip.clip_start_seconds)} — {formatTime(selectedClip.clip_end_seconds)} in source
                        </Typography>
                      </Stack>
                    </Paper>
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
                <Box sx={{ flex: 1, minWidth: 0 }}>
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
                          onClick={handleDelete}>
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
                        {selectedClip.clip_virality_reason && (
                          <Typography variant="body2" sx={{ color: "text.secondary", mt: 0.5, fontStyle: "italic", fontSize: "0.85rem" }}>
                            {selectedClip.clip_virality_reason}
                          </Typography>
                        )}
                      </Box>

                      {/* 5-factor virality scoreboard — Hook / Flow / Value /
                          Trend / Share bars. Hides itself for legacy clips that
                          carry neither a hook score nor a breakdown. */}
                      <ScoreBreakdownPanel clip={selectedClip} />

                      {/* YouTube */}
                      {selectedClip.youtube_title && (
                        <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
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
                        </Paper>
                      )}

                      {/* TikTok */}
                      {selectedClip.tiktok_title && (
                        <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
                          <Typography variant="overline" sx={{ color: "info.main", fontWeight: 700, fontSize: "0.65rem" }}>
                            TikTok
                          </Typography>
                          <Typography variant="body2" sx={{ fontWeight: 600, mt: 0.5 }}>
                            {selectedClip.tiktok_title}
                          </Typography>
                        </Paper>
                      )}

                      {/* Transcript */}
                      {selectedClip.script && (
                        <Box>
                          <Typography variant="overline" sx={{ color: "text.secondary", fontSize: "0.65rem" }}>
                            Transcript
                          </Typography>
                          <Paper variant="outlined" sx={{
                            p: 1.5, maxHeight: 200, overflowY: "auto",
                            fontSize: "0.8rem", color: "text.secondary", lineHeight: 1.6,
                            whiteSpace: "pre-wrap", borderRadius: 2,
                          }}>
                            {selectedClip.script}
                          </Paper>
                        </Box>
                      )}
                    </Stack>
                  )}
                </Box>
              </Box>
            </Box>
          ) : (
            /* Empty state */
            <Box sx={{
              flex: 1, display: "flex", flexDirection: "column",
              alignItems: "center", justifyContent: "center", gap: 2,
            }}>
              <ContentCutIcon sx={{ fontSize: 64, color: "text.disabled", opacity: 0.3 }} />
              <Typography variant="h6" sx={{ color: "text.disabled" }}>
                {clips.length === 0 ? "No clips yet" : "Select a clip to preview"}
              </Typography>
              <Typography variant="body2" sx={{ color: "text.disabled", textAlign: "center", maxWidth: 360 }}>
                {clips.length === 0
                  ? "Select a source video and click 'Extract Clips' to get started. AI will find the most viral moments."
                  : "Click on a clip in the filmstrip below to preview and manage it."}
              </Typography>
              {clips.length === 0 && sources.length > 0 && (
                <Button variant="contained" startIcon={<ContentCutIcon />}
                  onClick={() => { setExtractTarget(sources[0]); setExtractDialogOpen(true) }}>
                  Extract from {sources[0].title?.slice(0, 30) || "first video"}
                </Button>
              )}
            </Box>
          )}

          {/* ── Bottom: Clip Filmstrip ────────────────────────── */}
          <Box sx={{
            flexShrink: 0,
            borderTop: 1, borderColor: "divider",
            bgcolor: (t) => t.palette.mode === "dark" ? "rgba(0,0,0,0.2)" : "rgba(0,0,0,0.02)",
          }}>
            <Stack direction="row" spacing={0.5} alignItems="center" sx={{ px: 2, pt: 1.5, pb: 0.5 }}>
              <Typography variant="overline" sx={{ color: "text.secondary", fontSize: "0.65rem", flexShrink: 0 }}>
                Clips ({filteredClips.length})
              </Typography>
              <Box sx={{ flex: 1 }} />
              {/* The "Extract Clips" button that used to sit here is now
                  "Auto-cut" in the page hero, beside "Cut N clips". Leaving a
                  third door to the same dialog down here — under a heading
                  about clips you have ALREADY cut — made it read as a
                  different act from the one in the corner. */}
            </Stack>
            <Box sx={{
              // flex-start: with mixed aspects, a landscape card must not
              // stretch to portrait height with dead space under it.
              display: "flex", flexWrap: "nowrap", alignItems: "flex-start", gap: 1.5, px: 2, pb: 2, pt: 0.5,
              overflowX: "auto", overflowY: "hidden",
              "&::-webkit-scrollbar": { height: 6 },
              "&::-webkit-scrollbar-thumb": { bgcolor: "divider", borderRadius: 3 },
            }}>
              {filteredClips.length === 0 ? (
                <Box sx={{ py: 3, px: 4, textAlign: "center", width: "100%" }}>
                  <Typography variant="caption" sx={{ color: "text.disabled" }}>
                    {selectedSourceId ? "No clips from this video yet" : "No clips to display"}
                  </Typography>
                </Box>
              ) : (
                filteredClips.map(clip => (
                  <ClipCard
                    key={clip.id}
                    clip={clip}
                    isSelected={selectedClip?.id === clip.id}
                    onSelect={setSelectedClip}
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

      {/* Source video preview popup — video sizes to its natural aspect ratio
          (capped at 80vw/80vh), NOT stretched to the dialog width. */}
      <Dialog
        open={!!previewVideo}
        onClose={() => setPreviewVideo(null)}
        maxWidth="md"
        PaperProps={{ sx: { bgcolor: "#000", width: "auto" } }}
      >
        <DialogContent sx={{ p: 0, lineHeight: 0 }}>
          {previewVideo && (
            <Box
              component="video"
              src={`/api/downloaded/${previewVideo.id}/stream`}
              controls autoPlay
              style={{ display: "block", maxWidth: "80vw", maxHeight: "80vh" }}
            />
          )}
        </DialogContent>
      </Dialog>
    </Box>
  )
}
