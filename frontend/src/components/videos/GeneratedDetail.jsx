import { useState } from "react"
import { useNavigate } from "react-router-dom"
import {
  Box, Typography, Button, Chip, Stack, Paper, IconButton, Divider,
  Grid, CircularProgress, TextField, Menu, MenuItem, ListItemText, ListItemIcon,
} from "@mui/material"
import EditIcon from "@mui/icons-material/Edit"
import SaveIcon from "@mui/icons-material/Save"
import DeleteIcon from "@mui/icons-material/Delete"
import UploadIcon from "@mui/icons-material/Upload"
import CloseIcon from "@mui/icons-material/Close"
import AccessTimeIcon from "@mui/icons-material/AccessTime"
import DownloadIcon from "@mui/icons-material/Download"
import AspectRatioIcon from "@mui/icons-material/AspectRatio"
import ContentCutIcon from "@mui/icons-material/ContentCut"
import PhotoCameraIcon from "@mui/icons-material/PhotoCamera"
import WhatshotIcon from "@mui/icons-material/Whatshot"
import WarningAmberIcon from "@mui/icons-material/WarningAmber"
import MovieEditIcon from "@mui/icons-material/MovieEdit"
import http from "../../api/http"
import useAppStore from "../../store/appStore"
import { STATUS_COLOR } from "./constants"
import { errorDetailText } from "../../utils/errorDetail"

const EXPORT_FORMATS = [
  { aspect: "9:16", label: "9:16 Vertical", desc: "TikTok, Reels, Shorts" },
  { aspect: "16:9", label: "16:9 Landscape", desc: "YouTube, Web" },
  { aspect: "1:1",  label: "1:1 Square", desc: "Instagram, Facebook" },
  { aspect: "4:5",  label: "4:5 Portrait", desc: "Instagram Feed" },
]

// `embedded` — rendered inside the Library drawer, whose header already
// shows the title, origin, duration and date and has its own close button.
// The body then drops its own card frame, title and close, and stacks into
// one column at the drawer's width instead of a page-width two-column grid.
export default function GeneratedDetail({ video, onUpload, onDelete, onClose, onDetailUpdate, embedded = false }) {
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [regenThumb, setRegenThumb] = useState(false)
  const [draft, setDraft] = useState({})
  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const navigate = useNavigate()

  const handleRegenThumbnail = async () => {
    setRegenThumb(true)
    try {
      const res = await http.post(`/api/videos/${video.id}/regenerate-thumbnail`)
      showSnackbar("Thumbnail regenerated!", "success")
      if (onDetailUpdate) onDetailUpdate({ ...video, thumbnail_path: res.data.thumbnail_path })
    } catch (e) {
      showSnackbar(`Thumbnail regen failed: ${e.response?.data?.detail || e.message}`, "error")
    } finally {
      setRegenThumb(false)
    }
  }

  const startEditing = () => {
    setDraft({
      title: video.title || "",
      script: video.script || "",
      youtube_title: video.youtube_title || "",
      youtube_description: video.youtube_description || "",
      youtube_tags: (video.youtube_tags || []).join(", "),
      tiktok_title: video.tiktok_title || "",
      tiktok_description: video.tiktok_description || "",
    })
    setEditing(true)
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const payload = { ...draft }
      // Convert comma-separated tags string to array
      payload.youtube_tags = payload.youtube_tags
      const res = await http.patch(`/api/videos/${video.id}`, payload)
      showSnackbar("Video metadata updated", "success")
      setEditing(false)
      if (onDetailUpdate) onDetailUpdate({ ...video, ...res.data, youtube_tags: res.data.youtube_tags })
    } catch (e) {
      showSnackbar(`Save failed: ${errorDetailText(e)}`, "error")
    } finally {
      setSaving(false)
    }
  }

  const handleCancel = () => { setEditing(false); setDraft({}) }
  const setField = (field, value) => setDraft(prev => ({ ...prev, [field]: value }))

  // Export / multi-format download
  const [exportAnchor, setExportAnchor] = useState(null)
  const [exporting, setExporting] = useState(false)

  const handleExport = async (targetAspect) => {
    setExportAnchor(null)
    if (targetAspect === video.aspect_ratio) {
      // Same aspect — just download the original
      window.open(`/api/videos/${video.id}/stream`, "_blank")
      return
    }
    setExporting(true)
    try {
      // No `method` — the backend picks it ("auto"): crop when widening,
      // blur_fill when narrowing. Hardcoding blur_fill here fitted a 9:16
      // short inside a 16:9 frame, leaving the picture a narrow strip.
      const res = await http.post(`/api/videos/${video.id}/export`, {
        target_aspect: targetAspect,
      }, { responseType: "blob" })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement("a")
      a.href = url
      a.download = `${(video.title || "video").replace(/[^a-zA-Z0-9]/g, "_")}_${targetAspect.replace(":", "x")}.mp4`
      a.click()
      URL.revokeObjectURL(url)
      showSnackbar(`Exported as ${targetAspect}`, "success")
    } catch (e) {
      showSnackbar(`Export failed: ${e.response?.data?.detail || e.message}`, "error")
    } finally {
      setExporting(false)
    }
  }

  return (
    <Paper
      elevation={0}
      data-testid={"generated-detail"}
      // Embedded, the drawer IS the card: a second 2 px bordered card inside it
      // drew a hard frame around the whole body (harshest in dark mode).
      sx={embedded ? {
        p: 0, bgcolor: "transparent", border: 0, borderRadius: 0,
      } : {
        mx: -0.5, mb: 1.5, p: 0,
        border: 2, borderColor: "secondary.main",
        borderRadius: 3,
        overflow: "hidden",
        bgcolor: "background.paper",
      }}
    >
      {/* Header bar */}
      <Stack direction="row" alignItems="center" justifyContent="space-between"
        sx={embedded ? { pb: 1.5 } : { px: 2.5, py: 1.5, bgcolor: "action.hover" }}>
        {!embedded && (
          <Typography variant="subtitle1" sx={{ fontWeight: 600, flex: 1, minWidth: 0 }} noWrap>
            {video.title || "Untitled"}
          </Typography>
        )}
        <Stack direction="row" spacing={embedded ? 0 : 1} useFlexGap
          flexWrap={embedded ? "wrap" : "nowrap"}
          sx={embedded ? { gap: 1, flex: 1, minWidth: 0, alignItems: "center" } : { flexShrink: 0 }}>
          <Chip label={video.status} size="small" color={STATUS_COLOR[video.status] || "default"} />
          {editing ? (
            <>
              <Button variant="contained" size="small" startIcon={saving ? <CircularProgress size={16} /> : <SaveIcon />}
                onClick={handleSave} disabled={saving}>
                Save
              </Button>
              <Button variant="outlined" size="small" onClick={handleCancel} disabled={saving}>
                Cancel
              </Button>
            </>
          ) : (
            <>
              <Button variant="outlined" size="small" startIcon={<EditIcon />}
                onClick={startEditing}>
                Edit
              </Button>
              {video.project_id && (
                // A Smart Video render: its project is still editable —
                // change a line or a clip and render again.
                <Button variant="outlined" size="small" startIcon={<MovieEditIcon />}
                  onClick={() => navigate(`/stock/p/${video.project_id}`)}>
                  Edit in studio
                </Button>
              )}
              <Button variant="outlined" size="small"
                startIcon={regenThumb ? <CircularProgress size={16} /> : <PhotoCameraIcon />}
                disabled={regenThumb || !video.video_path}
                onClick={handleRegenThumbnail}>
                {regenThumb ? "Generating..." : "Regen Thumbnail"}
              </Button>
              {video.status === "ready" && (
                <>
                  <Button variant="contained" size="small" color="error" startIcon={<UploadIcon />}
                    onClick={() => onUpload(video.id, ["youtube"])}>
                    YouTube
                  </Button>
                  <Button variant="contained" size="small" color="info" startIcon={<UploadIcon />}
                    onClick={() => onUpload(video.id, ["tiktok"])}>
                    TikTok
                  </Button>
                </>
              )}
              {video.video_path && (
                <Button variant="outlined" size="small" color="success"
                  startIcon={exporting ? <CircularProgress size={16} /> : <AspectRatioIcon />}
                  onClick={e => setExportAnchor(e.currentTarget)}
                  disabled={exporting}>
                  {exporting ? "Converting..." : "Export"}
                </Button>
              )}
              <Menu anchorEl={exportAnchor} open={Boolean(exportAnchor)}
                onClose={() => setExportAnchor(null)}
                slotProps={{ paper: { sx: { minWidth: 220, borderRadius: 2 } } }}>
                {EXPORT_FORMATS.map(f => (
                  <MenuItem key={f.aspect} onClick={() => handleExport(f.aspect)}
                    selected={f.aspect === video.aspect_ratio}>
                    <ListItemIcon><DownloadIcon fontSize="small" /></ListItemIcon>
                    <ListItemText
                      primary={f.label}
                      secondary={f.aspect === video.aspect_ratio ? "Current — download original" : f.desc}
                      slotProps={{ primary: { fontSize: "0.85rem" }, secondary: { fontSize: "0.72rem" } }}
                    />
                  </MenuItem>
                ))}
              </Menu>
              <Button variant="outlined" size="small" color="inherit" startIcon={<DeleteIcon />}
                onClick={() => onDelete(video.id)}>
                Delete
              </Button>
            </>
          )}
        </Stack>
        {!embedded && (
          <IconButton size="small" onClick={onClose} sx={{ ml: 1 }} aria-label="Close">
            <CloseIcon fontSize="small" />
          </IconButton>
        )}
      </Stack>

      {/* Main content */}
      <Box sx={{ p: embedded ? 0 : 2.5 }}>
        <Grid container spacing={3}>
          {/* Left column: Video player + meta chips */}
          <Grid size={{ xs: 12, md: embedded ? 12 : 4 }}>
            {video.video_path ? (
              <Box
                component="video"
                controls
                sx={{
                  width: "100%", borderRadius: 2,
                  bgcolor: "#000", display: "block", mx: "auto", objectFit: "contain",
                  maxHeight: 480,
                }}
                src={`/api/videos/${video.id}/stream`}
              />
            ) : (
              <Box sx={{ height: 200, borderRadius: 2, bgcolor: "action.hover", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <Typography sx={{ color: "text.disabled" }}>Video not yet generated</Typography>
              </Box>
            )}

            {/* Meta chips */}
            <Stack direction="row" spacing={1} sx={{ mt: 1.5 }} flexWrap="wrap" useFlexGap>
              <Chip
                label={video.source_type === "clip_extraction" ? "Clip" : "Stock"}
                size="small" variant={video.source_type === "clip_extraction" ? "filled" : "outlined"}
                color={video.source_type === "clip_extraction" ? "secondary" : "default"}
                icon={video.source_type === "clip_extraction" ? <ContentCutIcon /> : undefined} />
              {!embedded && video.aspect_ratio && <Chip label={video.aspect_ratio} size="small" variant="outlined" />}
              {!embedded && video.duration_seconds && (
                <Chip icon={<AccessTimeIcon />} label={`${Math.floor(video.duration_seconds / 60)}m${video.duration_seconds % 60}s`}
                  size="small" variant="outlined" />
              )}
              {video.clip_virality_score != null && (
                <Chip icon={<WhatshotIcon />}
                  label={`Virality ${video.clip_virality_score.toFixed(1)}/10`}
                  size="small" variant="filled"
                  color={video.clip_virality_score >= 8 ? "success" : video.clip_virality_score >= 6 ? "warning" : "default"} />
              )}
              {video.caption_status === "failed" && (
                <Chip icon={<WarningAmberIcon />} label="Captions failed" size="small" color="warning" variant="filled" />
              )}
              {video.metadata_status === "fallback" && (
                <Chip icon={<WarningAmberIcon />} label="AI metadata failed" size="small" color="warning" variant="outlined" />
              )}
              {video.estimated_cost_usd > 0 && (
                <Chip label={`$${video.estimated_cost_usd.toFixed(2)}`} size="small" variant="outlined" />
              )}
              {!embedded && video.created_at && (
                <Typography variant="caption" sx={{ color: "text.secondary", alignSelf: "center" }}>
                  {new Date(video.created_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                </Typography>
              )}
            </Stack>

            {/* Clip source context */}
            {video.source_type === "clip_extraction" && (
              <Paper variant="outlined" sx={{ mt: 1.5, p: 1.5, borderRadius: 2, bgcolor: "action.hover" }}>
                <Stack spacing={0.75}>
                  {video.clip_start_seconds != null && video.clip_end_seconds != null && (
                    <Typography variant="caption" sx={{ color: "text.secondary" }}>
                      Source: {Math.floor(video.clip_start_seconds / 60)}:{String(Math.floor(video.clip_start_seconds % 60)).padStart(2, "0")}
                      {" — "}
                      {Math.floor(video.clip_end_seconds / 60)}:{String(Math.floor(video.clip_end_seconds % 60)).padStart(2, "0")}
                      {" in original video"}
                    </Typography>
                  )}
                  {video.clip_virality_reason && (
                    <Typography variant="caption" sx={{ color: "text.secondary", fontStyle: "italic" }}>
                      {video.clip_virality_reason}
                    </Typography>
                  )}
                </Stack>
              </Paper>
            )}
          </Grid>

          {/* Right column: Script + Platform metadata (editable) */}
          <Grid size={{ xs: 12, md: embedded ? 12 : 8 }}>
            {editing ? (
              <Stack spacing={2}>
                <TextField label="Title" size="small" fullWidth
                  value={draft.title} onChange={e => setField("title", e.target.value)} />
                <TextField label="Script" size="small" fullWidth multiline minRows={4} maxRows={12}
                  value={draft.script} onChange={e => setField("script", e.target.value)} />
                <Divider />
                <Typography variant="overline" sx={{ color: "text.secondary" }}>YouTube</Typography>
                <TextField label="YouTube Title" size="small" fullWidth
                  value={draft.youtube_title} onChange={e => setField("youtube_title", e.target.value)} />
                <TextField label="YouTube Description" size="small" fullWidth multiline minRows={2} maxRows={6}
                  value={draft.youtube_description} onChange={e => setField("youtube_description", e.target.value)} />
                <TextField label="YouTube Tags (comma-separated)" size="small" fullWidth
                  value={draft.youtube_tags} onChange={e => setField("youtube_tags", e.target.value)}
                  helperText="Separate tags with commas" />
                <Divider />
                <Typography variant="overline" sx={{ color: "text.secondary" }}>TikTok</Typography>
                <TextField label="TikTok Title" size="small" fullWidth
                  value={draft.tiktok_title} onChange={e => setField("tiktok_title", e.target.value)} />
                <TextField label="TikTok Description" size="small" fullWidth multiline minRows={2} maxRows={4}
                  value={draft.tiktok_description} onChange={e => setField("tiktok_description", e.target.value)} />
              </Stack>
            ) : (
              <>
                {video.script && (
                  <Box sx={{ mb: 2 }}>
                    <Typography variant="overline" sx={{ color: "text.secondary" }}>Script</Typography>
                    <Paper variant="outlined" sx={{ p: 1.5, maxHeight: 320, overflowY: "auto", fontSize: "0.82rem", color: "text.secondary", lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
                      {video.script}
                    </Paper>
                  </Box>
                )}

                {video.youtube_title && (
                  <Box sx={{ mb: 2 }}>
                    <Typography variant="overline" sx={{ color: "text.secondary" }}>YouTube</Typography>
                    <Typography variant="body2" sx={{ fontWeight: 600, fontSize: "0.9rem" }}>{video.youtube_title}</Typography>
                    <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mt: 0.25 }}>
                      {video.youtube_description?.slice(0, 200)}{video.youtube_description?.length > 200 ? "..." : ""}
                    </Typography>
                    {video.youtube_tags?.length > 0 && (
                      <Stack direction="row" spacing={0.5} sx={{ mt: 0.5 }} flexWrap="wrap" useFlexGap>
                        {video.youtube_tags.map((tag, i) => (
                          <Chip key={i} label={tag} size="small" variant="outlined" sx={{ height: 20, fontSize: "0.65rem" }} />
                        ))}
                      </Stack>
                    )}
                  </Box>
                )}

                {video.tiktok_title && (
                  <Box sx={{ mb: 2 }}>
                    <Typography variant="overline" sx={{ color: "text.secondary" }}>TikTok</Typography>
                    <Typography variant="body2" sx={{ fontWeight: 600, fontSize: "0.9rem" }}>{video.tiktok_title}</Typography>
                    {video.tiktok_description && (
                      <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mt: 0.25 }}>
                        {video.tiktok_description}
                      </Typography>
                    )}
                  </Box>
                )}

                {!video.script && !video.youtube_title && !video.tiktok_title && (
                  <Box sx={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", minHeight: 200, color: "text.disabled" }}>
                    <Typography variant="body2" sx={{ mb: 1 }}>No script or metadata yet</Typography>
                    <Typography variant="caption">This video is still being generated</Typography>
                  </Box>
                )}
              </>
            )}
          </Grid>
        </Grid>
      </Box>
    </Paper>
  )
}
