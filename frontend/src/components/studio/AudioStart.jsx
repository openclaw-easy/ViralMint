// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import { Alert, Box, CircularProgress, Typography } from "@mui/material"
import GraphicEqIcon from "@mui/icons-material/GraphicEqOutlined"
import { createFromAudio, forgetLastCaptionStyle, readLast } from "./studioApi"

// What the backend takes (studio_doc.AUDIO_MIN_S / AUDIO_MAX_S, studio_audio.MAX_BYTES; pinned by
// tests/test_studio_my_audio.py). Checked there; said here before anything is sent.
export const AUDIO_MIN_S = 3
export const AUDIO_MAX_S = 480
export const AUDIO_MAX_MB = 200
const ACCEPT = ".mp3,.m4a,.aac,.ogg,.flac,.wav,.mp4,.mov,.webm,audio/*,video/*"
export const TOO_LARGE = `This file is over ${AUDIO_MAX_MB} MB. Choose a smaller one.`

// "My audio" on Studio Home: the user's recording becomes an editable PROJECT whose scenes speak in
// it. One step: drop a file and the editor opens while the app listens — one scene per sentence.
export default function AudioStart({ aspect, style }) {
  const navigate = useNavigate()
  const inputRef = useRef(null)
  const [dragOver, setDragOver] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const start = async (file) => {
    if (busy || !file) return
    if (file.size > AUDIO_MAX_MB * 1024 * 1024) { setError(TOO_LARGE); return }
    setError("")
    setBusy(true)
    try {
      const make = () => createFromAudio({ file, aspect_ratio: aspect, visual_style: style, settings: readLast("settings") })
      let p
      try {
        p = await make()
      } catch (e) {
        // A remembered caption style that was deleted since: forget it, as the script start does.
        if (e.detail?.field !== "captions.style") throw e
        forgetLastCaptionStyle()
        p = await make()
      }
      navigate(`/stock/p/${p.id}`)
    } catch (e) {
      setError(e.message || "Couldn't use that recording.")
    } finally {
      setBusy(false)
    }
  }

  const onPick = (e) => {
    const f = e.target.files?.[0]
    e.target.value = ""          // the same file can be picked again after a refusal
    start(f)
  }
  const onDrop = (e) => {
    e.preventDefault()
    setDragOver(false)
    start(e.dataTransfer.files?.[0])
  }

  return (
    <Box sx={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", p: { xs: 2, md: 3 }, gap: 1.5 }}>
      {error && <Alert severity="warning" onClose={() => setError("")}>{error}</Alert>}
      <Box
        role="button" tabIndex={0} aria-label="Choose an audio or video file" aria-disabled={busy}
        onClick={() => !busy && inputRef.current?.click()}
        onKeyDown={(e) => { if (!busy && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); inputRef.current?.click() } }}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
        onDragLeave={() => setDragOver(false)}
        onDrop={busy ? (e) => e.preventDefault() : onDrop}
        sx={{
          flex: 1, minHeight: 200,
          display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
          gap: 1, textAlign: "center", p: 3, cursor: busy ? "default" : "pointer",
          border: "2px dashed", borderColor: dragOver ? "primary.main" : "divider",
          borderRadius: 2, bgcolor: dragOver ? "action.hover" : "transparent",
          transition: "border-color .15s ease, background .15s ease",
          "&:hover": busy ? {} : { borderColor: "primary.main" },
        }}
      >
        <input ref={inputRef} type="file" accept={ACCEPT} hidden onChange={onPick} data-testid="audio-file-input" />
        {busy ? (
          <>
            <CircularProgress size={26} />
            <Typography variant="body2" sx={{ fontWeight: 700 }}>Opening your recording…</Typography>
          </>
        ) : (
          <>
            <GraphicEqIcon sx={{ fontSize: 40, color: "text.disabled" }} />
            <Typography variant="body2" sx={{ fontWeight: 700 }}>Drop an audio or video file</Typography>
            <Typography variant="caption" sx={{ color: "text.secondary", maxWidth: 380 }}>
              Your own voice, a voiceover or a podcast cut, {AUDIO_MIN_S} seconds to {AUDIO_MAX_S / 60} minutes.
              We listen to it and make one scene per sentence, in your voice. A video gives its sound.
            </Typography>
          </>
        )}
      </Box>
      <Typography variant="body2" sx={{ color: "text.secondary" }}>
        Listening runs on this computer. It opens as a project: change the footage, fix a caption, reorder
        scenes, then render. Your audio is the voice.
      </Typography>
    </Box>
  )
}
