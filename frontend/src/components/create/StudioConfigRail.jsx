// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
//
// Right-hand glass config rail for the Smart Video studio (/stock). Stacked
// frosted panels: Visual Style swatches, Format & Mode, Voice/Captions/Music
// (reuses AudioConfig), Transitions, and the estimated cost. Ported look from
// the hosted studio, adapted to the OSS backend contract (BYOK, no cloud media).
import { Box, Typography, Stack, Paper, ToggleButton, ToggleButtonGroup, Chip } from "@mui/material"
import ImageIcon from "@mui/icons-material/Image"
import { glassPanelSx } from "../../utils/glassFx"
import { STYLES, StyleSwatch } from "./SmartVideoStyles"
import AudioConfig from "./AudioConfig"
import EstimatedCost from "./EstimatedCost"
import ImageUpload from "./ImageUpload"

// Transition presets the stock pipeline can honor when stitching clips.
export const TRANSITIONS = [
  { id: "auto", label: "Auto" },
  { id: "none", label: "Hard cut" },
  { id: "fade", label: "Fade" },
  { id: "slide", label: "Slide" },
  { id: "zoom", label: "Zoom" },
  { id: "whip", label: "Whip pan" },
]

function Panel({ title, children, sx }) {
  return (
    <Paper elevation={0} sx={(t) => ({ ...glassPanelSx(t), p: 1.75, borderRadius: 2.5, ...sx })}>
      {title && (
        <Typography variant="overline" sx={{ color: "text.secondary", fontWeight: 700, fontSize: "0.62rem", display: "block", mb: 1 }}>
          {title}
        </Typography>
      )}
      {children}
    </Paper>
  )
}

export default function StudioConfigRail({
  visualStyle, setVisualStyle,
  aspectRatio, setAspectRatio,
  operation, setOperation,
  startImage, setStartImage,
  transitionStyle, setTransitionStyle,
  audioProps, ttsProvider, script,
}) {
  return (
    <Box sx={{ width: { xs: 300, lg: 344 }, flexShrink: 0, overflowY: "auto", overflowX: "hidden", p: 1.75, height: "100%" }}>
      <Stack spacing={1.5}>
        {/* Visual Style — drives the render's look + the live preview palette */}
        <Panel title="Visual Style">
          <Box sx={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 1 }}>
            {STYLES.map((s) => (
              <StyleSwatch
                key={s.id}
                style={s}
                selected={visualStyle === s.id}
                onClick={() => setVisualStyle(s.id)}
                compact
              />
            ))}
          </Box>
        </Panel>

        {/* Format & mode */}
        <Panel title="Format & Mode">
          <ToggleButtonGroup
            value={aspectRatio} exclusive fullWidth size="small"
            onChange={(_, v) => v && setAspectRatio(v)}
            sx={{ mb: 1, "& .MuiToggleButton-root": { textTransform: "none", fontSize: "0.78rem", py: 0.4 } }}
          >
            <ToggleButton value="9:16">9:16 · Vertical</ToggleButton>
            <ToggleButton value="16:9">16:9 · Wide</ToggleButton>
          </ToggleButtonGroup>
          <ToggleButtonGroup
            value={operation} exclusive fullWidth size="small"
            onChange={(_, v) => v && setOperation(v)}
            sx={{ "& .MuiToggleButton-root": { textTransform: "none", fontSize: "0.78rem", py: 0.4 } }}
          >
            <ToggleButton value="t2v">Script → Video</ToggleButton>
            <ToggleButton value="i2v"><ImageIcon sx={{ fontSize: 15, mr: 0.5 }} />Image</ToggleButton>
          </ToggleButtonGroup>
          {operation === "i2v" && (
            <Box sx={{ mt: 1.25 }}>
              <ImageUpload label="Input image" value={startImage} onChange={setStartImage} onRemove={() => setStartImage(null)} />
            </Box>
          )}
        </Panel>

        {/* Voice, captions & music — reuse the OSS AudioConfig (BYOK/edge providers) */}
        <Panel sx={{ p: 1.5 }}>
          <AudioConfig {...audioProps} />
        </Panel>

        {/* Transitions between stock clips */}
        <Panel title="Transitions">
          <Stack direction="row" flexWrap="wrap" useFlexGap sx={{ gap: 0.75 }}>
            {TRANSITIONS.map((t) => (
              <Chip
                key={t.id}
                label={t.label}
                size="small"
                variant={transitionStyle === t.id ? "filled" : "outlined"}
                color={transitionStyle === t.id ? "primary" : "default"}
                onClick={() => setTransitionStyle(t.id)}
                sx={{ fontSize: "0.72rem", height: 26 }}
              />
            ))}
          </Stack>
        </Panel>

        <EstimatedCost mode="stock" model={null} ttsProvider={ttsProvider} script={script} />
      </Stack>
    </Box>
  )
}
