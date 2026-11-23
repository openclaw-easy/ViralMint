// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import {
  Box, Button, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Typography,
} from "@mui/material"
import MovieCreationOutlinedIcon from "@mui/icons-material/MovieCreationOutlined"
import { musicLabel, voiceKindLabel } from "./studioModel"
import { AUDIO_VOICE_LABEL, isAudioMode } from "./audioMode"
import { captionStyleLabel } from "../tools/captionOptions"
import { formatClock } from "../../utils/format"

// The one render confirmation: what the video is, what blocks the
// render and what's worth knowing first. Blockers and warnings come from the
// server (`project.checks`), which re-checks them when the render starts.
export default function RenderDialog({ open, onClose, onConfirm, rendering, project, timeline }) {
  if (!project) return null
  const s = project.doc.settings
  const blockers = project.checks?.blockers || []
  const warnings = project.checks?.warnings || []
  const rows = [
    ...(blockers.length ? [{ group: "Fix before rendering", tone: "error.main", items: blockers.map((b) => ["Blocked", b]) }] : []),
    {
      group: "Video",
      items: [
        ["Length", `${formatClock(timeline.total)} · ${timeline.scenes.length} scenes · ${project.doc.aspect_ratio}`],
        // A "My audio" project speaks the user's recording: no voice is used.
        ["Voice", isAudioMode(project.doc) ? AUDIO_VOICE_LABEL : `${s.voice?.label || s.voice?.voice || "—"} · ${voiceKindLabel(s.voice?.provider)}`],
        ["Music", musicLabel(s.music)],
        ["Captions", s.captions?.enabled ? captionStyleLabel(s.captions.style) : "Off"],
      ],
    },
    ...(warnings.length ? [{ group: "Before you render", tone: "warning.main", items: warnings.map((w) => ["Note", w]) }] : []),
  ]
  return (
    <Dialog open={open} onClose={rendering ? undefined : onClose} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ fontWeight: 800, pb: 0.5 }}>Render this video?</DialogTitle>
      <DialogContent>
        <Typography variant="body2" sx={{ color: "text.secondary", mb: 1.5 }}>
          Check the details — the render runs on this computer and usually takes a minute or two.
        </Typography>
        <Stack spacing={1.5}>
          {rows.map((r) => (
            <Box key={r.group}>
              <Typography variant="overline" sx={{ fontWeight: 800, color: r.tone || "text.secondary", lineHeight: 1.6 }}>
                {r.group}
              </Typography>
              {r.items.map(([label, value], i) => (
                <Stack key={`${label}:${i}`} direction="row" spacing={2} justifyContent="space-between" sx={{ py: 0.25 }}>
                  <Typography variant="body2" sx={{ color: "text.secondary", flexShrink: 0 }}>{label}</Typography>
                  <Typography variant="body2" sx={{ fontWeight: 600, textAlign: "right", minWidth: 0 }}>{value}</Typography>
                </Stack>
              ))}
            </Box>
          ))}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onClose} disabled={!!rendering} sx={{ textTransform: "none" }}>Cancel</Button>
        <Button
          variant="contained" onClick={() => onConfirm()} disabled={!!rendering || blockers.length > 0}
          startIcon={rendering ? <CircularProgress size={14} color="inherit" /> : <MovieCreationOutlinedIcon />}
          sx={{ textTransform: "none", fontWeight: 800 }}
        >
          Render
        </Button>
      </DialogActions>
    </Dialog>
  )
}
