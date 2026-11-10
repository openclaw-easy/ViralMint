// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import {
  Box, Chip, IconButton, LinearProgress, Stack, Tooltip, Typography, alpha, useTheme,
} from "@mui/material"
import AddIcon from "@mui/icons-material/Add"
import RemoveIcon from "@mui/icons-material/Remove"
import FitScreenOutlinedIcon from "@mui/icons-material/FitScreenOutlined"
import WarningAmberRoundedIcon from "@mui/icons-material/WarningAmberRounded"
import ErrorOutlineRoundedIcon from "@mui/icons-material/ErrorOutlineRounded"
import { onActivate } from "../../utils/a11y"
import { KIND_META, hashString, isShortVisual, musicLabel, musicTrackName } from "./studioModel"
import { formatClock } from "../../utils/format"
import { usePlayhead, usePlayheadStore } from "./playheadStore"

// The scene timeline: visual · voice · captions · music over one time
// axis. Scene-level editing only — click selects, drag reorders. Owns all
// px↔seconds conversion, like Clipper's bench timeline. No <video> anywhere
// in here: blocks are drawn, not decoded.
//
// Performance: the tracks are memoized on everything EXCEPT the playhead, so
// playback only moves the playhead line instead of rebuilding every block.

const LABEL_W = 78
const RULER_H = 20
const VIS_H = 60
const VOICE_H = 34
const CAP_H = 22
const MUS_H = 26


function niceStep(duration, width) {
  const target = (duration / Math.max(width, 1)) * 90
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300]
  return steps.find((s) => s >= target) || 300
}

const WaveBars = memo(function WaveBars({ seed, width, color, faint }) {
  const n = Math.max(3, Math.min(72, Math.floor(width / 5)))
  let h = hashString(seed || "x")
  const bars = []
  for (let i = 0; i < n; i += 1) {
    h = Math.imul(h ^ (i + 1), 2654435761) >>> 0
    const env = Math.sin(((i + 0.5) / n) * Math.PI) * 0.45 + 0.55
    bars.push((0.25 + ((h % 1000) / 1000) * 0.75) * env)
  }
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 2, height: "100%", padding: "0 5px", overflow: "hidden" }}>
      {bars.map((b, i) => (
        <div key={i} style={{
          width: 3, flexShrink: 0, height: `${Math.round(b * 80)}%`,
          background: color, borderRadius: 2, opacity: faint ? 0.3 : 0.9,
        }} />
      ))}
    </div>
  )
})

// The playhead line — the only part that moves during playback, and the only
// part that subscribes to the playhead store (playheadStore.js). It also keeps
// the line in view while playing.
function PlayheadLine({ clock, pps, playing, scrollRef, color }) {
  const t = usePlayhead(clock)
  const x = t * pps
  useEffect(() => {
    const el = scrollRef.current
    if (!playing || !el) return
    if (x > el.scrollLeft + el.clientWidth - 48 || x < el.scrollLeft) el.scrollLeft = Math.max(0, x - 48)
  }, [x, playing, scrollRef])
  return (
    <div data-testid="timeline-playhead" style={{
      position: "absolute", top: 0, bottom: 0, left: x, width: 2, marginLeft: -1,
      background: color, pointerEvents: "none", zIndex: 3,
    }}>
      <div style={{
        position: "absolute", top: 0, left: -4, width: 10, height: 10,
        borderRadius: 2, background: color,
      }} />
    </div>
  )
}

// Memoized: the editor re-renders for many reasons that don't touch the
// timeline, and playback doesn't re-render it at all (the playhead is a store).
function StudioTimeline({
  timeline, settings, playhead, playing, selectedUid, onSelect, onSeek, onReorder, onAddScene,
}) {
  const clock = usePlayheadStore(playhead)
  const theme = useTheme()
  const { scenes, total } = timeline
  const scrollRef = useRef(null)
  const contentRef = useRef(null)
  const [viewW, setViewW] = useState(900)
  const [zoom, setZoom] = useState(1)
  const [drag, setDrag] = useState(null)

  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return undefined
    const ro = new ResizeObserver(([e]) => setViewW(Math.round(e.contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const fitPps = total > 0 ? Math.max(6, (viewW - 72) / total) : 40
  const pps = fitPps * zoom
  const contentW = Math.max(viewW, Math.ceil(total * pps + 72))

  const music = settings?.music
  const captionsOn = !!settings?.captions?.enabled

  const tracks = useMemo(() => {
    const dark = theme.palette.mode === "dark"
    const trackBg = alpha(theme.palette.text.primary, dark ? 0.035 : 0.03)
    const m = music || {}

    const xToT = (clientX) => {
      const r = contentRef.current.getBoundingClientRect()
      return Math.max(0, Math.min(total, (clientX - r.left) / pps))
    }
    const dropIndexAt = (clientX) => {
      const t = xToT(clientX)
      const i = scenes.findIndex((s) => t < s.start + s.dur / 2)
      return i < 0 ? scenes.length : i
    }
    const onBlockDown = (e, sc) => {
      if (e.button !== 0) return
      e.stopPropagation()
      e.currentTarget.setPointerCapture?.(e.pointerId)
      setDrag({ uid: sc.uid, originX: e.clientX, originIndex: sc.index, active: false, drop: sc.index })
    }
    const onBlockMove = (e) => {
      if (!drag) return
      if (!drag.active && Math.abs(e.clientX - drag.originX) <= 5) return
      const drop = dropIndexAt(e.clientX)
      setDrag((d) => (d ? { ...d, active: true, drop } : d))
    }
    const onBlockUp = (sc) => {
      const d = drag
      setDrag(null)
      if (!d) return
      if (!d.active) { onSelect?.(sc.uid); return }
      let to = d.drop
      if (to > d.originIndex) to -= 1
      if (to === d.originIndex) return
      const order = scenes.map((s) => s.uid)
      const [moved] = order.splice(d.originIndex, 1)
      order.splice(to, 0, moved)
      onReorder?.(order)
    }

    const step = total ? niceStep(total, total * pps) : 0
    const ticks = []
    if (step) for (let t = 0; t <= total + 0.001; t += step) ticks.push(t)

    return (
      <>
        {/* Ruler */}
        <Box onPointerDown={(e) => onSeek?.(xToT(e.clientX))} sx={{ position: "relative", height: RULER_H, cursor: "pointer" }}>
          {ticks.map((t) => (
            <div key={t} style={{ position: "absolute", left: t * pps, top: 0, display: "flex", flexDirection: "column", alignItems: "flex-start" }}>
              <div style={{ width: 1, height: 6, background: "currentColor", opacity: 0.3 }} />
              <span style={{ fontSize: 10, opacity: 0.55, marginLeft: 2, lineHeight: 1.1 }}>{formatClock(t)}</span>
            </div>
          ))}
        </Box>

        {/* Visual track */}
        <Box onPointerDown={(e) => onSeek?.(xToT(e.clientX))}
          sx={{ position: "relative", height: VIS_H, mb: "3px", borderRadius: 1, bgcolor: trackBg }}>
          {scenes.map((sc) => {
            const kind = sc.visual?.kind || "stock"
            const color = KIND_META[kind]?.color(theme) || theme.palette.grey[500]
            const selected = sc.uid === selectedUid
            const empty = !sc.line
            // A failed visual is marked on the block itself — before, the
            // reason showed only on the player card, and only while the
            // playhead sat on that scene (audit 2026-09-19).
            const failed = sc.visual?.state === "failed"
            const isShort = isShortVisual(sc)
            const w = Math.max(10, sc.dur * pps - 3)
            const dragging = drag?.active && drag.uid === sc.uid
            return (
              <Box
                key={sc.uid}
                data-scene={sc.index + 1}
                // Reachable without a mouse (2026-10-13 audit): Tab to a scene,
                // Enter/Space opens it, Alt+←/→ moves it.
                role="button" tabIndex={0}
                aria-label={`Scene ${sc.index + 1}: ${(sc.line || "empty line").slice(0, 80)}`}
                aria-pressed={selected}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); onSelect?.(sc.uid) }
                  else if (e.altKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
                    e.preventDefault(); e.stopPropagation()
                    const order = scenes.map((x) => x.uid)
                    const i = order.indexOf(sc.uid)
                    const j = i + (e.key === "ArrowLeft" ? -1 : 1)
                    if (j < 0 || j >= order.length) return
                    ;[order[i], order[j]] = [order[j], order[i]]
                    onReorder?.(order)
                  }
                }}
                onPointerDown={(e) => onBlockDown(e, sc)}
                onPointerMove={onBlockMove}
                onPointerUp={() => onBlockUp(sc)}
                onPointerCancel={() => setDrag(null)}
                sx={{
                  position: "absolute", top: 3, bottom: 3, left: sc.start * pps, width: w,
                  borderRadius: 1, overflow: "hidden", cursor: drag?.active ? "grabbing" : "grab",
                  border: selected ? 2 : 1,
                  borderStyle: empty ? "dashed" : "solid",
                  borderColor: selected ? "primary.main" : failed ? "error.main" : empty ? "warning.main" : alpha(color, 0.65),
                  bgcolor: alpha(color, dark ? 0.24 : 0.16),
                  opacity: dragging ? 0.45 : 1,
                  transition: "left .18s ease, width .18s ease",
                  "&:hover": { bgcolor: alpha(color, dark ? 0.32 : 0.24) },
                  "&:focus-visible": { outline: "2px solid", outlineColor: "primary.main", outlineOffset: 1 },
                }}
              >
                <Box sx={{ height: 3, bgcolor: color }} />
                <Box sx={{ px: 0.75, py: 0.35, minWidth: 0 }}>
                  <Typography sx={{ fontSize: 11, fontWeight: 800, lineHeight: 1.25 }} noWrap>
                    {sc.index + 1}{w > 70 ? ` · ${KIND_META[kind]?.short || kind}` : ""}
                  </Typography>
                  {(w > 46 || empty) && (
                    // An empty scene is the shortest block on the track and the one that needs a label most.
                    <Typography sx={{ fontSize: 10.5, color: empty ? "warning.main" : "text.secondary", fontWeight: empty ? 700 : 400, lineHeight: 1.25 }} noWrap>
                      {empty ? "no line yet" : sc.visual?.asset?.title || sc.visual?.asset?.query || sc.visual?.query || ""}
                    </Typography>
                  )}
                  {w > 46 && (
                    <Typography sx={{ fontSize: 10, color: "text.disabled", lineHeight: 1.25 }} noWrap>
                      {sc.dur.toFixed(1)}s{sc.estimated ? " est." : ""}
                    </Typography>
                  )}
                </Box>
                {sc.visual?.state === "generating" && (
                  <LinearProgress sx={{ position: "absolute", left: 0, right: 0, bottom: 0, height: 2 }} />
                )}
                {failed && (
                  <Tooltip title={`This scene has no usable visual${sc.visual.reason ? ` — ${sc.visual.reason}` : ""}. Select the scene to pick another clip.`}>
                    <ErrorOutlineRoundedIcon aria-label={`Scene ${sc.index + 1} visual failed`}
                      sx={{ position: "absolute", top: 5, right: 3, fontSize: 13, color: "error.main" }} />
                  </Tooltip>
                )}
                {isShort && !failed && (
                  <Tooltip title={`Clip is ${(sc.dur - sc.visual.asset.native_duration).toFixed(1)}s shorter than the line — it will ${sc.visual.fit || "loop"}`}>
                    <WarningAmberRoundedIcon sx={{ position: "absolute", top: 5, right: 3, fontSize: 13, color: "warning.main" }} />
                  </Tooltip>
                )}
              </Box>
            )
          })}
          {drag?.active && (
            <Box sx={{
              position: "absolute", top: -2, bottom: -2, width: 3, ml: "-2px", borderRadius: 2,
              left: (drag.drop < scenes.length ? scenes[drag.drop].start : total) * pps,
              bgcolor: "primary.main", pointerEvents: "none",
            }} />
          )}
          <Tooltip title="Add a scene at the end">
            <Box
              // Was role="button" with neither tabIndex nor a key handler:
              // announced as a button, reachable by nobody.
              role="button" tabIndex={0} aria-label="Add scene"
              onPointerDown={(e) => e.stopPropagation()}
              onClick={() => onAddScene?.()}
              onKeyDown={onActivate(() => onAddScene?.())}
              sx={{
                position: "absolute", top: 3, bottom: 3, left: total * pps + 6, width: 42,
                borderRadius: 1, border: "1.5px dashed", borderColor: "divider",
                display: "grid", placeItems: "center", cursor: "pointer", color: "text.secondary",
                "&:hover": { borderColor: "primary.main", color: "primary.main" },
              }}
            >
              <AddIcon fontSize="small" />
            </Box>
          </Tooltip>
        </Box>

        {/* Voice track */}
        <Box onPointerDown={(e) => onSeek?.(xToT(e.clientX))}
          sx={{ position: "relative", height: VOICE_H, mb: "3px", borderRadius: 1, bgcolor: trackBg }}>
          {scenes.map((sc) => {
            const state = sc.voice?.state || "none"
            const w = Math.max(10, sc.dur * pps - 3)
            const color = state === "stale" ? theme.palette.warning.main
              : state === "failed" ? theme.palette.error.main : theme.palette.info.main
            const faint = state !== "ready" && state !== "stale"
            return (
              <Tooltip key={sc.uid} title={
                state === "ready" ? `Voice take · ${sc.voice.duration?.toFixed(1)}s${sc.voice.cached ? " · from cache" : ""}`
                  : state === "stale" ? "The line or voice changed — this take no longer matches"
                    : state === "generating" ? "Voicing…"
                      : state === "failed" ? `Voice failed${sc.voice?.error ? ` — ${sc.voice.error}` : ""}. Retry from the scene panel.`
                        : "Not voiced yet"
              }>
                <Box sx={{
                  position: "absolute", top: 3, bottom: 3, left: sc.start * pps, width: w,
                  borderRadius: 1, overflow: "hidden",
                  border: faint ? "1px dashed" : "1px solid",
                  borderColor: alpha(color, faint ? 0.4 : 0.35),
                  bgcolor: alpha(color, faint ? 0.03 : 0.1),
                  animation: state === "generating" ? "vmPulse 1s ease-in-out infinite alternate" : "none",
                  "@keyframes vmPulse": { from: { opacity: 0.45 }, to: { opacity: 1 } },
                }}>
                  <WaveBars seed={`${sc.uid}|${sc.line}`} width={w} color={color} faint={faint} />
                </Box>
              </Tooltip>
            )
          })}
        </Box>

        {/* Captions track */}
        {captionsOn && (
          <Box sx={{ position: "relative", height: CAP_H, mb: "3px", borderRadius: 1, bgcolor: trackBg }}>
            {scenes.map((sc) => (
              <div key={sc.uid} style={{
                position: "absolute", top: 2, bottom: 2, left: sc.start * pps, width: Math.max(10, sc.dur * pps - 3),
                overflow: "hidden", whiteSpace: "nowrap", textOverflow: "ellipsis",
                fontSize: 10.5, lineHeight: `${CAP_H - 4}px`, padding: "0 6px", opacity: 0.75,
              }}>
                {sc.line}
              </div>
            ))}
          </Box>
        )}

        {/* Music track */}
        <Box sx={{ position: "relative", height: MUS_H, borderRadius: 1, bgcolor: trackBg }}>
          {m.enabled ? (
            <Box sx={{
              position: "absolute", top: 3, bottom: 3, left: 0, width: Math.max(10, total * pps - 3),
              borderRadius: 1, px: 1, display: "flex", alignItems: "center",
              bgcolor: alpha(theme.palette.success.main, dark ? 0.18 : 0.12),
              border: 1, borderColor: alpha(theme.palette.success.main, 0.35),
            }}>
              <Typography sx={{ fontSize: 11, fontWeight: 600 }} noWrap>
                ♪ {musicTrackName(m) || `${musicLabel(m)} — Project → Music`}
                {musicTrackName(m) ? ` · ${m.volume_db ?? -14} dB${m.offset_s ? ` · from ${formatClock(m.offset_s)}` : ""}` : ""}
              </Typography>
            </Box>
          ) : (
            <Typography sx={{ fontSize: 11, color: "text.disabled", lineHeight: `${MUS_H}px`, px: 1 }}>Music off</Typography>
          )}
        </Box>
      </>
    )
  }, [scenes, total, pps, drag, selectedUid, theme, music, captionsOn, onSeek, onSelect, onReorder, onAddScene])

  const unvoiced = scenes.filter((s) => s.voice?.state !== "ready").length
  const empty = scenes.filter((s) => !s.line).length
  const short = scenes.filter(isShortVisual).length

  const rows = [
    { key: "ruler", label: "", h: RULER_H },
    { key: "visual", label: "Visual", h: VIS_H },
    { key: "voice", label: "Voice", h: VOICE_H },
    ...(captionsOn ? [{ key: "caps", label: "Captions", h: CAP_H }] : []),
    { key: "music", label: "Music", h: MUS_H },
  ]

  return (
    <Box sx={{ flexShrink: 0, borderTop: 1, borderColor: "divider", bgcolor: "background.paper", userSelect: "none" }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ px: 1.5, py: 0.5, minHeight: 36 }}>
        <Typography variant="overline" sx={{ fontWeight: 800, color: "text.secondary", lineHeight: 1 }}>Timeline</Typography>
        <Typography variant="caption" sx={{ color: "text.secondary" }}>
          {scenes.length} scenes · {formatClock(total, { decimals: 1 })}
        </Typography>
        <Stack direction="row" spacing={0.5} sx={{ ml: 1, display: { xs: "none", md: "flex" } }}>
          {Object.entries(KIND_META).map(([k, meta]) => (
            <Stack key={k} direction="row" spacing={0.4} alignItems="center">
              <Box sx={{ width: 8, height: 8, borderRadius: "2px", bgcolor: meta.color(theme) }} />
              <Typography variant="caption" sx={{ color: "text.secondary", fontSize: 11 }}>{meta.label}</Typography>
            </Stack>
          ))}
        </Stack>
        <Box sx={{ flex: 1 }} />
        {empty > 0 && (
          <Chip size="small" variant="outlined" color="warning" icon={<WarningAmberRoundedIcon sx={{ fontSize: 14 }} />}
            label={`${empty} scene${empty === 1 ? "" : "s"} without a line`} sx={{ height: 20, fontSize: 11 }} />
        )}
        {unvoiced > 0 && <Chip size="small" variant="outlined" label={`${unvoiced} not voiced`} sx={{ height: 20, fontSize: 11 }} />}
        {short > 0 && (
          <Chip size="small" variant="outlined" color="warning" icon={<WarningAmberRoundedIcon sx={{ fontSize: 14 }} />}
            label={`${short} clip${short === 1 ? "" : "s"} shorter than the line`} sx={{ height: 20, fontSize: 11 }} />
        )}
        <Tooltip title="Zoom out"><span><IconButton size="small" aria-label="Zoom out" disabled={zoom <= 1} onClick={() => setZoom((z) => Math.max(1, z / 1.5))}><RemoveIcon fontSize="small" /></IconButton></span></Tooltip>
        <Tooltip title="Fit"><IconButton size="small" onClick={() => setZoom(1)}><FitScreenOutlinedIcon fontSize="small" /></IconButton></Tooltip>
        <Tooltip title="Zoom in"><span><IconButton size="small" aria-label="Zoom in" disabled={zoom >= 8} onClick={() => setZoom((z) => Math.min(8, z * 1.5))}><AddIcon fontSize="small" /></IconButton></span></Tooltip>
      </Stack>

      <Box sx={{ display: "flex", pb: 1 }}>
        {/* Track labels */}
        <Box sx={{ width: LABEL_W, flexShrink: 0 }}>
          {rows.map((r) => (
            <Box key={r.key} sx={{ height: r.h, mb: r.key === "ruler" ? 0 : "3px", display: "flex", alignItems: "center", pl: 1.5 }}>
              <Typography variant="caption" sx={{ fontWeight: 700, color: "text.secondary", fontSize: 11 }}>{r.label}</Typography>
            </Box>
          ))}
        </Box>

        {/* Tracks */}
        <Box ref={scrollRef} sx={{ flex: 1, minWidth: 0, overflowX: "auto", overflowY: "hidden", pr: 1.5 }}>
          <Box ref={contentRef} sx={{ position: "relative", width: contentW }}>
            {tracks}
            <PlayheadLine clock={clock} pps={pps} playing={playing} scrollRef={scrollRef} color={theme.palette.error.main} />
          </Box>
        </Box>
      </Box>
    </Box>
  )
}

export default memo(StudioTimeline)
