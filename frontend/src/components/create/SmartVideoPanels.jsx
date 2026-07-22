import { useState } from "react"
import {
  Box, Typography, Button, Stack, IconButton, Paper, CircularProgress,
} from "@mui/material"
import PlayArrowIcon from "@mui/icons-material/PlayArrow"

/**
 * Compact Inspiration-Source banner used in the script panel. Replaces the
 * legacy <SourcePanel> for /stock so 5 insights + transcript toggle fit in
 * ~120px instead of ~260px. Insights render as a 2-col grid; each value is
 * line-clamped to 2 lines with hover-to-see-full via the native `title` attr.
 */
export function CompactSourcePanel({ source }) {
  const [showTranscript, setShowTranscript] = useState(false)
  if (!source) return null
  const insights = source.insights || {}
  const insightItems = [
    insights.hook            && { label: "Hook",      color: "secondary.main", value: insights.hook },
    insights.structure       && { label: "Structure", color: "secondary.main", value: insights.structure },
    insights.why_viral       && { label: "Why viral", color: "warning.main",   value: insights.why_viral },
    insights.suggested_angle && { label: "Angle",     color: "primary.main",   value: insights.suggested_angle },
    insights.suggested_title && { label: "Title",     color: "primary.main",   value: insights.suggested_title },
  ].filter(Boolean)

  return (
    <Paper
      variant="outlined"
      sx={{ p: 1.25, borderRadius: 2, mb: 1, bgcolor: "action.hover" }}
    >
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="overline" sx={{ fontSize: "0.6rem", fontWeight: 700, color: "text.secondary", letterSpacing: "0.06em" }}>
          Inspiration
        </Typography>
        <Typography variant="caption" sx={{ fontWeight: 700, fontSize: "0.78rem", flex: 1, minWidth: 0 }} noWrap title={source.title}>
          {source.title || "Untitled"}
        </Typography>
        {source.transcript && (
          <Button
            size="small" variant="text"
            onClick={() => setShowTranscript(!showTranscript)}
            sx={{ minWidth: 0, px: 0.75, fontSize: "0.65rem", textTransform: "none", fontWeight: 600, lineHeight: 1 }}
          >
            {showTranscript ? "Hide transcript" : "Transcript"}
            {source.transcript_language && ` (${source.transcript_language})`}
          </Button>
        )}
      </Stack>

      {showTranscript && source.transcript && (
        <Paper variant="outlined" sx={{
          p: 1, mt: 0.5, mb: 0.75, maxHeight: 110, overflowY: "auto",
          fontSize: "0.72rem", color: "text.secondary", lineHeight: 1.5, bgcolor: "background.paper",
        }}>
          {source.transcript}
        </Paper>
      )}

      {insightItems.length > 0 && (
        <Box sx={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 0.75,
        }}>
          {insightItems.map((it, i) => {
            // An odd last item spans both columns so the grid never leaves a
            // ragged empty cell (5 insights = 2 + 2 + 1-full-width).
            const spanFull = insightItems.length % 2 === 1 && i === insightItems.length - 1
            return (
              <Box
                key={it.label}
                title={it.value}
                sx={{
                  gridColumn: spanFull ? "1 / -1" : "auto",
                  px: 0.875, py: 0.625, borderRadius: 1.5,
                  bgcolor: "background.paper",
                  borderLeft: "3px solid", borderColor: it.color,
                  minWidth: 0,
                }}
              >
                <Typography sx={{
                  color: it.color, fontWeight: 800, fontSize: "0.58rem",
                  textTransform: "uppercase", letterSpacing: "0.05em", lineHeight: 1,
                  display: "block", mb: 0.35,
                }}>
                  {it.label}
                </Typography>
                <Typography variant="body2" sx={{
                  fontSize: "0.74rem", lineHeight: 1.35, color: "text.secondary",
                  // Clamp so a long insight doesn't blow up the banner (the
                  // full text is on the native `title` tooltip). Full-width
                  // items get a 3rd line since they have the room.
                  display: "-webkit-box",
                  WebkitLineClamp: spanFull ? 3 : 2,
                  WebkitBoxOrient: "vertical",
                  overflow: "hidden",
                }}>
                  {it.value}
                </Typography>
              </Box>
            )
          })}
        </Box>
      )}
    </Paper>
  )
}

export function VoiceRow({ voice, selected, onClick, onPreview, isPlayingPreview }) {
  return (
    <Box
      onClick={onClick}
      sx={(theme) => ({
        cursor: "pointer",
        height: 50,
        px: 0.875,
        borderRadius: 1.5,
        display: "flex", alignItems: "center", gap: 1,
        position: "relative",
        transition: "background .12s ease, transform .12s ease",
        border: "1px solid",
        borderColor: selected ? "primary.main" : "transparent",
        background: selected
          ? (theme.palette.mode === "dark"
              ? "linear-gradient(90deg, rgba(99,179,237,0.16), rgba(99,179,237,0.04))"
              : "linear-gradient(90deg, rgba(99,179,237,0.18), rgba(99,179,237,0.05))")
          : "transparent",
        ...(selected && {
          boxShadow: "0 0 14px rgba(99,179,237,0.30), inset 0 0 8px rgba(99,179,237,0.10)",
        }),
        "&:hover": {
          background: selected ? undefined
            : (theme.palette.mode === "dark" ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.04)"),
          transform: "translateX(2px)",
        },
      })}
    >
      <IconButton
        size="small"
        onClick={(e) => { e.stopPropagation(); onPreview?.(voice) }}
        sx={{
          p: 0.25, width: 30, height: 30, flexShrink: 0,
          bgcolor: selected ? "primary.main" : "action.hover",
          color: selected ? "#fff" : "text.primary",
          "&:hover": { bgcolor: selected ? "primary.dark" : "action.selected" },
        }}
      >
        {isPlayingPreview
          ? <CircularProgress size={14} sx={{ color: "inherit" }} />
          : <PlayArrowIcon sx={{ fontSize: 17 }} />}
      </IconButton>
      <Typography
        variant="body2"
        sx={{ fontWeight: selected ? 700 : 600, fontSize: "0.85rem", lineHeight: 1.1, flexShrink: 0 }}
      >
        {voice.label}
      </Typography>
      <Typography
        variant="caption"
        sx={{ color: "text.secondary", fontSize: "0.72rem", flex: 1, minWidth: 0, lineHeight: 1.2 }}
        noWrap
      >
        {voice.tagline}
      </Typography>
      {voice.country && (
        <Box sx={{
          px: 0.5, py: 0.1,
          fontSize: "0.6rem", fontWeight: 700,
          color: "text.secondary",
          border: 1, borderColor: "divider",
          borderRadius: 0.5,
          lineHeight: 1, flexShrink: 0,
          fontVariantNumeric: "tabular-nums",
        }}>
          {voice.country}
        </Box>
      )}
      {voice.provider === "edge_tts" && (
        <Box sx={{
          px: 0.5, py: 0.1,
          fontSize: "0.55rem", fontWeight: 700,
          color: "success.main",
          border: 1, borderColor: "success.main",
          borderRadius: 0.5,
          lineHeight: 1, flexShrink: 0,
        }}>
          FREE
        </Box>
      )}
    </Box>
  )
}

// ── Main page ──────────────────────────────────────────────────────

