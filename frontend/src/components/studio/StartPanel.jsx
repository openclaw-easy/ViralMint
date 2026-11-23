// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { useEffect, useMemo, useRef, useState } from "react"
import {
  Box, Button, Chip, CircularProgress, Stack, TextField, ToggleButton, ToggleButtonGroup,
  Tooltip, Typography, alpha,
} from "@mui/material"
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome"
import EditNoteIcon from "@mui/icons-material/EditNoteOutlined"
import LightbulbOutlinedIcon from "@mui/icons-material/LightbulbOutlined"
import GraphicEqIcon from "@mui/icons-material/GraphicEqOutlined"
import AudioStart from "./AudioStart"
import { PreviewCanvas, STYLES, StyleSwatch } from "../create/SmartVideoStyles"
import SampleShowcase from "../SampleShowcase"
import { SMART_VIDEO_SAMPLES } from "../../data/sampleShowcase"
import { narrationStats } from "../../utils/spokenLength"
import { formatClock } from "../../utils/format"
import { ASPECTS, LINE_MAX, estimateSeconds } from "./studioModel"
import { SCENE_MAX, planLines } from "./studioModel"
import { draftScript, readLast, rememberLast } from "./studioApi"

// The desk's own width (the app sidebar's width varies, so a window
// breakpoint mis-fires): below this the sample column starts folded, so the
// script keeps a writing width. At a 1024px window the open column left the
// editor ~196px wide (audit 2026-09-25).
const FOLD_SAMPLES_BELOW = 960

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

const TOPIC_EXAMPLES = ["5 money habits", "Morning routine for focus", "Why cats purr", "Rice hack"]

const SOURCES = [
  { value: "script", label: "Write a script", icon: <EditNoteIcon fontSize="small" /> },
  { value: "topic", label: "Start from a topic", icon: <LightbulbOutlinedIcon fontSize="small" /> },
  { value: "audio", label: "My audio", icon: <GraphicEqIcon fontSize="small" /> },
]

// Studio Home's script desk. The script is the thing you work on here:
// a topic drafts INTO it and a sample loads INTO it, so every start is
// reviewable text first. The scene list beside it is the same grouping the
// editor builds from (planLines), so it previews the storyboard exactly.
export default function StartPanel({ onCreate, busy = false, preferTopic = false, workingLabel = "", prefill = null }) {
  const [source, setSource] = useState(preferTopic ? "topic" : "script")
  const [script, setScript] = useState("")
  const [topic, setTopic] = useState("")
  const [origin, setOrigin] = useState({ title: "", sampleId: null })
  // The last video's aspect and look are this one's defaults.
  const last = readLast("start") || {}
  const [aspect, setAspect] = useState(ASPECTS.includes(last.aspect) ? last.aspect : "9:16")
  const [style, setStyle] = useState(STYLES.some((s) => s.id === last.style) ? last.style : "cinematic")
  const [drafting, setDrafting] = useState(false)
  const [error, setError] = useState("")
  const previousScript = useRef(null)
  const [canRestore, setCanRestore] = useState(false)
  const textRef = useRef(null)

  // A first-time user (no projects yet) or "Make another like this" starts on
  // the topic box — one line is easier than a blank script page. Applied once
  // when the hint arrives (the project list loads after mount), and only
  // while the desk is untouched.
  const nudged = useRef(preferTopic)
  useEffect(() => {
    if (preferTopic && !nudged.current && source === "script" && !script.trim()) setSource("topic")
    if (preferTopic) nudged.current = true
  }, [preferTopic])  // eslint-disable-line react-hooks/exhaustive-deps

  // A failed handoff (chat / Library link) hands its topic or script back to
  // the desk instead of losing it with the error toast.
  useEffect(() => {
    if (!prefill) return
    if (prefill.source) setSource(prefill.source)
    if (prefill.topic) setTopic(prefill.topic)
    if (prefill.script) setScript(prefill.script)
  }, [prefill])

  const lines = useMemo(() => planLines(script), [script])
  const stats = narrationStats(script)
  const kept = lines.slice(0, SCENE_MAX)
  const shown = source === "audio" ? [] : kept     // My audio: the scenes come from listening, not from the text box
  const overflow = Math.max(0, lines.length - SCENE_MAX)
  const totalSeconds = kept.reduce((a, l) => a + estimateSeconds(l) + 0.15, 0)

  const hasScript = script.trim().length > 10
  const topicReady = source === "topic" && topic.trim().length > 1
  const working = busy || drafting
  const ready = hasScript || topicReady
  // The palette the storyboard frames are drawn in — changing the Look below
  // redraws the whole strip, so the choice shows its effect immediately.
  const look = STYLES.find((s) => s.id === style) || STYLES[0]
  const samples = SMART_VIDEO_SAMPLES
  const [deskRef, deskW] = useWidth()
  const narrow = deskW > 0 && deskW < FOLD_SAMPLES_BELOW
  // Narrow: folded by default, and opening it is this visit's choice only —
  // it doesn't rewrite the preference the wide layout remembers.
  const [narrowOpen, setNarrowOpen] = useState(false)

  const replaceScript = (text) => {
    if (script.trim() && script !== text) {
      previousScript.current = script
      setCanRestore(true)
    }
    setScript(text)
  }

  const writeFromTopic = async () => {
    setError("")
    setDrafting(true)
    try {
      const text = await draftScript(topic, aspect)
      rememberLast("start", { aspect, style })
      replaceScript(text)
      setOrigin({ title: topic.trim(), sampleId: null })
      return text
    } catch (e) {
      setError(e.message)
      return null
    } finally {
      setDrafting(false)
    }
  }

  const loadSample = (s) => {
    replaceScript(s.script)
    setOrigin({ title: s.label, sampleId: s.id })
    if (s.apply?.aspectRatio) setAspect(s.apply.aspectRatio)
    if (s.apply?.visualStyle && STYLES.some((x) => x.id === s.apply.visualStyle)) setStyle(s.apply.visualStyle)
  }

  const go = async () => {
    if (!ready || working) return
    // Topic mode writes the script FIRST and stops there: the desk promises
    // "edit it as much as you like before you build", so building straight
    // from a topic skipped the very review it offered (2026-10-13 audit).
    if (!hasScript && topicReady) {
      await writeFromTopic()
      return
    }
    const text = script
    rememberLast("start", { aspect, style })
    onCreate?.(
      { mode: "script", script: text, title: origin.title, sample_id: origin.sampleId },
      { firstDraft: true, aspect_ratio: aspect, visual_style: style },
    )
  }

  return (
    <Box>
      <Stack
        direction={{ xs: "column", sm: "row" }} spacing={1.5}
        alignItems={{ xs: "flex-start", sm: "center" }} justifyContent="space-between" sx={{ mb: 1.5 }}
      >
        <Box>
          <Typography variant="h6" sx={{ fontWeight: 800, lineHeight: 1.2 }}>New video</Typography>
          <Typography variant="body2" sx={{ color: "text.secondary" }}>
            {source === "audio"
              ? "Bring your own recording. Each sentence becomes a scene, spoken in your audio."
              : "Write what the video says. Each sentence or two becomes a scene you can watch and change."}
          </Typography>
        </Box>
        <ToggleButtonGroup
          size="small" exclusive value={source}
          onChange={(_, v) => { if (v) { setSource(v); setError("") } }}
          // One segmented control, never split across two rows (at 1024 it
          // broke into "Write a script" above the other two). It keeps its
          // width; the description beside it wraps instead.
          sx={{
            flexShrink: 0, flexWrap: { xs: "wrap", sm: "nowrap" },
            "& .MuiToggleButton-root": { textTransform: "none", fontWeight: 600, gap: 0.75, px: 1.5, py: 0.6, whiteSpace: "nowrap" },
          }}
        >
          {SOURCES.map((s) => (
            <ToggleButton key={s.value} value={s.value}>{s.icon}{s.label}</ToggleButton>
          ))}
        </ToggleButtonGroup>
      </Stack>

      <Box ref={deskRef} sx={(t) => ({
        display: "grid",
        // `auto` for the samples column: it sizes itself (200px open, 40px
        // collapsed) exactly as it did before the redesign. The scenes column
        // gives way (340 → 260px) before the script does.
        gridTemplateColumns: { xs: "1fr", md: "auto minmax(300px, 1fr) minmax(260px, min(340px, 32%))" },
        // Fill the window: the hero (~82px), the "New video" row (~55px) and
        // the page padding are ~180px. At 100vh - 300px the desk stopped
        // ~150px above the fold at 1440×900 and the Look preview had to
        // squeeze into what was left (audit 2026-09-25).
        height: { md: "clamp(480px, calc(100vh - 190px), 760px)" },
        border: 1, borderColor: "divider", borderRadius: 2, overflow: "hidden",
        bgcolor: "background.paper",
        boxShadow: t.palette.mode === "dark" ? "none" : `0 1px 3px ${alpha(t.palette.common.black, 0.05)}`,
      })}>
        {/* ── LEFTMOST: "Start from a sample" (as before the redesign) ────
            An in-flow collapsible column, not an overlay: 10 cards need the
            room, and collapsing gives it back to the script. The component
            persists its own open state under `vm_showcase_open`. Hidden on a
            phone, where the single-column grid has no width to spare. */}
        <Box sx={{ display: { xs: "none", md: "block" }, minHeight: 0 }}>
          {narrow
            ? <SampleShowcase samples={samples} onUse={loadSample} open={narrowOpen} onToggle={() => setNarrowOpen((v) => !v)} />
            : <SampleShowcase samples={samples} onUse={loadSample} />}
        </Box>

        {/* ── Writing column (or, in My audio, the recording drop) ── */}
        {source === "audio" ? (
          <Box sx={{ display: "flex", flexDirection: "column", minHeight: { xs: 320, md: 0 }, minWidth: 0 }}>
            <AudioStart aspect={aspect} style={style} />
          </Box>
        ) : (
        <Box sx={{ display: "flex", flexDirection: "column", minHeight: 0, minWidth: 0 }}>
          {source === "topic" && (
            <Box sx={{ px: 3, pt: 2.5, pb: 2, borderBottom: 1, borderColor: "divider" }}>
              <Stack direction="row" spacing={1} alignItems="flex-start">
                <TextField
                  fullWidth autoFocus size="small" value={topic}
                  onChange={(e) => setTopic(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && topicReady && !working) writeFromTopic() }}
                  label="What's the video about?"
                  placeholder="e.g. 5 money habits that actually work"
                  helperText="Written by your AI provider, in your topic's language"
                />
                <Button
                  variant="outlined" onClick={writeFromTopic} disabled={!topicReady || working}
                  startIcon={drafting ? <CircularProgress size={14} color="inherit" /> : <AutoAwesomeIcon />}
                  sx={{ textTransform: "none", fontWeight: 700, whiteSpace: "nowrap", flexShrink: 0, height: 40 }}
                >
                  {hasScript ? "Rewrite script" : "Write script"}
                </Button>
              </Stack>
              <Stack direction="row" spacing={0.75} useFlexGap flexWrap="wrap" sx={{ mt: 1.25 }}>
                {TOPIC_EXAMPLES.map((ex) => (
                  <Chip key={ex} size="small" variant="outlined" label={ex} onClick={() => setTopic(ex)} />
                ))}
              </Stack>
            </Box>
          )}

          <Box sx={{ flex: 1, minHeight: 0, position: "relative" }}>
            {(drafting || workingLabel) && (
              <Stack
                data-testid="desk-working" role="status" alignItems="center" justifyContent="center" spacing={1.25}
                sx={(t) => ({ position: "absolute", inset: 0, zIndex: 1, bgcolor: alpha(t.palette.background.paper, 0.86), px: 3, textAlign: "center" })}
              >
                <CircularProgress size={22} />
                <Typography variant="body2" sx={{ fontWeight: 700 }}>
                  {workingLabel || `Writing a script about “${topic.trim()}”…`}
                </Typography>
                <Typography variant="caption" sx={{ color: "text.secondary" }}>Usually 5–15 seconds</Typography>
              </Stack>
            )}
            <Box
              component="textarea"
              ref={textRef}
              aria-label="Script"
              value={script}
              onChange={(e) => { setScript(e.target.value); setCanRestore(false) }}
              onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); go() } }}
              placeholder={source === "topic"
                ? "Your script appears here once it's written. Edit it as much as you like before you build."
                : "Paste or write your narration.\n\nStart with a line that makes someone stop scrolling. Keep sentences short, since each one or two becomes a scene."}
              spellCheck
              sx={(t) => ({
                display: "block", width: "100%", height: { xs: 360, md: "100%" },
                resize: "none", border: 0, outline: 0, m: 0,
                px: { xs: 2.5, md: 4 }, py: 3,
                bgcolor: "transparent", color: "text.primary",
                font: "inherit", fontSize: 17, lineHeight: 1.75,
                "&::placeholder": { color: t.palette.text.disabled, opacity: 1 },
              })}
            />
          </Box>

          <Box sx={{
            px: { xs: 2, md: 3 }, py: 1.5, borderTop: 1, borderColor: "divider",
            display: "flex", alignItems: "center", gap: 1.5, flexWrap: "wrap",
          }}>
            <Box sx={{ flex: 1, minWidth: 180 }}>
              {error ? (
                <Typography variant="body2" sx={{ color: "error.main" }}>{error}</Typography>
              ) : (
                <Typography variant="body2" sx={{ color: "text.secondary" }}>
                  {stats.units
                    ? `${stats.units} ${stats.unitLabel}, about ${formatClock(totalSeconds)} spoken`
                    : "Nothing written yet"}
                  {canRestore && (
                    <Button
                      size="small" variant="text"
                      onClick={() => { setScript(previousScript.current || ""); setCanRestore(false) }}
                      sx={{ textTransform: "none", ml: 1, py: 0, minWidth: 0 }}
                    >
                      Restore previous script
                    </Button>
                  )}
                </Typography>
              )}
            </Box>
            {/* ONE button (2026-09-17). A first draft plans the visuals AND
                records every line; the plan-only path is still there for the
                callers that need it (`plan: true`, used by the `?script=`
                handoff), just not as a second thing to weigh up. */}
            <Tooltip title={!hasScript && topicReady
              ? "Writes the script here first, so you can read and change it before anything is built."
              : "Plans every scene and voices each line, so you can watch it before you render. ⌘Enter"}>
              <span>
                <Button
                  data-testid="desk-primary"
                  variant="contained" disabled={!ready || working} onClick={() => go()}
                  startIcon={working ? <CircularProgress size={14} color="inherit" /> : <AutoAwesomeIcon />}
                  sx={{ textTransform: "none", fontWeight: 700 }}
                >
                  {!hasScript && topicReady ? "Write my script" : "Make a first draft"}
                </Button>
              </span>
            </Tooltip>
          </Box>
        </Box>
        )}

        {/* ── Scenes + look ──────────────────────────────── */}
        <Box sx={(t) => ({
          display: "flex", flexDirection: "column", minHeight: 0,
          // Full shorthands with the colour in them: a responsive `borderLeft: {md: 1}`
          // is emitted after `borderColor` and resets it to currentColor (a black rule).
          borderLeft: { xs: "none", md: `1px solid ${t.palette.divider}` },
          borderTop: { xs: `1px solid ${t.palette.divider}`, md: "none" },
          bgcolor: alpha(t.palette.text.primary, t.palette.mode === "dark" ? 0.03 : 0.015),
        })}>
          <Stack direction="row" alignItems="baseline" justifyContent="space-between" sx={{ px: 2, pt: 2, pb: 1 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 800 }}>Scenes</Typography>
            {shown.length > 0 && (
              <Typography variant="caption" sx={{ color: "text.secondary" }}>
                {shown.length} of up to {SCENE_MAX}
              </Typography>
            )}
          </Stack>

          <Box data-testid="scene-preview" sx={{ flex: 1, minHeight: { xs: 0, md: 0 }, maxHeight: { xs: 320, md: "none" }, overflowY: "auto", px: 1, pb: 1 }}>
            {shown.length === 0 ? (
              <Box sx={{ px: 1, py: 1, height: "100%", display: "flex", flexDirection: "column", gap: 1.5 }}>
                <Typography variant="body2" sx={{ color: "text.secondary" }}>
                  {source === "audio"
                    ? "Scenes appear in the editor once we've listened to your recording: one per sentence."
                    : "Scenes appear here as you write, so you can see how the video will be cut before you build it."}
                </Typography>
                {/* Until there are scenes, the rail shows the LOOK instead of
                    nothing — one canvas, so picking a style below has a visible
                    effect before a word is written. The canvas caps itself at
                    max-height:100%, which only resolves against a DEFINITE
                    height; the absolutely-inset box gives it one, so it shrinks
                    to whatever height the rail has (audit 2026-09-25). */}
                <Box data-testid="look-preview" sx={{ flex: 1, minHeight: 120, position: "relative" }}>
                  <Box sx={{ position: "absolute", inset: 0, pb: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
                    <PreviewCanvas
                      paletteA={look.cA} paletteB={look.cB} aspectRatio={aspect}
                      captionText={`${look.label} — ${look.tag}`}
                    />
                  </Box>
                </Box>
              </Box>
            ) : shown.map((line, i) => (
              <ScenePreviewRow key={`${i}:${line.slice(0, 24)}`} index={i} line={line} aspect={aspect} look={look} />
            ))}
            {source !== "audio" && overflow > 0 && (
              <Typography variant="body2" sx={{ color: "warning.main", px: 1, py: 1 }}>
                {overflow} more scene{overflow === 1 ? "" : "s"} won't fit in one video. Shorten the script or split it into two videos.
              </Typography>
            )}
          </Box>

          <Box sx={{ px: 2, py: 1.75, borderTop: 1, borderColor: "divider" }}>
            <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1.25 }}>
              <Typography variant="subtitle2" sx={{ fontWeight: 800 }}>Look</Typography>
              <ToggleButtonGroup
                size="small" exclusive value={aspect} onChange={(_, v) => v && setAspect(v)}
                aria-label="Aspect ratio"
                sx={{ "& .MuiToggleButton-root": { py: 0.2, px: 1.2, fontWeight: 700, fontSize: "0.72rem" } }}
              >
                {ASPECTS.map((a) => <ToggleButton key={a} value={a}>{a}</ToggleButton>)}
              </ToggleButtonGroup>
            </Stack>
            <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(72px, 1fr))", gap: 0.5, justifyItems: "center" }}>
              {STYLES.map((s) => (
                <StyleSwatch key={s.id} style={s} mini selected={style === s.id} onClick={() => setStyle(s.id)} />
              ))}
            </Box>
          </Box>
        </Box>
      </Box>
    </Box>
  )
}

// A scene's frame in the storyboard: the chosen look's palette with the line
// set in it, at the chosen aspect. Static CSS — one gradient and some text, no
// canvas and no rAF, because a project can have 18 of these. The angle walks
// with the index so consecutive frames don't read as one repeated tile.
const FRAME = { "9:16": { w: 62, h: 110 }, "16:9": { w: 112, h: 63 }, "1:1": { w: 84, h: 84 } }

function SceneFrame({ index, line, aspect, look }) {
  const { w, h } = FRAME[aspect] || FRAME["9:16"]
  // Angle AND stop offsets walk with the index, so a run of scenes reads as
  // separate frames rather than one tile repeated. Deliberately NOT a CSS
  // `filter` — per-tile filters are what froze the Library grid's scroll.
  const a = 115 + (index % 4) * 30
  const from = 4 + (index % 3) * 12
  const to = 88 - (index % 3) * 10
  return (
    <Box sx={{
      width: w, height: h, flexShrink: 0, borderRadius: 1, overflow: "hidden", position: "relative",
      background: `linear-gradient(${a}deg, ${look.cA} ${from}%, ${look.cB} ${to}%)`,
      display: "flex", alignItems: "flex-end",
    }}>
      {/* The caption sits where the render burns it: bottom, bold, shadowed.
          Too small to read on purpose — the line is spelled out beside the
          frame; here it only has to look like a burned-in caption. */}
      <Typography component="span" sx={{
        px: 0.5, pb: 0.5, width: "100%",
        fontSize: aspect === "16:9" ? 8 : 7.5, lineHeight: 1.25, fontWeight: 800, textAlign: "center",
        color: "#fff", textShadow: "0 1px 2px rgba(0,0,0,.9)",
        display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden",
      }}>
        {line}
      </Typography>
      <Box sx={{
        position: "absolute", top: 3, left: 3, px: 0.4, borderRadius: 0.5,
        bgcolor: "rgba(0,0,0,.55)", color: "#fff", fontSize: 8, fontWeight: 800, lineHeight: 1.5,
      }}>
        {index + 1}
      </Box>
    </Box>
  )
}

function ScenePreviewRow({ index, line, aspect, look }) {
  const secs = estimateSeconds(line)
  const long = line.length >= LINE_MAX
  return (
    <Stack direction="row" spacing={1.25} sx={{ px: 1, py: 0.75, borderRadius: 1, "&:hover": { bgcolor: "action.hover" } }}>
      <SceneFrame index={index} line={line} aspect={aspect} look={look} />
      <Box sx={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 0.25 }}>
        <Typography variant="body2" sx={{
          lineHeight: 1.4,
          display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden",
        }}>
          {line}
        </Typography>
        <Tooltip title={long ? `This scene hits the ${LINE_MAX}-character limit. Break the sentence up.` : "Estimated — the exact length comes from the voice"}>
          <Typography variant="caption" sx={{ color: long || secs > 7 ? "warning.main" : "text.secondary", alignSelf: "flex-start" }}>
            {secs.toFixed(1)}s
          </Typography>
        </Tooltip>
      </Box>
    </Stack>
  )
}
