import { useState, useEffect, useCallback, useRef, useMemo, Fragment } from "react"
import { useNavigate } from "react-router-dom"
import {
  Box, Typography, Stack, Button, LinearProgress, Alert, IconButton, Chip,
  Dialog, DialogContent, Tooltip,
} from "@mui/material"
import { GlassPanel, GlassCard } from "../../utils/glassFx"
import { alpha } from "@mui/material/styles"
import MovieFilterOutlinedIcon from "@mui/icons-material/MovieFilterOutlined"
import CloudUploadOutlinedIcon from "@mui/icons-material/CloudUploadOutlined"
import DownloadOutlinedIcon from "@mui/icons-material/DownloadOutlined"
import PlayCircleOutlineIcon from "@mui/icons-material/PlayCircleOutline"
import CloseIcon from "@mui/icons-material/Close"
import ArrowBackIcon from "@mui/icons-material/ArrowBack"
import DragIndicatorIcon from "@mui/icons-material/DragIndicator"
import WarningAmberIcon from "@mui/icons-material/WarningAmber"
import http from "../../api/http"
import { toolJobTypeFor } from "../../components/tools/toolJobType"
import useAppStore from "../../store/appStore"
import useDocumentTitle from "../../hooks/useDocumentTitle"

// Mirrors backend/api/tools.py MERGE_CLIPS_MIN/MAX. Keep in sync.
const MIN_CLIPS = 2
const MAX_CLIPS = 10
const MAX_MB = 1000
const ACCEPT = ".mp4,.mov,.mkv,.webm,.m4v"

const ASPECT_OPTIONS = [
  { id: "auto",  label: "Auto",      sub: "match first clip" },
  { id: "9:16",  label: "Vertical",  sub: "TikTok / Shorts" },
  { id: "16:9",  label: "Landscape", sub: "YouTube" },
  { id: "1:1",   label: "Square",    sub: "Instagram" },
]

// Card visual constants — kept in one place so the empty-state placeholder
// matches the real card height pixel-for-pixel.
const CARD_W = 140

export default function ToolMergeClips() {
  useDocumentTitle("Merge Clips")
  const navigate = useNavigate()
  const inputRef = useRef(null)
  const [clips, setClips] = useState([])      // [{ id, file, url }]
  const [aspect, setAspect] = useState("auto")
  const [bundle, setBundle] = useState(false)  // export 9:16 + 1:1 + 16:9 as ZIP
  const [transition, setTransition] = useState("none")  // none | crossfade between clips
  const [error, setError] = useState("")
  const [dragOver, setDragOver] = useState(false)        // dropzone hover state
  const [reorderIdx, setReorderIdx] = useState(null)     // index of card being dragged
  const [reorderOver, setReorderOver] = useState(null)   // index of card being hovered as drop target
  const [previewClip, setPreviewClip] = useState(null)   // clip object for play dialog
  const [jobId, setJobId] = useState(null)
  const [terminal, setTerminal] = useState(null)

  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const startJob = useAppStore((s) => s.startJob)
  const job = useAppStore((s) => (jobId ? s.activeJobs[jobId] : null))

  // Snapshot terminal state — appStore drops the entry 10s after success and
  // our Download button would disappear with it.
  useEffect(() => {
    if (job && (job.status === "success" || job.status === "failed") && !terminal) {
      setTerminal({ status: job.status, step: job.step })
    }
  }, [job, terminal])

  // Revoke blob URLs on unmount so we don't leak memory if the user navigates
  // away mid-session with several large clips loaded.
  useEffect(() => {
    return () => {
      for (const c of clips) {
        try { URL.revokeObjectURL(c.url) } catch { /* noop */ }
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const reset = useCallback(() => {
    for (const c of clips) {
      try { URL.revokeObjectURL(c.url) } catch { /* noop */ }
    }
    setClips([])
    setAspect("auto")
    setError("")
    setJobId(null)
    setTerminal(null)
  }, [clips])

  const addFiles = useCallback((fileList) => {
    setError("")
    const incoming = Array.from(fileList || [])
    if (!incoming.length) return
    const maxBytes = MAX_MB * 1024 * 1024
    const valid = []
    for (const f of incoming) {
      if (f.size > maxBytes) {
        setError(`"${f.name}" is too large (max ${MAX_MB} MB).`)
        continue
      }
      const ext = "." + (f.name.split(".").pop() || "").toLowerCase()
      if (!ACCEPT.split(",").includes(ext)) {
        setError(`"${f.name}" is not a supported video format.`)
        continue
      }
      valid.push({
        id: `${f.name}-${f.size}-${f.lastModified}-${Math.random()}`,
        file: f,
        url: URL.createObjectURL(f),
      })
    }
    setClips((prev) => {
      const merged = [...prev, ...valid]
      if (merged.length > MAX_CLIPS) {
        // Revoke the URLs we're about to drop so they don't leak.
        for (const c of merged.slice(MAX_CLIPS)) {
          try { URL.revokeObjectURL(c.url) } catch { /* noop */ }
        }
        setError(`Max ${MAX_CLIPS} clips per merge — keeping the first ${MAX_CLIPS}.`)
        return merged.slice(0, MAX_CLIPS)
      }
      return merged
    })
  }, [])

  const removeClip = (idx) => {
    setClips((prev) => {
      const removed = prev[idx]
      if (removed) {
        try { URL.revokeObjectURL(removed.url) } catch { /* noop */ }
      }
      return prev.filter((_, i) => i !== idx)
    })
  }

  // ── Drag-to-reorder (HTML5 native, no library) ──────────────────────────
  // Pattern: dragstart records source index, dragover on a target detects
  // left/right half (= insert before/after) and updates hover state, drop
  // moves source → resolved insert index by splice-out / splice-in.
  const onCardDragStart = (i) => (e) => {
    setReorderIdx(i)
    e.dataTransfer.effectAllowed = "move"
    try {
      const thumb = e.currentTarget.querySelector("[data-drag-ghost]")
      if (thumb) {
        const r = thumb.getBoundingClientRect()
        e.dataTransfer.setDragImage(thumb, r.width / 2, r.height / 2)
      }
    } catch { /* noop — fall back to default drag image */ }
    try { e.dataTransfer.setData("text/plain", String(i)) } catch { /* noop */ }
  }
  const onCardDragOver = (i) => (e) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = "move"
    if (reorderIdx === null) return
    const r = e.currentTarget.getBoundingClientRect()
    const side = (e.clientX - r.left) < r.width / 2 ? "before" : "after"
    if (!reorderOver || reorderOver.targetIdx !== i || reorderOver.side !== side) {
      setReorderOver({ targetIdx: i, side })
    }
  }
  const onCardDrop = (i) => (e) => {
    e.preventDefault()
    if (reorderIdx === null) return
    const side = reorderOver?.targetIdx === i ? reorderOver.side : "before"
    let insertAt = side === "before" ? i : i + 1
    if (reorderIdx === insertAt || reorderIdx === insertAt - 1) {
      // No-op: dropping back where we started (or one position over from itself).
      setReorderIdx(null); setReorderOver(null); return
    }
    setClips((prev) => {
      const arr = [...prev]
      const [moved] = arr.splice(reorderIdx, 1)
      // Adjust for the spliced-out element when inserting AFTER source.
      if (insertAt > reorderIdx) insertAt -= 1
      arr.splice(insertAt, 0, moved)
      return arr
    })
    setReorderIdx(null); setReorderOver(null)
  }
  const onCardDragEnd = () => { setReorderIdx(null); setReorderOver(null) }

  // ── Dropzone (different from card-reorder drag — this accepts FILES) ────
  const onDropzoneDrop = (e) => {
    e.preventDefault()
    setDragOver(false)
    // Skip if this is a card reorder, not a file drop.
    if (reorderIdx !== null) return
    addFiles(e.dataTransfer.files)
  }

  const submit = async () => {
    setError("")
    if (clips.length < MIN_CLIPS) {
      setError(`Need at least ${MIN_CLIPS} clips.`)
      return
    }
    const form = new FormData()
    for (const c of clips) form.append("files", c.file)
    form.append("target_aspect", aspect)
    if (bundle) form.append("bundle", "true")
    form.append("transition", transition)
    try {
      const { data } = await http.post("/api/tools/merge-clips", form, {
        headers: { "Content-Type": "multipart/form-data" },
        timeout: 300000,
      })
      startJob(data.job_id, toolJobTypeFor("/api/tools/merge-clips"), "Starting...")
      setJobId(data.job_id)
    } catch (e) {
      const msg = e?.response?.data?.detail || e.message || "Upload failed"
      setError(msg)
      showSnackbar?.(msg, "error")
    }
  }

  const jobRunning = job && (job.status === "running" || job.status === "pending") && !terminal
  const jobSuccess = (job && job.status === "success") || terminal?.status === "success"
  const jobFailed = (job && job.status === "failed") || terminal?.status === "failed"
  const editingDisabled = jobRunning || jobSuccess || jobFailed

  // Guard browser close / reload while a merge is in progress.
  useEffect(() => {
    if (!jobRunning) return
    const handler = (e) => {
      e.preventDefault()
      e.returnValue = ""  // legacy Chrome/Firefox API; still required
      return ""
    }
    window.addEventListener("beforeunload", handler)
    return () => window.removeEventListener("beforeunload", handler)
  }, [jobRunning])

  return (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column" }}>
      {/* Header — title on the left, primary action on the right.
          The Merge button used to live in the body; the bottom-pinned
          filmstrip would hide it on shorter viewports. Moving the action
          to the title bar keeps it always visible. Hidden during
          running/success/failed states (`editingDisabled`). */}
      <Box sx={{
        px: 3, py: 2, flexShrink: 0,
        borderBottom: 1, borderColor: "divider",
        display: "flex", alignItems: "center", justifyContent: "space-between",
        gap: 2,
        background: (t) => t.palette.mode === "dark"
          ? "linear-gradient(135deg, rgba(201,100,66,0.08) 0%, rgba(30,28,26,1) 100%)"
          : "linear-gradient(135deg, rgba(201,100,66,0.06) 0%, rgba(255,255,255,1) 100%)",
      }}>
        <Stack direction="row" spacing={1.5} alignItems="center" sx={{ minWidth: 0 }}>
          <IconButton size="small" onClick={() => navigate("/tools")}>
            <ArrowBackIcon />
          </IconButton>
          <Box sx={{ color: "primary.main", display: "flex", alignItems: "center" }}>
            <MovieFilterOutlinedIcon fontSize="large" />
          </Box>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="h5" sx={{ fontWeight: 700, letterSpacing: -0.3 }}>
              Merge Clips
            </Typography>
            <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.25, display: "block" }} noWrap>
              Stitch multiple clips into one video — auto-cropped to your target aspect.
            </Typography>
          </Box>
        </Stack>

        {!editingDisabled && (
          <Button
            variant="contained"
            onClick={submit}
            disabled={clips.length < MIN_CLIPS}
            sx={{ flexShrink: 0, px: 3 }}
          >
            Merge {clips.length >= MIN_CLIPS ? `${clips.length} clips` : "clips"}
          </Button>
        )}
      </Box>

      {/* Top region: dropzone + config + action / progress / result.
          Scrolls if content overflows; the filmstrip stays pinned at the bottom. */}
      <Box sx={{ flex: 1, overflow: "auto", px: 3, pt: 3, pb: 6, display: "flex", justifyContent: "center" }}>
        <Box sx={{ maxWidth: 1100, width: "100%", display: "flex", flexDirection: "column", gap: 2.5 }}>

          {/* Dropzone */}
          {!editingDisabled && (
            <GlassPanel
              onDragOver={(e) => { e.preventDefault(); if (reorderIdx === null) setDragOver(true) }}
              onDragLeave={() => setDragOver(false)}
              onDrop={onDropzoneDrop}
              onClick={() => inputRef.current?.click()}
              sx={{
                p: 4,
                textAlign: "center",
                borderRadius: 2.5,
                borderStyle: "dashed",
                borderWidth: 2,
                borderColor: (t) => dragOver
                  ? t.palette.primary.main
                  : (t.palette.mode === "dark" ? "rgba(255,255,255,0.18)" : "rgba(0,0,0,0.16)"),
                cursor: "pointer",
                transition: "all 0.18s ease",
                "&:hover": {
                  borderColor: "primary.main",
                  transform: "translateY(-1px)",
                },
              }}
            >
              <input
                ref={inputRef}
                type="file"
                hidden
                accept={ACCEPT}
                multiple
                onChange={(e) => { addFiles(e.target.files); e.target.value = "" }}
              />
              <Stack spacing={1} alignItems="center">
                <CloudUploadOutlinedIcon sx={{ fontSize: 40, color: "text.secondary" }} />
                <Typography variant="body2" sx={{ fontWeight: 600 }}>
                  {clips.length === 0
                    ? "Drop clips here or click to browse"
                    : `Add more clips (${clips.length}/${MAX_CLIPS})`}
                </Typography>
                <Typography variant="caption" sx={{ color: "text.secondary" }}>
                  {ACCEPT.replace(/\./g, "").toUpperCase()} · up to {MAX_MB} MB each · {MIN_CLIPS}-{MAX_CLIPS} clips
                </Typography>
              </Stack>
            </GlassPanel>
          )}

          {/* Aspect ratio selector. The bundle toggle below replaces it
              when active — same merge runs three full passes (9:16 + 1:1
              + 16:9), output is a ZIP. */}
          {!editingDisabled && (
            <GlassPanel sx={{ p: 2.5 }}>
              <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
                <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                  Output aspect ratio
                </Typography>
                <Stack
                  direction="row" alignItems="center" spacing={0.75}
                  onClick={() => setBundle((b) => !b)}
                  sx={{ cursor: "pointer", userSelect: "none" }}
                >
                  <Box
                    sx={{
                      width: 32, height: 18, borderRadius: 9,
                      bgcolor: bundle ? "primary.main" : "action.disabledBackground",
                      position: "relative", transition: "background 0.15s",
                    }}
                  >
                    <Box
                      sx={{
                        position: "absolute", top: 2, left: bundle ? 16 : 2,
                        width: 14, height: 14, borderRadius: "50%",
                        bgcolor: "#fff",
                        transition: "left 0.15s",
                        boxShadow: "0 1px 3px rgba(0,0,0,0.3)",
                      }}
                    />
                  </Box>
                  <Typography variant="caption" sx={{ fontWeight: 600 }}>
                    Export all platforms (ZIP)
                  </Typography>
                </Stack>
              </Stack>
              {bundle ? (
                <Typography variant="caption" sx={{ color: "text.secondary", display: "block" }}>
                  Renders the merge three times — 9:16 (TikTok / Reels / Shorts), 1:1 (Instagram Feed),
                  and 16:9 (YouTube) — and downloads as a single ZIP.
                </Typography>
              ) : (
                <>
                  <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mb: 1.5 }}>
                    Each clip is center-cropped to fill the target. Auto picks the closest match to your first clip.
                  </Typography>
                  <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                    {ASPECT_OPTIONS.map((opt) => (
                      <Chip
                        key={opt.id}
                        label={
                          <Stack alignItems="flex-start" sx={{ py: 0.25 }}>
                            <Typography variant="caption" sx={{ fontWeight: 700, lineHeight: 1.2 }}>
                              {opt.label}
                            </Typography>
                            <Typography variant="caption" sx={{ fontSize: 10, opacity: 0.8, lineHeight: 1.1 }}>
                              {opt.sub}
                            </Typography>
                          </Stack>
                        }
                        color={aspect === opt.id ? "primary" : "default"}
                        variant={aspect === opt.id ? "filled" : "outlined"}
                        onClick={() => setAspect(opt.id)}
                        sx={{ height: "auto", py: 0.5 }}
                      />
                    ))}
                  </Stack>
                </>
              )}
            </GlassPanel>
          )}

          {/* Transition between clips */}
          <GlassPanel sx={{ p: 2, borderRadius: 2 }}>
            <Typography variant="caption" sx={{ fontWeight: 700, display: "block", mb: 1 }}>
              Transition between clips
            </Typography>
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              {[
                { id: "none", label: "Hard cut", sub: "Instant — fastest" },
                { id: "crossfade", label: "Crossfade", sub: "Short dissolve" },
              ].map((opt) => (
                <Chip
                  key={opt.id}
                  label={
                    <Stack alignItems="flex-start" sx={{ py: 0.25 }}>
                      <Typography variant="caption" sx={{ fontWeight: 700, lineHeight: 1.2 }}>
                        {opt.label}
                      </Typography>
                      <Typography variant="caption" sx={{ fontSize: 10, opacity: 0.8, lineHeight: 1.1 }}>
                        {opt.sub}
                      </Typography>
                    </Stack>
                  }
                  color={transition === opt.id ? "primary" : "default"}
                  variant={transition === opt.id ? "filled" : "outlined"}
                  onClick={() => setTransition(opt.id)}
                  sx={{ height: "auto", py: 0.5 }}
                />
              ))}
            </Stack>
          </GlassPanel>

          {/* Error */}
          {error && <Alert severity="error" onClose={() => setError("")}>{error}</Alert>}

          {/* Submit moved to the title bar (top-right) so the bottom
              filmstrip never hides it. See the header above. */}

          {/* Running — banner first so the warning lands before the progress bar. */}
          {jobRunning && (
            <>
              <Alert
                severity="warning"
                icon={<WarningAmberIcon />}
                sx={{ "& .MuiAlert-message": { fontWeight: 500 } }}
              >
                Don't leave or refresh this page — clips merging is in progress.
                Closing the page will interrupt the process and you'll need to start over.
              </Alert>
              <GlassPanel sx={{ p: 2.5 }}>
                <Typography variant="body2" sx={{ fontWeight: 600, mb: 1 }}>
                  {job.step || "Working…"}
                </Typography>
                <LinearProgress variant="determinate" value={job.percent || 0} sx={{ borderRadius: 1, height: 8 }} />
                <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.5, display: "block" }}>
                  {Math.round(job.percent || 0)}%
                </Typography>
              </GlassPanel>
            </>
          )}

          {/* Success */}
          {jobSuccess && (
            <Alert severity="success" action={
              <Stack direction="row" spacing={1}>
                <Button
                  size="small"
                  variant="contained"
                  startIcon={<DownloadOutlinedIcon />}
                  href={`/api/tools/download/${jobId}`}
                  target="_blank"
                  rel="noopener"
                >
                  Download
                </Button>
                <Button size="small" onClick={reset}>Merge more</Button>
              </Stack>
            }>
              Done. Your merged video is ready.
            </Alert>
          )}

          {/* Failed */}
          {jobFailed && (
            <Alert severity="error" action={<Button size="small" onClick={reset}>Try again</Button>}>
              {job?.step || terminal?.step || "Job failed"}
            </Alert>
          )}
        </Box>
      </Box>

      {/* Bottom filmstrip — always visible. */}
      <Box sx={{
        flexShrink: 0,
        borderTop: 1, borderColor: "divider",
        bgcolor: (t) => t.palette.mode === "dark" ? "rgba(0,0,0,0.2)" : "rgba(0,0,0,0.02)",
      }}>
        <Stack
          direction="row" alignItems="center" spacing={0.5}
          sx={{ px: 2, pt: 1.5, pb: 0.5 }}
        >
          <Typography variant="overline" sx={{ color: "text.secondary", fontSize: "0.65rem", flexShrink: 0 }}>
            Clips ({clips.length})
          </Typography>
          {clips.length > 0 && (
            <Typography variant="caption" sx={{ color: "text.disabled", fontSize: "0.7rem", ml: 1 }}>
              · drag to reorder
            </Typography>
          )}
        </Stack>
        <Box sx={{
          display: "flex", flexWrap: "nowrap", gap: 1, px: 2, pb: 2, pt: 0.5,
          overflowX: "auto", overflowY: "hidden",
          "&::-webkit-scrollbar": { height: 6 },
          "&::-webkit-scrollbar-thumb": { bgcolor: "divider", borderRadius: 3 },
        }}>
          {clips.length === 0 ? (
            <Box sx={{ py: 3, px: 4, textAlign: "center", width: "100%" }}>
              <Typography variant="caption" sx={{ color: "text.disabled" }}>
                No clips yet — drop or browse above to add some.
              </Typography>
            </Box>
          ) : (
            clips.map((c, i) => {
              const showBefore = reorderIdx !== null
                && reorderOver?.targetIdx === i
                && reorderOver?.side === "before"
                && reorderIdx !== i
              const showAfter = reorderIdx !== null
                && reorderOver?.targetIdx === i
                && reorderOver?.side === "after"
                && reorderIdx !== i
                && i === clips.length - 1
              return (
                <Fragment key={c.id}>
                  {showBefore && <DropIndicator />}
                  <FilmstripCard
                    clip={c}
                    index={i}
                    disabled={editingDisabled}
                    isDragging={reorderIdx === i}
                    onDragStart={onCardDragStart(i)}
                    onDragOver={onCardDragOver(i)}
                    onDrop={onCardDrop(i)}
                    onDragEnd={onCardDragEnd}
                    onPlay={() => setPreviewClip(c)}
                    onRemove={() => removeClip(i)}
                  />
                  {showAfter && <DropIndicator />}
                </Fragment>
              )
            })
          )}
        </Box>
      </Box>

      {/* Play preview dialog */}
      <Dialog
        open={!!previewClip}
        onClose={() => setPreviewClip(null)}
        maxWidth="md"
        slotProps={{ paper: { sx: { bgcolor: "#000" } } }}
      >
        <DialogContent sx={{ p: 0, position: "relative" }}>
          {previewClip && (
            <Box component="video"
              src={previewClip.url}
              controls
              autoPlay
              style={{ display: "block", maxWidth: "80vw", maxHeight: "80vh" }}
            />
          )}
          <IconButton
            onClick={() => setPreviewClip(null)}
            sx={{ position: "absolute", top: 8, right: 8, color: "#fff", bgcolor: "rgba(0,0,0,0.5)", "&:hover": { bgcolor: "rgba(0,0,0,0.7)" } }}
          >
            <CloseIcon />
          </IconButton>
        </DialogContent>
      </Dialog>
    </Box>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Filmstrip card — order badge, first-frame preview, hover play, remove
// button, draggable.
// ────────────────────────────────────────────────────────────────────────────
function FilmstripCard({
  clip, index, disabled, isDragging,
  onDragStart, onDragOver, onDrop, onDragEnd, onPlay, onRemove,
}) {
  const fileLabel = useMemo(() => clip.file.name, [clip.file.name])
  const sizeLabel = useMemo(
    () => `${(clip.file.size / 1024 / 1024).toFixed(1)} MB`,
    [clip.file.size],
  )

  return (
    <GlassCard
      hover={false}
      cheap
      draggable={!disabled}
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDrop={onDrop}
      onDragEnd={onDragEnd}
      sx={{
        width: CARD_W, minWidth: CARD_W, flexShrink: 0,
        cursor: disabled ? "default" : "grab",
        "&:active": { cursor: disabled ? "default" : "grabbing" },
        border: 2,
        borderColor: "transparent",
        borderRadius: 1.5,
        overflow: "hidden",
        opacity: isDragging ? 0.4 : 1,
        transition: "all 0.15s ease",
        "&:hover .filmstrip-overlay": { opacity: 1 },
      }}
    >
      {/* Thumbnail (first frame via <video preload="metadata"/>). */}
      <Box data-drag-ghost sx={{ width: "100%", aspectRatio: "9/16", position: "relative", bgcolor: "#000" }}>
        <Box
          component="video"
          src={clip.url}
          preload="metadata"
          muted
          playsInline
          onLoadedMetadata={(e) => { try { e.currentTarget.currentTime = 0.1 } catch { /* noop */ } }}
          sx={{ width: "100%", height: "100%", objectFit: "cover", display: "block", pointerEvents: "none" }}
        />

        {/* Order number badge — top-left */}
        <Chip
          label={index + 1}
          size="small"
          color="primary"
          sx={{
            position: "absolute", top: 4, left: 4,
            height: 22, minWidth: 22, fontWeight: 800, fontSize: "0.75rem",
            "& .MuiChip-label": { px: 0.75 },
          }}
        />

        {/* Drag handle hint — bottom-left */}
        {!disabled && (
          <DragIndicatorIcon sx={{
            position: "absolute", bottom: 4, left: 4,
            fontSize: 18, color: "rgba(255,255,255,0.7)",
            filter: "drop-shadow(0 1px 2px rgba(0,0,0,0.6))",
            pointerEvents: "none",
          }} />
        )}

        {/* Hover overlay with centered play button. */}
        <Box
          className="filmstrip-overlay"
          onClick={(e) => { e.stopPropagation(); onPlay() }}
          sx={{
            position: "absolute", inset: 0,
            display: "flex", alignItems: "center", justifyContent: "center",
            opacity: 0, transition: "opacity 0.15s",
            bgcolor: "rgba(0,0,0,0.35)",
            cursor: "pointer",
          }}
        >
          <PlayCircleOutlineIcon sx={{ fontSize: 40, color: "#fff" }} />
        </Box>

        {/* Remove button — top-right. */}
        {!disabled && (
          <IconButton
            size="small"
            onMouseDown={(e) => e.stopPropagation()}
            onClick={(e) => { e.stopPropagation(); onRemove() }}
            sx={{
              position: "absolute", top: 4, right: 4, zIndex: 2,
              p: 0.5,
              bgcolor: "rgba(0,0,0,0.6)",
              color: "#fff",
              "&:hover": { bgcolor: "rgba(0,0,0,0.85)" },
            }}
          >
            <CloseIcon sx={{ fontSize: 20 }} />
          </IconButton>
        )}
      </Box>

      {/* Single-line footer. */}
      <Box sx={{ p: 1 }}>
        <Tooltip title={`${fileLabel} · ${sizeLabel}`} placement="top" enterDelay={400}>
          <Typography
            variant="caption"
            sx={{
              display: "-webkit-box",
              WebkitLineClamp: 2,
              WebkitBoxOrient: "vertical",
              overflow: "hidden",
              fontWeight: 600,
              fontSize: "0.7rem",
              lineHeight: 1.2,
            }}
          >
            {fileLabel}
          </Typography>
        </Tooltip>
      </Box>
    </GlassCard>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Drop indicator — thin vertical bar that renders BETWEEN cards while a drag
// is in progress, so the user sees exactly where the dropped clip will land.
// ────────────────────────────────────────────────────────────────────────────
function DropIndicator() {
  return (
    <Box
      sx={{
        flexShrink: 0,
        width: 4,
        my: 1,
        borderRadius: 2,
        bgcolor: "primary.main",
        boxShadow: (t) => `0 0 8px ${alpha(t.palette.primary.main, 0.6)}`,
        animation: "dropPulse 1s ease-in-out infinite",
        "@keyframes dropPulse": {
          "0%, 100%": { opacity: 0.7 },
          "50%": { opacity: 1 },
        },
      }}
    />
  )
}
