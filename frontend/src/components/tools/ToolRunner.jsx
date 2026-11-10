import { useState, useRef, useCallback, useEffect, useMemo } from "react"
import {
  Box, Typography, Stack, Button, LinearProgress, Alert, IconButton,
  Paper, Slide, ButtonBase, Tooltip,
} from "@mui/material"
import CloudUploadOutlinedIcon from "@mui/icons-material/CloudUploadOutlined"
import InsertDriveFileOutlinedIcon from "@mui/icons-material/InsertDriveFileOutlined"
import DownloadOutlinedIcon from "@mui/icons-material/DownloadOutlined"
import CloseIcon from "@mui/icons-material/Close"
import CheckCircleOutlinedIcon from "@mui/icons-material/CheckCircleOutlined"
import AutoAwesomeOutlinedIcon from "@mui/icons-material/AutoAwesomeOutlined"
import ChevronRightIcon from "@mui/icons-material/ChevronRight"
import ChevronLeftIcon from "@mui/icons-material/ChevronLeft"
import WarningAmberIcon from "@mui/icons-material/WarningAmber"
import http from "../../api/http"
import useAppStore from "../../store/appStore"
import { toolJobTypeFor } from "./toolJobType"
import ToolInputContext from "./ToolInputContext"
import PageHero from "../PageHero"
import { GlassPanel, glassPanelSx } from "../../utils/glassFx"
import CancelJobButton from "./CancelJobButton"

// Default result preview — used when a tool doesn't pass its own
// `resultPreview`. Probes the cheap /download-meta endpoint for the output's
// content-type, then renders the right inline element (video / audio / image /
// text) pointed at the streaming /download URL. Before this, ~22 tools showed
// only a generic "file ready" message — you had to download to see the result.
function AutoPreview({ jobId }) {
  const [meta, setMeta] = useState(null)
  const [text, setText] = useState(null)
  const downloadUrl = `/api/tools/download/${jobId}`

  useEffect(() => {
    let alive = true
    if (!jobId) return
    setMeta(null); setText(null)
    http.get(`/api/tools/download-meta/${jobId}`)
      .then(async ({ data }) => {
        if (!alive) return
        setMeta(data)
        const ct = data?.content_type || ""
        if (data?.ready && (ct.startsWith("text/") || ct === "application/json")) {
          try {
            const r = await http.get(downloadUrl, { responseType: "text" })
            if (alive) setText(typeof r.data === "string" ? r.data : JSON.stringify(r.data, null, 2))
          } catch { /* fall through to "ready" */ }
        }
      })
      .catch(() => { if (alive) setMeta({ ready: false }) })
    return () => { alive = false }
  }, [jobId, downloadUrl])

  const ct = meta?.content_type || ""
  const ready = meta?.ready

  if (ready && ct.startsWith("video/")) {
    return (
      <GlassPanel sx={{ p: 1, borderRadius: 2 }}>
        <Box component="video" src={downloadUrl} controls playsInline
          sx={{ display: "block", width: "100%", maxHeight: "70vh", borderRadius: 1.5, bgcolor: "#000" }} />
      </GlassPanel>
    )
  }
  if (ready && ct.startsWith("image/")) {
    return (
      <GlassPanel sx={{ p: 1, borderRadius: 2 }}>
        <Box component="img" src={downloadUrl} alt="Result"
          sx={{ display: "block", width: "100%", borderRadius: 1.5 }} />
      </GlassPanel>
    )
  }
  if (ready && ct.startsWith("audio/")) {
    return (
      <Stack spacing={1.5} sx={{ py: 3, px: 1 }} alignItems="center">
        <CheckCircleOutlinedIcon sx={{ fontSize: 36, color: "success.main" }} />
        <Box component="audio" src={downloadUrl} controls sx={{ width: "100%" }} />
      </Stack>
    )
  }
  if (ready && text !== null) {
    return (
      <GlassPanel sx={{ p: 0, borderRadius: 2, overflow: "hidden" }}>
        <Box component="pre" sx={{
          m: 0, p: 1.5, fontSize: "0.78rem", lineHeight: 1.5,
          whiteSpace: "pre-wrap", wordBreak: "break-word",
          maxHeight: "60vh", overflow: "auto",
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        }}>
          {text}
        </Box>
      </GlassPanel>
    )
  }
  // Loading or unknown type — the generic confirmation (download still works).
  return (
    <Stack alignItems="center" spacing={1.5} sx={{ py: 6, px: 2 }}>
      <CheckCircleOutlinedIcon sx={{ fontSize: 48, color: "success.main" }} />
      <Typography variant="body1" sx={{ fontWeight: 600 }}>Your file is ready</Typography>
      <Typography variant="caption" sx={{ color: "text.secondary", textAlign: "center" }}>
        Download it below, or run the tool again with new settings.
      </Typography>
    </Stack>
  )
}

export default function ToolRunner({
  title,
  description,
  icon,
  endpoint,
  acceptExts = ".mp4,.mov,.mkv,.webm,.m4v",
  maxMB = 1000,
  extraFiles = {},     // { fieldName: File } — e.g. { logo: File }
  fieldBuilder,        // () => Record<string, string|number|boolean> sent as form fields
  canSubmit = true,
  children,            // tool-specific config UI
  onJobComplete,       // optional — (jobId) => void
  processLabel = "Process",
  // Optional cost / usage hint string (e.g. "Free", "BYOK"). Rendered as a
  // muted caption in the PageHero secondary row. ToolRunner just forwards the
  // value through.
  price,
  costNote,            // optional — <Box>…</Box> rendered below the config panel
  hideFileInput = false,
  scriptInput,         // voice-over: no file, just script text
  resultPreview,       // optional — (jobId) => ReactNode rendered in the result rail
  downloadHref,        // optional — (jobId) => string. Overrides the default
                       //   `/api/tools/download/{jobId}` URL. Used to switch
                       //   to ?bundle=true when variations > 1 so one click
                       //   downloads a ZIP of all outputs.
  downloadLabel = "Download",  // optional override for the button label
  // Optional human-readable reason the Process button is disabled, shown as
  // a tooltip on hover. Only surfaces when canSubmit is false (a missing
  // file/script reports its own message).
  disabledReason = "",
}) {
  const [file, setFile] = useState(null)
  const [jobId, setJobId] = useState(null)
  const [error, setError] = useState("")
  const [dragging, setDragging] = useState(false)
  // Object URL for the selected file, so the config panel can render the very
  // frame it is about to operate on (Crop draws its box on it). Revoked on
  // change/unmount — an un-revoked blob URL pins the whole file in memory.
  const [filePreviewUrl, setFilePreviewUrl] = useState("")
  // Duration + intrinsic pixel size of the loaded video, read off the mounted
  // <video>'s metadata rather than a second server probe. 0 until it lands,
  // and 0 forever for audio/image inputs — every consumer treats 0 as
  // "unknown" and degrades instead of guessing.
  const [inputDuration, setInputDuration] = useState(0)
  const [inputSize, setInputSize] = useState({ w: 0, h: 0 })
  // Local snapshot of terminal state — the global store auto-removes the job
  // entry shortly after completion, which would blank our UI. Snapshot keeps
  // the download button visible until the user explicitly resets.
  const [terminal, setTerminal] = useState(null)
  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const startJob = useAppStore((s) => s.startJob)
  const activeJobs = useAppStore((s) => s.activeJobs)
  const job = useAppStore((s) => (jobId ? s.activeJobs[jobId] : null))
  const inputRef = useRef(null)
  // Generated output lives in a right-edge rail (a non-modal overlay), not
  // inline — so a tall image / video result never changes the height of the
  // tool page or forces a page scroll, and it never blocks the config the
  // way a modal does. Two states:
  //   resultOpen     — the rail exists at all (false fully dismisses it)
  //   railCollapsed  — true shrinks it to a thin "Result ready" tab pinned
  //                    to the screen edge; click the tab to expand again.
  // Auto-opens (expanded) on success; the hero keeps a "View result" button
  // to bring it back after a full dismiss.
  const [resultOpen, setResultOpen] = useState(false)
  const [railCollapsed, setRailCollapsed] = useState(false)

  useEffect(() => {
    if (job && (job.status === "success" || job.status === "failed") && !terminal) {
      setTerminal({ status: job.status, step: job.step })
    }
  }, [job, terminal])

  // Re-attach to an in-flight job when the user navigates back to a tool
  // page while their job is still running. Without this, ToolRunner's
  // component-local `jobId` is wiped on unmount; the global activeJobs
  // store keeps the job, but coming back to /tools/<x> shows the empty
  // dropzone state — user sees their file is "gone" and there's no
  // progress bar on the page.
  //
  // The job type the backend's `create_job(...)` really uses for this endpoint
  // (see tools/toolJobType.js — mostly derived from the slug, with the few
  // documented exceptions). If exactly one running job matches, re-attach. We
  // deliberately don't restore completed/failed jobs — those auto-clear from
  // the store after they reach terminal and the user has likely moved on.
  useEffect(() => {
    if (jobId || terminal) return
    const expectedJobType = toolJobTypeFor(endpoint)
    if (!expectedJobType) return
    const match = Object.values(activeJobs).find(
      (j) => j.jobType === expectedJobType && j.status === "running"
    )
    if (match) {
      setJobId(match.jobId)
    }
  }, [activeJobs, jobId, terminal, endpoint])

  // After a Cancel: forget the job, keep the inputs so a retry is one click.
  const resetJob = useCallback(() => {
    setJobId(null)
    setTerminal(null)
  }, [])

  const reset = useCallback(() => {
    setFile(null)
    setJobId(null)
    setError("")
    setTerminal(null)
    setResultOpen(false)
    setRailCollapsed(false)
  }, [])

  // Live input preview. The utility tools were render-blind: a selected upload
  // showed a filename and a byte count, so every config panel was a naked
  // FFmpeg form. Mount the real media instead — and publish what the element
  // reports so a tool can size its output honestly.
  useEffect(() => {
    if (!file) { setFilePreviewUrl(""); return }
    const url = URL.createObjectURL(file)
    setFilePreviewUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  // A new input invalidates the old measurements. Cleared on `file` rather
  // than inside handleFile so clearing the input resets them too.
  useEffect(() => { setInputDuration(0); setInputSize({ w: 0, h: 0 }) }, [file])

  const onLoadedMetadata = (e) => {
    const el = e?.currentTarget
    const d = el?.duration
    if (Number.isFinite(d) && d > 0) setInputDuration(d)
    const w = el?.videoWidth || 0
    const h = el?.videoHeight || 0
    if (w > 0 && h > 0) setInputSize({ w, h })
  }

  // `file.type` is not reliable: browsers report "" for .mkv and some .mov
  // files, and those are both in the default accept list. Falling back to the
  // extension keeps the preview (and therefore Crop's canvas) working for
  // them instead of silently showing nothing.
  const isVideoInput = !!file && (
    (file.type || "").startsWith("video/")
    || /\.(mp4|mov|mkv|webm|m4v)$/i.test(file.name || "")
  )

  const toolInput = useMemo(() => ({
    duration: inputDuration,
    width: inputSize.w,
    height: inputSize.h,
    previewUrl: filePreviewUrl,
  }), [inputDuration, inputSize, filePreviewUrl])

  const handleFile = (f) => {
    setError("")
    if (!f) return
    const maxBytes = maxMB * 1024 * 1024
    if (f.size > maxBytes) {
      setError(`File too large (max ${maxMB} MB)`)
      return
    }
    setFile(f)
  }

  const onDrop = (e) => {
    e.preventDefault()
    setDragging(false)
    const f = e.dataTransfer.files?.[0]
    handleFile(f)
  }

  const submit = async () => {
    setError("")
    // Clear any prior terminal result so a previous success doesn't keep
    // jobSuccess "true" while the new job runs, and close the old result
    // rail — the new run will open a fresh one on completion. Inputs (file,
    // prompt, etc.) are deliberately left intact so "generate again with a
    // tweak" works without re-entering everything.
    setTerminal(null)
    setResultOpen(false)
    setRailCollapsed(false)
    const form = new FormData()
    if (!hideFileInput) {
      if (!file) {
        setError("Please select a file")
        return
      }
      form.append("file", file)
    }
    for (const [name, f] of Object.entries(extraFiles)) {
      // Allow `File[]` to send multiple files under the same field name —
      // FastAPI's `list[UploadFile]` parameter receives all of them. Used
      // by AI Image multi-reference, where the same `input_images` field
      // can carry up to 3 reference uploads at once.
      if (Array.isArray(f)) {
        for (const file of f) if (file) form.append(name, file)
      } else if (f) {
        form.append(name, f)
      }
    }
    const fields = fieldBuilder ? fieldBuilder() : {}
    for (const [k, v] of Object.entries(fields)) {
      if (v !== undefined && v !== null) form.append(k, String(v))
    }
    try {
      const { data } = await http.post(endpoint, form, {
        headers: { "Content-Type": "multipart/form-data" },
        timeout: 120000,
      })
      // Seed the appStore entry synchronously. Runners emit job_progress /
      // job_complete events, which updateJobProgress / completeJob no-op if
      // activeJobs[jobId] doesn't already exist.
      //
      // Tag the entry with the specific job type the backend uses (e.g.
      // `tool:auto_chapters`), from the same single source as the re-attach
      // hook above — the two used to derive it independently, which is how
      // /tools/subtitles ended up tagged `tool:subtitles` while the backend
      // created `tool:subtitle_export`.
      startJob(data.job_id, toolJobTypeFor(endpoint), "Starting...")
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

  // Primary-input presence: file tools need a file; script tools (voice-over,
  // AI music) pass `scriptInput`; `hideFileInput` tools drive entirely off
  // fieldBuilder. When the primary input is missing that's the most
  // fundamental block, so it takes priority over a page-specific reason.
  const missingPrimaryInput = !hideFileInput && !file && !scriptInput
  const submitDisabled = !canSubmit || missingPrimaryInput
  const submitBlockedReason = !submitDisabled
    ? ""
    : missingPrimaryInput
      ? "Select a video file first"
      : disabledReason

  // ⌘/Ctrl+Enter runs the tool from anywhere in the page (incl. while a
  // prompt/script textarea is focused) — the standard power-user affordance
  // for prompt boxes. No-ops when the submit is blocked or a job's running.
  const onKeyDownSubmit = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !submitDisabled && !jobRunning) {
      e.preventDefault()
      submit()
    }
  }

  useEffect(() => {
    if (jobSuccess && onJobComplete) {
      try { onJobComplete(jobId) } catch { /* noop */ }
    }
  }, [jobSuccess, jobId, onJobComplete])

  // Auto-open the result rail (expanded) the moment the job lands. Keyed on
  // the jobSuccess transition only — if the user dismisses it while the job
  // is still terminal, it stays closed (reopen via the hero "View result"
  // button) until a fresh run flips jobSuccess again.
  useEffect(() => {
    if (jobSuccess) { setResultOpen(true); setRailCollapsed(false) }
  }, [jobSuccess])

  const reopenResult = () => { setResultOpen(true); setRailCollapsed(false) }

  // Guard browser close / reload while a tool job is in progress. Modern
  // browsers ignore the custom message and show their own generic dialog
  // ("Reload site? Changes you made may not be saved") — but the dialog
  // still appears, which is what we want. Doesn't catch SPA navigation;
  // the on-screen warning Alert below is what nudges users not to use
  // the sidebar mid-run.
  useEffect(() => {
    if (!jobRunning) return
    const handler = (e) => {
      e.preventDefault()
      e.returnValue = ""
      return ""
    }
    window.addEventListener("beforeunload", handler)
    return () => window.removeEventListener("beforeunload", handler)
  }, [jobRunning])

  return (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column" }} onKeyDown={onKeyDownSubmit}>
      {/* Title-bar primary action lives in the PageHero `actions` slot so it
          stays visible on shorter viewports. Hidden during
          running/success/failed states (those have their own affordances in
          the body — progress bar, download CTA, retry). */}
      <PageHero
        backTo="/tools"
        icon={icon && (
          // ToolRunner callers pass full-sized icons (`fontSize="large"`).
          // PageHero renders inside a 40x40 chip-sized container, so
          // wrap and clamp the size for visual parity with other pages.
          <Box sx={{ display: "flex", "& svg": { fontSize: 22 } }}>{icon}</Box>
        )}
        title={title}
        subtitle={description}
        price={price}
        actions={
          // The primary action stays available whenever a job ISN'T running,
          // so the user can immediately generate again (with tweaked inputs)
          // after a result lands — no page refresh needed. When a result
          // exists but its rail was dismissed, a secondary "View result"
          // button reopens it. While one runs the same spot is its Cancel: the header
          // stays on screen, where a long script pushes the progress panel out of view.
          jobRunning ? <CancelJobButton jobId={jobId} onCancelled={resetJob} variant="outlined" /> : (
            <Stack direction="row" spacing={1}>
              {jobSuccess && !resultOpen && (
                <Button
                  variant="outlined"
                  startIcon={<AutoAwesomeOutlinedIcon />}
                  onClick={reopenResult}
                  sx={{ flexShrink: 0 }}
                >
                  View result
                </Button>
              )}
              {/* Tooltip needs a non-disabled wrapper to receive hover
                  events — a disabled MUI Button swallows them — hence the
                  span. Empty title renders no tooltip (enabled state). */}
              <Tooltip title={submitBlockedReason} arrow>
                <span style={{ display: "inline-flex", flexShrink: 0 }}>
                  <Button
                    variant="contained"
                    onClick={submit}
                    disabled={submitDisabled}
                    sx={{ flexShrink: 0, px: 3 }}
                  >
                    {processLabel}
                  </Button>
                </span>
              </Tooltip>
            </Stack>
          )
        }
      />

      {/* Body */}
      {/* Bottom padding (pb: 6 / 48 px) is intentionally larger than the
          rest. With content-rich tools (AI Image with refs + prompt +
          styles + variations, Voiceover with script + voice + instructions),
          the last UI element ends up flush against the viewport edge after
          scroll — a 24 px footer gap doesn't look like padding, it looks
          like the page got cut off. 48 px reads as deliberate breathing
          room. */}
      <Box sx={{ flex: 1, overflow: "auto", px: 3, pt: 3, pb: 6, display: "flex", justifyContent: "center" }}>
        {/* Content cap of 1100 keeps mobile/tablet usable (responsive
            width: 100%) and gives widescreen users room to breathe —
            gallery output, reference row, and config chips all benefit. */}
        <Box sx={{ maxWidth: 1100, width: "100%", display: "flex", flexDirection: "column", gap: 2.5 }}>

          {/* Script input (voice-over) or file dropzone */}
          {scriptInput ? (
            scriptInput
          ) : !hideFileInput && (
            <GlassPanel
              onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
              onClick={() => !file && inputRef.current?.click()}
              sx={{
                p: 4,
                textAlign: "center",
                borderRadius: 2.5,
                borderStyle: "dashed",
                borderWidth: 2,
                borderColor: (t) => dragging
                  ? t.palette.primary.main
                  : (t.palette.mode === "dark" ? "rgba(255,255,255,0.18)" : "rgba(0,0,0,0.16)"),
                cursor: file ? "default" : "pointer",
                transition: "all 0.18s ease",
                "&:hover": file ? undefined : {
                  borderColor: "primary.main",
                  transform: "translateY(-1px)",
                },
              }}
            >
              <input
                ref={inputRef}
                type="file"
                hidden
                accept={acceptExts}
                onChange={(e) => handleFile(e.target.files?.[0])}
              />
              {file ? (
                <Stack spacing={1.5} alignItems="center">
                  <Stack direction="row" spacing={1.5} alignItems="center" justifyContent="center">
                    <InsertDriveFileOutlinedIcon sx={{ color: "primary.main", fontSize: 32 }} />
                    <Box sx={{ textAlign: "left" }}>
                      <Typography variant="body2" sx={{ fontWeight: 600 }}>
                        {file.name}
                      </Typography>
                      <Typography variant="caption" sx={{ color: "text.secondary" }}>
                        {(file.size / 1024 / 1024).toFixed(1)} MB
                        {inputSize.w > 0 && ` · ${inputSize.w}×${inputSize.h}`}
                      </Typography>
                    </Box>
                    <IconButton size="small" onClick={(e) => { e.stopPropagation(); setFile(null) }}>
                      <CloseIcon fontSize="small" />
                    </IconButton>
                  </Stack>
                  {/* The mounted element is also where duration + intrinsic
                      size come from (onLoadedMetadata), so it stays in the
                      tree for video inputs even when a tool draws its own
                      preview from `previewUrl`. */}
                  {isVideoInput && filePreviewUrl && (
                    <Box
                      component="video"
                      src={filePreviewUrl}
                      controls
                      playsInline
                      preload="metadata"
                      onLoadedMetadata={onLoadedMetadata}
                      onClick={(e) => e.stopPropagation()}
                      sx={{
                        display: "block", width: "100%", maxHeight: 260,
                        borderRadius: 1.5, bgcolor: "#000",
                      }}
                    />
                  )}
                </Stack>
              ) : (
                <Stack spacing={1} alignItems="center">
                  <CloudUploadOutlinedIcon sx={{ fontSize: 40, color: "text.secondary" }} />
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>
                    Drop a video here or click to browse
                  </Typography>
                  <Typography variant="caption" sx={{ color: "text.secondary" }}>
                    {acceptExts.replace(/\./g, "").toUpperCase()} · up to {maxMB} MB
                  </Typography>
                </Stack>
              )}
            </GlassPanel>
          )}

          {/* Tool-specific config. Wrapped in ToolInputContext so a tool's
              own controls can size themselves against the real input instead
              of assuming one. */}
          {children && (
            <GlassPanel sx={{ p: 2.5 }}>
              <ToolInputContext.Provider value={toolInput}>
                {children}
              </ToolInputContext.Provider>
            </GlassPanel>
          )}

          {/* Error */}
          {error && <Alert severity="error" onClose={() => setError("")}>{error}</Alert>}

          {/* Cost / usage preview */}
          {costNote}

          {/* Action button lives in the title bar (top-right). See header above. */}

          {/* Running — banner first so the warning lands before the progress bar. */}
          {jobRunning && (
            <>
              <Alert
                severity="warning"
                icon={<WarningAmberIcon />}
                sx={{ "& .MuiAlert-message": { fontWeight: 500 } }}
              >
                Don't leave or refresh this page — the job is in progress.
                Closing the page will interrupt it and you'll need to start over.
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

          {/* Success output is shown in the right-edge result rail below — it
              is deliberately NOT rendered inline so generating a result never
              changes the height of the tool page. */}

          {/* Failed */}
          {jobFailed && (
            <Alert severity="error" action={<Button size="small" onClick={reset}>Try again</Button>}>
              {job?.step || terminal?.step || "Job failed"}
            </Alert>
          )}
        </Box>
      </Box>

      {/* Result rail — a non-modal panel docked to the RIGHT edge holding the
          generated output (image / video / audio preview when the tool gives
          one, else a simple "ready" confirmation). A tall preview scrolls
          INSIDE the rail, so the page underneath keeps its layout + height and
          stays fully interactive. Slides in (expanded) on success; the
          chevron minimizes it to a thin edge tab (below) that persists so the
          result is never lost; ✕ fully dismisses (reopen via hero). */}
      <Slide direction="left" in={resultOpen && jobSuccess && !railCollapsed} mountOnEnter unmountOnExit>
        <Paper
          elevation={0}
          sx={(t) => ({
            ...glassPanelSx(t),
            position: "fixed",
            top: 0, right: 0, bottom: 0,
            width: { xs: "100%", sm: 460 },
            maxWidth: "100vw",
            zIndex: t.zIndex.drawer + 2,
            display: "flex",
            flexDirection: "column",
            borderRadius: 0,
            borderTop: 0,
            borderBottom: 0,
            borderRight: 0,
            boxShadow: "-14px 0 44px rgba(0,0,0,0.20)",
          })}
        >
          {/* Header */}
          <Stack
            direction="row"
            alignItems="center"
            spacing={1}
            sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: "divider", flexShrink: 0 }}
          >
            <CheckCircleOutlinedIcon sx={{ color: "success.main" }} />
            <Typography variant="subtitle1" sx={{ flex: 1, fontWeight: 700, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {title} — ready
            </Typography>
            <Tooltip title="Minimize to edge">
              <IconButton size="small" onClick={() => setRailCollapsed(true)} aria-label="Minimize result">
                <ChevronRightIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <Tooltip title="Close">
              <IconButton size="small" onClick={() => setResultOpen(false)} aria-label="Close result">
                <CloseIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </Stack>

          {/* Content — scrolls inside the rail */}
          <Box sx={{ flex: 1, overflow: "auto", p: 2 }}>
            {resultPreview ? (
              resultPreview(jobId)
            ) : (
              <AutoPreview jobId={jobId} />
            )}
          </Box>

          {/* Footer actions */}
          <Stack direction="row" spacing={1} sx={{ p: 2, borderTop: 1, borderColor: "divider", flexShrink: 0 }}>
            <Button fullWidth variant="outlined" onClick={reset}>Run again</Button>
            <Button
              fullWidth
              variant="contained"
              startIcon={<DownloadOutlinedIcon />}
              href={jobId ? (downloadHref ? downloadHref(jobId) : `/api/tools/download/${jobId}`) : undefined}
              target="_blank"
              rel="noopener"
            >
              {downloadLabel}
            </Button>
          </Stack>
        </Paper>
      </Slide>

      {/* Collapsed edge tab — persistent handle on the right edge. Vertical
          "Result ready" label; click to expand the rail back open. */}
      {resultOpen && jobSuccess && railCollapsed && (
        <Tooltip title="Show generated result" placement="left">
          <ButtonBase
            onClick={() => setRailCollapsed(false)}
            aria-label="Show generated result"
            sx={(t) => ({
              position: "fixed",
              right: 0,
              top: "50%",
              transform: "translateY(-50%)",
              zIndex: t.zIndex.drawer + 2,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 0.75,
              py: 1.75,
              px: 0.65,
              color: "#fff",
              background: `linear-gradient(135deg, ${t.palette.primary.main}, #e88a5a)`,
              borderTopLeftRadius: 12,
              borderBottomLeftRadius: 12,
              boxShadow: "-5px 0 18px rgba(0,0,0,0.24)",
              transition: "filter 0.15s ease",
              "&:hover": { filter: "brightness(1.06)" },
            })}
          >
            <ChevronLeftIcon fontSize="small" />
            <CheckCircleOutlinedIcon sx={{ fontSize: 16 }} />
            <Typography
              sx={{
                writingMode: "vertical-rl",
                fontWeight: 700,
                fontSize: "0.78rem",
                letterSpacing: "0.04em",
              }}
            >
              Result ready
            </Typography>
          </ButtonBase>
        </Tooltip>
      )}
    </Box>
  )
}
