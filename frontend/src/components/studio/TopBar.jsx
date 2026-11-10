// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { memo, useEffect, useRef, useState } from "react"
import {
  Box, Button, CircularProgress, IconButton, InputBase, LinearProgress, Stack,
  ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from "@mui/material"
import ArrowBackIcon from "@mui/icons-material/ArrowBack"
import UndoIcon from "@mui/icons-material/Undo"
import RedoIcon from "@mui/icons-material/Redo"
import MovieCreationOutlinedIcon from "@mui/icons-material/MovieCreationOutlined"
import CloudDoneOutlinedIcon from "@mui/icons-material/CloudDoneOutlined"
import CloudOffOutlinedIcon from "@mui/icons-material/CloudOffOutlined"
import NotesOutlinedIcon from "@mui/icons-material/NotesOutlined"
import PlayCircleOutlineIcon from "@mui/icons-material/PlayCircleOutline"
import ShortcutsPopover from "../ShortcutsPopover"
import { ASPECTS } from "./studioModel"
import { formatClock } from "../../utils/format"

// The editor's keys (SmartVideoStudio.jsx's window handler). They were named
// only inside a few button labels — "Split (S)", "Next clip (N)" — so half of
// them were undiscoverable. Kept here, next to the bar that surfaces them.
const STUDIO_SHORTCUTS = [
  ["Space", "Play / pause"],
  ["← →", "Previous / next scene"],
  ["S", "Split the selected scene"],
  ["M", "Merge it with the next one"],
  ["N", "Next clip for the selected scene"],
  ["⌫", "Delete the selected scene"],
  ["Esc", "Close the preview, then the scene panel"],
  ["⌘Z / ⇧⌘Z", "Undo / redo"],
]

// The bar's own width (not the window's: the app sidebar's width varies).
// 0 until measured, which reads as "wide" — the full bar.
function useWidth() {
  const ref = useRef(null)
  const [w, setW] = useState(0)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === "undefined") return undefined
    const ro = new ResizeObserver(([e]) => setW(Math.round(e.contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, w]
}

// Below these bar widths the secondary items give way, so Render — the main
// action here — never wraps or leaves the screen (audit 2026-09-25: at
// 1024-1280px windows it wrapped to two lines and was pushed off the edge).
const HIDE_DURATION_BELOW = 1100
const COMPACT_BELOW = 960
// Narrower than this (a phone, a split window) no single row holds it all, so
// the bar takes two rows and Render ends the second.
const TWO_ROWS_BELOW = 760
// On a phone the second row holds Render only if Last render is an icon: as
// words it took a third row.
const PHONE_BELOW = 480

function TopBar({
  project, timeline, saving, saveError, canUndo, canRedo, onUndo, onRedo, onRename, onAspect,
  onRender, rendering, onBack, onScript, onOpenLast = null, onRetrySave = null,
}) {
  const [title, setTitle] = useState(project.title)
  useEffect(() => { setTitle(project.title) }, [project.title])
  // A blank title isn't a name — put the current one back instead of showing an empty field.
  const commit = () => {
    const t = title.trim()
    if (!t) { setTitle(project.title); return }
    if (t !== project.title) onRename(t)
  }

  const [barRef, barW] = useWidth()
  const measured = barW > 0
  const twoRows = measured && barW < TWO_ROWS_BELOW
  const phone = measured && barW < PHONE_BELOW
  const showDuration = !measured || barW >= HIDE_DURATION_BELOW
  const compact = measured && barW < COMPACT_BELOW
  return (
    <Box ref={barRef} sx={{ flexShrink: 0, bgcolor: "background.paper", position: "relative" }}>
      <Stack direction="row" alignItems="center" spacing={1} useFlexGap={twoRows} data-testid="topbar-row" sx={{ px: 1.25, py: 0.75, minHeight: 52, flexWrap: twoRows ? "wrap" : "nowrap", minWidth: 0 }}>
        <Tooltip title="All projects">
          <IconButton size="small" aria-label="All projects" onClick={onBack} sx={{ flexShrink: 0 }}><ArrowBackIcon fontSize="small" /></IconButton>
        </Tooltip>
        <InputBase
          value={title} onChange={(e) => setTitle(e.target.value)} onBlur={commit}
          onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur() }}
          inputProps={{ "aria-label": "Project title" }}
          sx={{
            fontWeight: 800, fontSize: "1rem", flex: twoRows ? "1 1 0" : "0 1 460px", minWidth: 72, maxWidth: 460, px: 0.75, borderRadius: 1,
            "&:hover": { bgcolor: "action.hover" }, "&.Mui-focused": { bgcolor: "action.hover" },
          }}
        />
        <ToggleButtonGroup size="small" exclusive value={project.doc.aspect_ratio} onChange={(_, v) => v && onAspect(v)}
          sx={{ ml: 0.5, flexShrink: 0, "& .MuiToggleButton-root": { py: 0.15, px: 1, fontWeight: 700, fontSize: "0.7rem" } }}>
          {ASPECTS.map((a) => <ToggleButton key={a} value={a}>{a}</ToggleButton>)}
        </ToggleButtonGroup>
        {showDuration && (
          <Typography variant="caption" sx={{ color: "text.secondary", whiteSpace: "nowrap", flexShrink: 0, display: { xs: "none", sm: "block" } }}>
            {formatClock(timeline.total)} · {timeline.scenes.length} scenes
          </Typography>
        )}

        <Tooltip title="Edit the whole script as a document">
          {compact ? (
            <IconButton size="small" aria-label="Script" onClick={onScript} sx={{ flexShrink: 0 }}>
              <NotesOutlinedIcon fontSize="small" />
            </IconButton>
          ) : (
            <Button size="small" color="inherit" aria-label="Script" startIcon={<NotesOutlinedIcon fontSize="small" />} onClick={onScript} sx={{ textTransform: "none", fontWeight: 700, flexShrink: 0 }}>
              Script
            </Button>
          )}
        </Tooltip>

        <Box sx={twoRows ? { flexBasis: "100%", height: 0 } : { flex: 1, minWidth: 0 }} />

        <Tooltip title="Undo (⌘Z)"><span><IconButton size="small" aria-label="Undo" disabled={!canUndo} onClick={onUndo}><UndoIcon fontSize="small" /></IconButton></span></Tooltip>
        <Tooltip title="Redo (⇧⌘Z)"><span><IconButton size="small" aria-label="Redo" disabled={!canRedo} onClick={onRedo}><RedoIcon fontSize="small" /></IconButton></span></Tooltip>
        <Tooltip title={saveError && !saving ? `Your last change wasn't saved: ${saveError}` : ""}>
          {/* The one place a failed save is reported (no toast on top of it):
              announced politely, and focusable while failed so the reason in
              the tooltip is reachable from the keyboard too. */}
          <Stack role="status" aria-live="polite" tabIndex={saveError && !saving ? 0 : undefined}
            direction="row" alignItems="center" spacing={0.5} sx={{ color: saveError && !saving ? "warning.main" : "text.secondary", minWidth: compact ? 0 : 64, flexShrink: 0, borderRadius: 1 }}>
            {saving ? <CircularProgress size={12} />
              : saveError ? <CloudOffOutlinedIcon sx={{ fontSize: 16 }} /> : <CloudDoneOutlinedIcon sx={{ fontSize: 16 }} />}
            {/* Compact: the icon says it; the words stay for screen readers. */}
            <Typography variant="caption" sx={compact ? {
              position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)", whiteSpace: "nowrap",
            } : undefined}>{saving ? "Saving…" : saveError ? "Not saved" : "Saved"}</Typography>
          </Stack>
        </Tooltip>
        {saveError && !saving && onRetrySave && (
          <Button size="small" color="warning" onClick={onRetrySave} sx={{ textTransform: "none", fontWeight: 700, minWidth: 0, flexShrink: 0 }}>
            Retry
          </Button>
        )}

        <ShortcutsPopover shortcuts={STUDIO_SHORTCUTS} />
        {onOpenLast && (phone ? (
          <Tooltip title="Last render">
            <IconButton size="small" color="primary" aria-label="Last render" onClick={onOpenLast} sx={{ flexShrink: 0 }}><PlayCircleOutlineIcon fontSize="small" /></IconButton>
          </Tooltip>
        ) : (
          <Button size="small" onClick={onOpenLast} sx={{ textTransform: "none", fontWeight: 700, flexShrink: 0, whiteSpace: "nowrap" }}>
            Last render
          </Button>
        ))}
        <Button
          variant="contained" disabled={!!rendering || !timeline.scenes.length} onClick={onRender}
          startIcon={rendering ? <CircularProgress size={14} color="inherit" /> : <MovieCreationOutlinedIcon />}
          // Pinned: never shrinks, never wraps — everything else gives way first.
          sx={{ textTransform: "none", fontWeight: 800, px: 2, flexShrink: 0, whiteSpace: "nowrap", ...(twoRows ? { ml: "auto" } : null) }}
        >
          {rendering ? `Rendering ${rendering.pct}%` : "Render"}
        </Button>
      </Stack>
      {rendering && (
        <Box sx={{ position: "absolute", left: 0, right: 0, bottom: 0 }}>
          <LinearProgress variant="determinate" value={rendering.pct} sx={{ height: 2 }} />
        </Box>
      )}
    </Box>
  )
}

export default memo(TopBar)
