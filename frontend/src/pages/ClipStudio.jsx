import { useState, useEffect, useCallback, useRef, useMemo } from "react"
import {
  Box, Typography, Paper, Stack, Chip, IconButton, Button, Divider,
  TextField, Tooltip, CircularProgress, Slider, Menu, MenuItem,
  ListItemText, ListItemIcon, Skeleton, Badge, alpha, FormControlLabel, Checkbox,
  Dialog, DialogTitle, DialogContent, DialogActions,
  LinearProgress, Collapse, Tabs, Tab,
} from "@mui/material"
import ContentCutIcon from "@mui/icons-material/ContentCut"
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
import TuneIcon from "@mui/icons-material/TuneOutlined"
import ExpandMoreIcon from "@mui/icons-material/ExpandMore"
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesomeOutlined"
import http from "../api/http"
import useAppStore from "../store/appStore"
import ActiveJobsBanner from "../components/create/ActiveJobsBanner"

/* ── Helpers ───────────────────────────────────────────────── */

function formatTime(seconds) {
  if (seconds == null) return "--:--"
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${String(s).padStart(2, "0")}`
}

function viralityColor(score) {
  if (score >= 8) return "success"
  if (score >= 6) return "warning"
  return "default"
}

function viralityLabel(score) {
  if (score >= 9) return "Viral"
  if (score >= 8) return "Strong"
  if (score >= 6) return "Good"
  if (score >= 4) return "Average"
  return "Low"
}

// Map AI-returned hook_type values to short user-facing labels. The closed
// set lives server-side in clip_extractor (curiosity_gap, contrarian,
// emotional_peak, question, number_promise, story_loop, actionable_tip,
// shocking_claim, general). "general" is the catch-all and renders as null
// (no chip) so we don't crowd the UI with an uninformative label.
const HOOK_TYPE_LABEL = {
  curiosity_gap:  "Curiosity gap",
  contrarian:     "Contrarian",
  emotional_peak: "Emotional peak",
  question:       "Question hook",
  number_promise: "Number promise",
  story_loop:     "Story loop",
  actionable_tip: "Actionable tip",
  shocking_claim: "Shocking claim",
}

function hookTypeLabel(t) {
  if (!t || t === "general") return null
  return HOOK_TYPE_LABEL[t] || null
}

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

function SourceVideoCard({ video, clipCount, isSelected, onClick }) {
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
        {(video.thumbnail_path || video.thumbnail_url) ? (
          <Box component="img"
            src={video.thumbnail_path ? `/api/downloaded/${video.id}/thumbnail` : video.thumbnail_url}
            alt=""
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

function ClipCard({ clip, isSelected, onClick }) {
  const score = clip.clip_virality_score
  return (
    <Paper
      elevation={0}
      onClick={onClick}
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
      <Box sx={{ width: "100%", aspectRatio: "9/16", position: "relative", bgcolor: "#000" }}>
        {clip.thumbnail_path ? (
          <Box component="img" src={`/api/videos/${clip.id}/thumbnail`} alt=""
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

/* ── Extract Clips Dialog ──────────────────────────────────── */

const WHISPER_QUALITIES = [
  { value: "fast", label: "Fast (base)", desc: "~30s per 5min video" },
  { value: "balanced", label: "Balanced (small)", desc: "~90s per 5min video" },
  { value: "accurate", label: "Accurate (medium)", desc: "~3min per 5min video" },
  { value: "best", label: "Best (large-v3)", desc: "~8min per 5min video" },
]

// Caption styles the OSS pipeline can render. emoji_style vocab matches the
// backend's _EMOJI_STYLES set (none|minimal|moderate|heavy, default moderate).
const CAPTION_STYLE_OPTIONS = ["viral", "classic", "bold", "none"]
const EMOJI_STYLE_OPTIONS = [
  { v: "none", label: "Off" },
  { v: "minimal", label: "Minimal" },
  { v: "moderate", label: "Moderate" },
  { v: "heavy", label: "Heavy" },
]

// Mirrors _MANUAL_MAX_RANGES in backend/api/downloaded.py — keep aligned so
// the UI's add-row cap matches what the backend will accept.
const MANUAL_ROWS_MAX = 10

// Client-side timestamp parse mirroring backend clip_extractor._parse_timestamp
// so the dialog can red-state a bad row + disable Submit before posting. The
// backend re-validates and is the authority; this is just instant feedback.
function parseTimestamp(text) {
  if (typeof text === "number") return text >= 0 ? text : null
  const s = String(text ?? "").trim()
  if (!s) return null
  const parts = s.split(":")
  if (parts.length > 3) return null
  const nums = parts.map(p => parseFloat(p))
  if (nums.some(n => !Number.isFinite(n) || n < 0)) return null
  if (nums.length >= 2 && nums[nums.length - 1] >= 60) return null
  if (nums.length === 3 && nums[1] >= 60) return null
  if (nums.length === 1) return nums[0]
  if (nums.length === 2) return nums[0] * 60 + nums[1]
  return nums[0] * 3600 + nums[1] * 60 + nums[2]
}

// Shared post-processing controls (rendered under both extraction modes).
function CaptionStylePicker({ value, onChange }) {
  return (
    <Box>
      <Typography variant="caption" sx={{ fontWeight: 600, mb: 0.5, display: "block" }}>Caption style</Typography>
      <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
        {CAPTION_STYLE_OPTIONS.map(s => (
          <Chip key={s} label={s} size="small"
            variant={value === s ? "filled" : "outlined"}
            color={value === s ? "primary" : "default"}
            onClick={() => onChange(s)}
            sx={{ textTransform: "capitalize", cursor: "pointer" }} />
        ))}
      </Stack>
    </Box>
  )
}

function EmojiStylePicker({ value, onChange, disabled }) {
  return (
    <Box>
      <Typography variant="caption" sx={{ fontWeight: 600, mb: 0.5, display: "block" }}>
        AutoEmoji <Chip label="captions only" size="small" variant="outlined" sx={{ ml: 0.5, height: 18, fontSize: "0.6rem" }} />
      </Typography>
      <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
        {EMOJI_STYLE_OPTIONS.map(({ v, label }) => (
          <Chip key={v} label={label} size="small"
            variant={value === v ? "filled" : "outlined"}
            color={value === v ? "primary" : "default"}
            onClick={() => onChange(v)}
            sx={{ cursor: "pointer" }}
            disabled={disabled} />
        ))}
      </Stack>
      <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.5, display: "block" }}>
        Emojis are inserted after matched keywords (🔥 fire, 💰 money, ❤️ love, etc.).
        {disabled ? " Disabled because captions are off." : ""}
      </Typography>
    </Box>
  )
}

function ExtractDialog({ open, onClose, video, onExtract }) {
  const hasSegments = !!video?.has_transcript_segments
  const [opts, setOpts] = useState({
    caption_style: "viral", min_duration: null, max_duration: null,
    whisper_quality: "balanced", retranscribe: false,
    remove_silence: false, user_query: "", target_platform: "",
    emoji_style: "moderate", genre: "",
  })
  const [advancedOpen, setAdvancedOpen] = useState(false)

  // Extraction strategy: "ai" (viral-clip picker, default) or "manual" (cut
  // user-supplied time ranges verbatim). Manual mode fills the `rows` list —
  // one row per range, capped at MANUAL_ROWS_MAX.
  const [mode, setMode] = useState("ai")
  const [rows, setRows] = useState([{ start: "", end: "" }])

  // Reset intent-tied state when the source video changes.
  useEffect(() => {
    setOpts(p => ({ ...p, retranscribe: false, user_query: "" }))
    setAdvancedOpen(false)
    setMode("ai")
    setRows([{ start: "", end: "" }])
  }, [video?.id])

  // Per-row resolution for manual mode: each entry gets {startSec, endSec,
  // valid, msg} so we can show an inline duration / error next to the fields.
  const rowResolutions = useMemo(() => rows.map((r) => {
    const startSec = parseTimestamp(r.start)
    const endSec = parseTimestamp(r.end)
    if (r.start === "" && r.end === "") return { startSec: null, endSec: null, valid: false, msg: null }
    if (startSec == null || endSec == null) return { startSec, endSec, valid: false, msg: "invalid time" }
    if (endSec <= startSec) return { startSec, endSec, valid: false, msg: "end ≤ start" }
    if (endSec - startSec < 1) return { startSec, endSec, valid: false, msg: "< 1s" }
    return { startSec, endSec, valid: true, msg: null }
  }), [rows])

  if (!video) return null

  const transcribeEnabled = !hasSegments || opts.retranscribe
  const durationError = opts.min_duration && opts.max_duration && opts.max_duration - opts.min_duration < 1

  // Active manual ranges = only the fully-valid rows. Any partial-but-invalid
  // row blocks Submit so a half-typed range can't ship.
  const activeManualRanges = mode === "manual"
    ? rowResolutions.filter(r => r.valid).map(r => ({ start: r.startSec, end: r.endSec }))
    : []
  const manualHasErrors = mode === "manual" && rows.some((r, i) =>
    (r.start !== "" || r.end !== "") && !rowResolutions[i].valid,
  )
  const manualEmpty = mode === "manual" && activeManualRanges.length === 0
  const canSubmit = mode === "ai" ? !durationError : (!manualEmpty && !manualHasErrors)

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <ContentCutIcon color="primary" /> Extract Clips
      </DialogTitle>
      <DialogContent>
        <Typography variant="body2" sx={{ color: "text.secondary", mb: 2 }}>
          From: <strong>{video.title || "Untitled"}</strong> ({formatTime(video.duration_seconds)})
        </Typography>

        {/* Mode toggle — AI picks the best moments, or you cut your own ranges. */}
        <Tabs
          value={mode}
          onChange={(_, v) => v && setMode(v)}
          variant="fullWidth"
          sx={{
            mb: 2, minHeight: 40,
            "& .MuiTab-root": { textTransform: "none", minHeight: 40, fontWeight: 600, fontSize: "0.85rem" },
          }}
        >
          <Tab value="ai" icon={<AutoAwesomeIcon sx={{ fontSize: 18 }} />} iconPosition="start" label="AI picks" />
          <Tab value="manual" icon={<ContentCutIcon sx={{ fontSize: 18 }} />} iconPosition="start" label="My ranges" />
        </Tabs>

        <Stack spacing={2.5}>
          {/* Transcription — used by BOTH modes (manual still needs the
              transcript to render captions against each cut range). */}
          <Box>
            <FormControlLabel
              control={
                <Checkbox
                  checked={transcribeEnabled}
                  onChange={e => setOpts(p => ({ ...p, retranscribe: e.target.checked }))}
                  disabled={!hasSegments}
                  size="small"
                />
              }
              label={
                <Typography variant="caption" sx={{ fontWeight: 600 }}>
                  Transcription {hasSegments
                    ? <Chip label="cached" size="small" color="success" variant="outlined" sx={{ ml: 0.5, height: 18, fontSize: "0.65rem" }} />
                    : <Chip label="required" size="small" color="warning" variant="outlined" sx={{ ml: 0.5, height: 18, fontSize: "0.65rem" }} />
                  }
                </Typography>
              }
              sx={{ mb: 0.5 }}
            />
            {!hasSegments && (
              <Typography variant="caption" sx={{ color: "text.secondary", display: "block", ml: 4, mb: 1 }}>
                No word-level transcript found — Whisper will transcribe the audio first
              </Typography>
            )}
            {hasSegments && !opts.retranscribe && (
              <Typography variant="caption" sx={{ color: "success.main", display: "block", ml: 4, mb: 1 }}>
                Using cached transcript — Whisper will be skipped
              </Typography>
            )}
            {hasSegments && opts.retranscribe && (
              <Typography variant="caption" sx={{ color: "warning.main", display: "block", ml: 4, mb: 1 }}>
                Will re-transcribe with selected model (replaces cached transcript)
              </Typography>
            )}
            <TextField select size="small" fullWidth
              value={opts.whisper_quality}
              onChange={e => setOpts(p => ({ ...p, whisper_quality: e.target.value }))}
              disabled={!transcribeEnabled}
              sx={{ ml: 0 }}
            >
              {WHISPER_QUALITIES.map(q => (
                <MenuItem key={q.value} value={q.value}>
                  <Stack direction="row" justifyContent="space-between" sx={{ width: "100%" }}>
                    <Typography variant="body2">{q.label}</Typography>
                    <Typography variant="caption" sx={{ color: "text.secondary", ml: 2 }}>{q.desc}</Typography>
                  </Stack>
                </MenuItem>
              ))}
            </TextField>
          </Box>

          {/* ── AI mode ─────────────────────────────────────────── */}
          {mode === "ai" && (
          <>
          {/* Duration range */}
          <Box>
            <Typography variant="caption" sx={{ fontWeight: 600, mb: 0.5, display: "block" }}>
              Clip duration range (leave empty for auto 15–60s)
            </Typography>
            <Stack direction="row" spacing={2} alignItems="center">
              <TextField label="Min (s)" type="number" size="small" sx={{ width: 90 }}
                value={opts.min_duration || ""}
                error={!!durationError}
                slotProps={{ htmlInput: { min: 10, max: 120 } }}
                onChange={e => setOpts(p => ({ ...p, min_duration: parseInt(e.target.value) || null }))} />
              <Typography variant="body2" sx={{ color: "text.secondary" }}>to</Typography>
              <TextField label="Max (s)" type="number" size="small" sx={{ width: 90 }}
                value={opts.max_duration || ""}
                error={!!durationError}
                slotProps={{ htmlInput: { min: 15, max: 180 } }}
                onChange={e => setOpts(p => ({ ...p, max_duration: parseInt(e.target.value) || null }))} />
            </Stack>
            {durationError && (
              <Typography variant="caption" sx={{ color: "error.main", mt: 0.5, display: "block" }}>
                Max must be at least 1 second greater than Min
              </Typography>
            )}
          </Box>

          <CaptionStylePicker
            value={opts.caption_style}
            onChange={v => setOpts(p => ({ ...p, caption_style: v }))} />

          {/* Advanced-options expander — power-user knobs collapsed so the
              dialog opens compact. Defaults are sensible; only niche cases
              (custom query, platform / genre bias, emoji, silence) touch these. */}
          <Box>
            <Button
              variant="text"
              startIcon={<TuneIcon sx={{ fontSize: 18 }} />}
              endIcon={<ExpandMoreIcon sx={{ fontSize: 18, transform: advancedOpen ? "rotate(180deg)" : "rotate(0deg)", transition: "transform .15s ease" }} />}
              onClick={() => setAdvancedOpen(v => !v)}
              sx={{
                textTransform: "none", fontWeight: 600, color: "text.secondary", px: 0.5, py: 0.5,
                "&:hover": { bgcolor: "action.hover", color: "primary.main" },
              }}
            >
              Advanced options
              <Typography variant="caption" sx={{ ml: 0.75, color: "text.disabled", fontWeight: 500 }}>
                find specific moments · platform · genre · emoji · silence
              </Typography>
            </Button>
          </Box>

          <Collapse in={advancedOpen} timeout={200}>
            <Stack spacing={2.5}>
              {/* User query — natural-language clip filter. When non-empty the
                  AI ranks segments by match to this query first, then virality. */}
              <Box>
                <Typography variant="caption" sx={{ fontWeight: 600, mb: 0.5, display: "block" }}>
                  Find specific moments <Chip label="optional" size="small" variant="outlined" sx={{ ml: 0.5, height: 18, fontSize: "0.6rem" }} />
                </Typography>
                <TextField
                  size="small" fullWidth
                  placeholder='e.g. "every joke that landed", "all Q&A moments"'
                  value={opts.user_query}
                  onChange={e => setOpts(p => ({ ...p, user_query: e.target.value }))}
                  slotProps={{ htmlInput: { maxLength: 500 } }}
                />
                <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.5, display: "block" }}>
                  Leave empty to find the most viral clips automatically.
                </Typography>
              </Box>

              {/* Target platform — biases the AI hook-type ranker. */}
              <Box>
                <Typography variant="caption" sx={{ fontWeight: 600, mb: 0.5, display: "block" }}>
                  Target platform <Chip label="optional" size="small" variant="outlined" sx={{ ml: 0.5, height: 18, fontSize: "0.6rem" }} />
                </Typography>
                <TextField select size="small" fullWidth
                  value={opts.target_platform}
                  onChange={e => setOpts(p => ({ ...p, target_platform: e.target.value }))}
                >
                  <MenuItem value="">Any (general viral ranking)</MenuItem>
                  <MenuItem value="tiktok">TikTok — shock / contrarian / emotional</MenuItem>
                  <MenuItem value="youtube_shorts">YouTube Shorts — curiosity / numbers / story</MenuItem>
                  <MenuItem value="reels">Instagram Reels — emotional / lifestyle</MenuItem>
                  <MenuItem value="linkedin">LinkedIn — actionable / data-backed</MenuItem>
                  <MenuItem value="twitter">Twitter / X — hot takes / debates</MenuItem>
                </TextField>
                <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.5, display: "block" }}>
                  Biases which hook types the AI prioritizes. Clip length is unchanged.
                </Typography>
              </Box>

              {/* Content genre — biases the AI's clip-selection heuristics. */}
              <Box>
                <Typography variant="caption" sx={{ fontWeight: 600, mb: 0.5, display: "block" }}>
                  Content genre <Chip label="optional" size="small" variant="outlined" sx={{ ml: 0.5, height: 18, fontSize: "0.6rem" }} />
                </Typography>
                <TextField select size="small" fullWidth
                  value={opts.genre}
                  onChange={e => setOpts(p => ({ ...p, genre: e.target.value }))}
                >
                  <MenuItem value="">Auto-detect (no genre bias)</MenuItem>
                  <MenuItem value="podcast">Podcast — guest's quotable moments</MenuItem>
                  <MenuItem value="interview">Interview — best Q&amp;A answers</MenuItem>
                  <MenuItem value="qa">Q&amp;A / AMA — question + answer pairs</MenuItem>
                  <MenuItem value="vlog">Vlog — reactions + storytelling beats</MenuItem>
                  <MenuItem value="tutorial">Tutorial / how-to — standalone tips</MenuItem>
                  <MenuItem value="gaming">Gaming — big plays + reactions</MenuItem>
                  <MenuItem value="reaction">Reaction — emotional peaks</MenuItem>
                  <MenuItem value="lecture">Lecture / educational — concept explainers</MenuItem>
                </TextField>
                <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.5, display: "block" }}>
                  Tells the AI what shape a "good clip" has for this content type.
                </Typography>
              </Box>

              <EmojiStylePicker
                value={opts.emoji_style}
                disabled={opts.caption_style === "none"}
                onChange={v => setOpts(p => ({ ...p, emoji_style: v }))} />

              {/* Silence & filler removal */}
              <Box>
                <FormControlLabel
                  control={
                    <Checkbox
                      checked={opts.remove_silence}
                      onChange={e => setOpts(p => ({ ...p, remove_silence: e.target.checked }))}
                      size="small"
                    />
                  }
                  label={<Typography variant="caption" sx={{ fontWeight: 600 }}>Remove silence & filler words</Typography>}
                />
                <Typography variant="caption" sx={{ color: "text.secondary", display: "block", ml: 4, mt: -0.5 }}>
                  Cuts out "um", "uh", long pauses — tighter pacing for short-form
                </Typography>
              </Box>
            </Stack>
          </Collapse>
          </>
          )}

          {/* ── Manual mode ─────────────────────────────────────── */}
          {mode === "manual" && (
          <>
            <Box>
              <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
                <Typography variant="caption" sx={{ fontWeight: 600 }}>Time ranges</Typography>
                <Typography variant="caption" sx={{ color: "text.disabled" }}>
                  ({rows.length}/{MANUAL_ROWS_MAX})
                </Typography>
              </Stack>
              <Typography variant="caption" sx={{ color: "text.secondary", mb: 1, display: "block" }}>
                Accepts <code>SS</code>, <code>MM:SS</code>, or <code>HH:MM:SS</code> (with optional <code>.fff</code>).
              </Typography>

              <Stack spacing={0.75}>
                {rows.map((r, i) => {
                  const res = rowResolutions[i]
                  const isError = (r.start !== "" || r.end !== "") && !res.valid
                  return (
                    <Stack key={i} direction="row" spacing={1} alignItems="center">
                      <TextField
                        size="small" placeholder="Start" value={r.start}
                        onChange={(e) => setRows(prev => prev.map((row, j) => j === i ? { ...row, start: e.target.value } : row))}
                        error={isError && res.startSec == null}
                        sx={{ width: 110 }}
                        slotProps={{ htmlInput: { style: { fontFamily: "ui-monospace, monospace", fontSize: "0.85rem" } } }}
                      />
                      <Typography variant="caption" sx={{ color: "text.disabled" }}>→</Typography>
                      <TextField
                        size="small" placeholder="End" value={r.end}
                        onChange={(e) => setRows(prev => prev.map((row, j) => j === i ? { ...row, end: e.target.value } : row))}
                        error={isError && (res.endSec == null || (res.startSec != null && res.endSec != null && res.endSec <= res.startSec))}
                        sx={{ width: 110 }}
                        slotProps={{ htmlInput: { style: { fontFamily: "ui-monospace, monospace", fontSize: "0.85rem" } } }}
                      />
                      <Box sx={{ flex: 1, minWidth: 0 }}>
                        {res.valid && (
                          <Typography variant="caption" sx={{ color: "success.main", fontWeight: 600 }}>
                            {Math.round(res.endSec - res.startSec)}s
                          </Typography>
                        )}
                        {res.msg && (
                          <Typography variant="caption" sx={{ color: "error.main" }}>{res.msg}</Typography>
                        )}
                      </Box>
                      <IconButton
                        size="small" aria-label="Remove row"
                        onClick={() => setRows(prev => prev.length > 1 ? prev.filter((_, j) => j !== i) : prev)}
                        disabled={rows.length === 1}
                        sx={{ color: "text.secondary" }}
                      >
                        <CloseIcon sx={{ fontSize: 16 }} />
                      </IconButton>
                    </Stack>
                  )
                })}
                <Button
                  size="small"
                  startIcon={<AddIcon sx={{ fontSize: 16 }} />}
                  onClick={() => setRows(prev => prev.length < MANUAL_ROWS_MAX ? [...prev, { start: "", end: "" }] : prev)}
                  disabled={rows.length >= MANUAL_ROWS_MAX}
                  sx={{ textTransform: "none", alignSelf: "flex-start", fontSize: "0.78rem" }}
                >
                  Add range
                </Button>
                {rows.length >= MANUAL_ROWS_MAX && (
                  <Typography variant="caption" sx={{ color: "text.secondary" }}>
                    Cap at {MANUAL_ROWS_MAX} ranges per submit.
                  </Typography>
                )}
              </Stack>
            </Box>

            <CaptionStylePicker
              value={opts.caption_style}
              onChange={v => setOpts(p => ({ ...p, caption_style: v }))} />

            <EmojiStylePicker
              value={opts.emoji_style}
              disabled={opts.caption_style === "none"}
              onChange={v => setOpts(p => ({ ...p, emoji_style: v }))} />
          </>
          )}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" startIcon={<ContentCutIcon />}
          disabled={!canSubmit}
          onClick={() => {
            onExtract(video.id, {
              ...opts,
              force_retranscribe: transcribeEnabled,
              mode,
              time_ranges: mode === "manual" ? activeManualRanges : undefined,
            })
            onClose()
          }}>
          {mode === "manual"
            ? `Extract ${activeManualRanges.length || ""} clip${activeManualRanges.length !== 1 ? "s" : ""}`.trim()
            : "Extract Clips"}
        </Button>
      </DialogActions>
    </Dialog>
  )
}


/* ══════════════════════════════════════════════════════════════
   MAIN COMPONENT: Clip Studio
   ══════════════════════════════════════════════════════════════ */

export default function ClipStudio() {
  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const activeJobs = useAppStore((s) => s.activeJobs)

  // Track clip extraction jobs (restored from API on page load via useWebSocket)
  const isClipJob = (j) =>
    (j.message && j.message.toLowerCase().includes("clip"))
    || (j.inputData && j.inputData.type === "clip_extraction")
  const clipJobs = Object.values(activeJobs).filter(isClipJob)
  const clipJobFilter = (j) => j.status === "running" && isClipJob(j)
  const justCompletedRef = useRef(new Set())

  // Data
  const [sources, setSources] = useState([])
  const [clips, setClips] = useState([])
  const [loading, setLoading] = useState(true)
  const [selectedSourceId, setSelectedSourceId] = useState(null) // "all" or video id
  const [selectedClip, setSelectedClip] = useState(null)
  const [sourceFilter, setSourceFilter] = useState("")
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

  // Video player ref
  const videoRef = useRef(null)

  // ── Load data ────────────────────────────────────────────────
  const fetchData = useCallback(async () => {
    try {
      const [srcRes, clipRes] = await Promise.all([
        http.get("/api/downloaded", { params: { limit: 200 } }),
        http.get("/api/videos", { params: { limit: 100 } }),
      ])
      // Show all downloaded videos (sorted longest first — best for clipping)
      const downloadedVideos = (srcRes.data?.videos || srcRes.data || [])
        .sort((a, b) => (b.duration_seconds || 0) - (a.duration_seconds || 0))
      setSources(downloadedVideos)

      // Only show clip_extraction videos
      const clipVideos = (clipRes.data.videos || []).filter(v => v.source_type === "clip_extraction")
      setClips(clipVideos)

      // Don't auto-select — let user choose from filmstrip or sidebar
    } catch (e) {
      console.error("Failed to load clip studio data:", e)
      showSnackbar("Failed to load clip data", "error")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchData() }, [fetchData])

  // Auto-refresh when a clip extraction job completes
  useEffect(() => {
    for (const job of clipJobs) {
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
  }, [clipJobs, fetchData])

  // ── Derived data ─────────────────────────────────────────────
  const clipCountBySource = {}
  clips.forEach(c => {
    const sid = c.source_downloaded_video_id
    if (sid) clipCountBySource[sid] = (clipCountBySource[sid] || 0) + 1
  })

  // Only show clips when a source is selected (or "all" explicitly chosen)
  const showAllClips = selectedSourceId === "all"
  const filteredClips = clips
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
    })

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
      // remove_silence is an AI-mode concept only. A hand-picked manual range
      // is a deliberate cut, so never re-cut it for silence (would shift the
      // user's chosen timing). Gate the field on mode rather than let a stale
      // AI-mode value leak through.
      if (opts.remove_silence && !isManual) payload.remove_silence = true

      if (isManual) {
        payload.mode = "manual"
        payload.time_ranges = opts.time_ranges
      } else {
        if (opts.min_duration) payload.min_duration = opts.min_duration
        if (opts.max_duration) payload.max_duration = opts.max_duration
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
    } catch (e) {
      showSnackbar(`Extract failed: ${e.response?.data?.detail || e.message}`, "error")
    } finally {
      setExtracting(false)
    }
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

  const handleRegenThumbnail = async () => {
    if (!selectedClip) return
    setRegenThumb(true)
    try {
      const res = await http.post(`/api/videos/${selectedClip.id}/regenerate-thumbnail`)
      showSnackbar("Thumbnail regenerated!", "success")
      setSelectedClip(prev => ({ ...prev, thumbnail_path: res.data.thumbnail_path }))
      setClips(prev => prev.map(c => c.id === selectedClip.id ? { ...c, thumbnail_path: res.data.thumbnail_path } : c))
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
              {clips.length} clip{clips.length !== 1 ? "s" : ""} from {sources.length} video{sources.length !== 1 ? "s" : ""}
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

        {/* ── Center: Preview + Details ──────────────────────── */}
        <Box sx={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>

          {selectedClip ? (
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
                    <Chip label="9:16" size="small" variant="outlined" />
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
              {/* Extract button for selected source */}
              {selectedSourceId && selectedSourceId !== "all" && (
                <Button size="small" variant="contained" color="primary"
                  startIcon={extracting ? <CircularProgress size={14} color="inherit" /> : <ContentCutIcon />}
                  disabled={extracting}
                  onClick={() => {
                    const src = sources.find(s => s.id === selectedSourceId)
                    if (src) { setExtractTarget(src); setExtractDialogOpen(true) }
                  }}
                  sx={{ textTransform: "none", height: 30, fontSize: "0.8rem", fontWeight: 700, px: 2, borderRadius: 2 }}>
                  Extract Clips
                </Button>
              )}
            </Stack>
            <Box sx={{
              display: "flex", flexWrap: "nowrap", gap: 1.5, px: 2, pb: 2, pt: 0.5,
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
                    onClick={() => setSelectedClip(clip)}
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
      />
    </Box>
  )
}
