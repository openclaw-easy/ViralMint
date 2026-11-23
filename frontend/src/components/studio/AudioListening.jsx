// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
// The editor of a "My audio" project that has no scenes yet: the app is listening to the recording
// (a local job), or that listening ended without scenes. It reads `derived.audio_listening`,
// `audio_job_id` and `audio_failed` from the project envelope.
import { Box, Button, LinearProgress, Stack, Typography } from "@mui/material"
import GraphicEqIcon from "@mui/icons-material/GraphicEqOutlined"
import useJobWatch from "./useJobWatch"
import { audioListening, fmtSeconds } from "./audioMode"

export const LISTENING_NOTE = "every sentence becomes a scene, in your own voice."

export default function AudioListening({ project, retrying = false, onRetry, onStartOver }) {
  const audio = project?.doc?.input?.audio || {}
  const listening = audioListening(project)
  const job = useJobWatch(listening ? project?.audio_job_id : null)
  const failed = project?.audio_failed || {}
  const about = `${audio.title || "Your audio"}${audio.duration ? ` · ${fmtSeconds(audio.duration)}` : ""}`
  const pct = Math.max(0, Math.min(100, Number(job?.pct) || 0))
  return (
    <Stack alignItems="center" justifyContent="center" spacing={1.5} sx={{ height: "100%", minHeight: 320, px: 3, textAlign: "center" }}
      data-testid={listening ? "audio-listening" : "audio-failed"}>
      <GraphicEqIcon color={listening ? "primary" : "disabled"} sx={{ fontSize: 40 }} />
      <Typography variant="h6" component="h1" sx={{ fontWeight: 800 }}>{listening ? "Listening to your audio…" : "Listening didn't finish"}</Typography>
      <Typography variant="body2" sx={{ color: "text.secondary" }}>{about}</Typography>
      {listening ? (
        <Box sx={{ width: "min(420px, 90%)" }}>
          <LinearProgress aria-label="Listening progress" variant={pct > 0 ? "determinate" : "indeterminate"} value={pct} />
          <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mt: 1 }}>
            {job?.step || "Opening your audio"} — {LISTENING_NOTE}
          </Typography>
        </Box>
      ) : (
        <>
          <Typography variant="body2" sx={{ maxWidth: 440 }}>{failed.message || "Listening didn't finish. Try again."}</Typography>
          <Stack direction="row" spacing={1}>
            {failed.retry !== false && (
              <Button variant="contained" disabled={retrying} onClick={onRetry} sx={{ textTransform: "none", fontWeight: 700 }}>Try again</Button>
            )}
            <Button variant={failed.retry === false ? "contained" : "text"} onClick={onStartOver} sx={{ textTransform: "none" }}>Use another recording</Button>
          </Stack>
        </>
      )}
    </Stack>
  )
}
