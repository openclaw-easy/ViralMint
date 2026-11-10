// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import {
  Box, Button, Chip, CircularProgress, IconButton, ListItemIcon, ListItemText, Menu, MenuItem,
  Stack, Tooltip, Typography, alpha,
} from "@mui/material"
import { keyframes } from "@emotion/react"
import PlayArrowRoundedIcon from "@mui/icons-material/PlayArrowRounded"
import PauseRoundedIcon from "@mui/icons-material/PauseRounded"
import SkipPreviousRoundedIcon from "@mui/icons-material/SkipPreviousRounded"
import SkipNextRoundedIcon from "@mui/icons-material/SkipNextRounded"
import RecordVoiceOverOutlinedIcon from "@mui/icons-material/RecordVoiceOverOutlined"
import VoiceOverOffOutlinedIcon from "@mui/icons-material/VoiceOverOffOutlined"
import MusicNoteOutlinedIcon from "@mui/icons-material/MusicNoteOutlined"
import MusicOffOutlinedIcon from "@mui/icons-material/MusicOffOutlined"
import RepeatOneRoundedIcon from "@mui/icons-material/RepeatOneRounded"
import FullscreenRoundedIcon from "@mui/icons-material/FullscreenRounded"
import FullscreenExitRoundedIcon from "@mui/icons-material/FullscreenExitRounded"
import CloseRoundedIcon from "@mui/icons-material/CloseRounded"
import VolumeOffRoundedIcon from "@mui/icons-material/VolumeOffRounded"
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined"
import MoreHorizRoundedIcon from "@mui/icons-material/MoreHorizRounded"
import { CAPTION_STYLES } from "../tools/captionOptions"
import {
  HOOK_SECONDS, STYLE_FILTER, aspectValue, hookIsAuto, hookTitle, musicLabel, sceneAt, secondShots,
} from "./studioModel"
import { useCandidateUrl, useSceneMedia } from "./studioMedia"
import { takeMeta, voiceUrl } from "./studioApi"
import { formatClock } from "../../utils/format"
import { usePlayhead, usePlayheadStore } from "./playheadStore"

// The sequence player: one clock drives the visual, the voice, the music
// bed and the caption overlay, scene by scene. At most two <video> elements
// exist — the current scene and the next one, preloaded — and React's keyed
// reconciliation hands the preloaded element over at the boundary.
//
// The voice is the scene's real take and the footage is what the render uses;
// captions, colour grade and motion are approximations of the render.

// The player's chip for a scene it can't voice yet (D27: a stale or unvoiced
// scene plays silent rather than with the wrong take).
const SILENT_LABEL = {
  none: "Not voiced yet — plays silent",
  generating: "Still voicing — plays silent",
  failed: "Voice failed — plays silent",
  stale: "Line or voice changed — re-voice to hear it",
}

const kenBurns = keyframes`
  from { transform: scale(1.04) translate(0, 0); }
  to   { transform: scale(1.16) translate(-2%, -1.5%); }
`

function useFitBox(ratio) {
  const ref = useRef(null)
  const [box, setBox] = useState({ w: 0, h: 0 })
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return undefined
    const measure = (W, H) => {
      let w = W
      let h = W / ratio
      if (h > H) { h = H; w = H * ratio }
      setBox({ w: Math.max(0, Math.floor(w)), h: Math.max(0, Math.floor(h)) })
    }
    const ro = new ResizeObserver(([e]) => measure(e.contentRect.width, e.contentRect.height))
    ro.observe(el)
    const r = el.getBoundingClientRect()
    measure(r.width, r.height)
    return () => ro.disconnect()
  }, [ratio])
  return [ref, box]
}

const PREVIEW_NOTE = "Preview approximates captions, colour grade and motion — the render is exact."

// Below this transport width the secondary toggles (voice, music, loop, info)
// move into an overflow menu instead of wrapping onto a second row. The full
// row is ~400px: 3 transport buttons + clock + 5 toggles + Fullscreen.
export const TRANSPORT_FULL_PX = 420

function useNarrow(threshold) {
  const ref = useRef(null)
  const [narrow, setNarrow] = useState(false)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return undefined
    const apply = (w) => { if (w > 0) setNarrow(w < threshold) }
    const ro = new ResizeObserver(([e]) => apply(e.contentRect.width))
    ro.observe(el)
    apply(el.getBoundingClientRect().width)
    return () => ro.disconnect()
  }, [threshold])
  return [ref, narrow]
}

// The paused player's centre play glyph. Drawn by the visual itself and only
// over real media: over "Loading footage…", "Couldn't load… / Try again" or an
// AI placeholder card it sat on top of the message (audit 2026-09-25).
function PlayHint() {
  return (
    <Box data-testid="play-hint" sx={{
      position: "absolute", left: "50%", top: "50%", transform: "translate(-50%, -50%)",
      width: 58, height: 58, borderRadius: "50%", display: "grid", placeItems: "center",
      bgcolor: "rgba(0,0,0,0.45)", pointerEvents: "none",
    }}>
      <PlayArrowRoundedIcon sx={{ fontSize: 38, color: "#fff" }} />
    </Box>
  )
}

const SceneVisual = memo(function SceneVisual({ projectId, scene, local: sceneLocal, playing, hidden, paused = false, shot = null }) {
  const visual = scene?.visual || {}
  const main = useSceneMedia(projectId, visual)
  // A long stock scene cuts to its second shot halfway (studioModel.secondShots
  // — the render cuts at the same point). One <video> element switches source.
  const altVisual = useMemo(() => (shot ? { ...visual, asset: shot.asset } : null), [shot, visual])
  const alt = useSceneMedia(projectId, altVisual)
  const onSecond = !!(shot && alt.url && sceneLocal >= shot.cut)
  const local = onSecond ? sceneLocal - shot.cut : sceneLocal
  const { url, image: still, failed: previewFailed, retry: retryPreview } = onSecond ? alt : main
  const vref = useRef(null)
  const fit = visual.fit || "loop"

  useEffect(() => {
    const v = vref.current
    if (!v) return
    if (!hidden && playing && !still) v.play().catch(() => {})
    else v.pause()
  }, [playing, url, hidden, still])

  // Map scene-local time into the clip (D11). The rate comes from the clip's
  // REAL duration, and the clip position is local × rate for every fit:
  //   loop   rate 1, wraps (the element's own `loop` keeps it seamless)
  //   slow   rate ≥ 0.75 — plays through to its real end, then holds the last
  //          frame if even 0.75× can't fill the slot
  //   freeze rate 1, holds the last frame once the clip runs out
  // A held frame pauses the element; anything else resumes it, so seeking back
  // or looping the scene after a hold plays again.
  useEffect(() => {
    const v = vref.current
    if (!v || hidden || !url) return
    const d = v.duration
    if (!Number.isFinite(d) || d <= 0) return
    const slot = shot ? (onSecond ? scene.dur - shot.cut : shot.cut) : scene.dur
    const rate = !still && fit === "slow" && d < slot ? Math.max(0.75, d / slot) : 1
    if (v.playbackRate !== rate) v.playbackRate = rate
    let target
    let hold = false
    if (still) {
      target = Math.min(1, d / 2)
    } else {
      const pos = local * rate
      if (pos < d - 0.05) target = pos
      else if (fit === "loop") target = pos % d
      else { target = Math.max(0, d - 0.05); hold = true }
    }
    if (Math.abs(v.currentTime - target) > 0.45) {
      try { v.currentTime = target } catch { /* not seekable yet */ }
    }
    if (still) return
    if (hold) { if (!v.paused) v.pause() } else if (playing && v.paused) v.play().catch(() => {})
  }, [local, url, hidden, still, fit, scene?.dur, playing, shot, onSecond])

  const layer = { position: "absolute", inset: 0, opacity: hidden ? 0 : 1, pointerEvents: "none" }

  if (!url) {
    const pickedAtRender = visual.kind === "stock" && !visual.asset
    return hidden ? null : (
      <Stack alignItems="center" justifyContent="center" spacing={1} sx={{ ...layer, color: "grey.400", px: 3, textAlign: "center" }}>
        {pickedAtRender ? (
          <Typography variant="caption">
            Footage for “{visual.query}” is picked at render — Re-plan visuals to preview it now.
          </Typography>
        ) : previewFailed ? (
          <>
            <Typography variant="caption">Couldn’t load this clip’s preview — check your connection.</Typography>
            <Button size="small" variant="outlined" onClick={retryPreview}
              sx={{ pointerEvents: "auto", color: "grey.200", borderColor: "grey.600", textTransform: "none" }}>
              Try again
            </Button>
          </>
        ) : (
          <>
            <CircularProgress size={18} sx={{ color: "grey.500" }} />
            <Typography variant="caption">Loading footage…</Typography>
          </>
        )}
      </Stack>
    )
  }
  const hint = !hidden && paused ? <PlayHint /> : null
  if (still) {
    return (
      <>
        <Box component="img" src={url} alt="" sx={{
          ...layer, width: "100%", height: "100%", objectFit: "cover",
          ...(!hidden ? { animation: `${kenBurns} ${Math.max(3, scene.dur)}s ease-in-out infinite alternate` } : {}),
        }} />
        {hint}
      </>
    )
  }
  return (
    <>
      <Box
        ref={vref} component="video" src={url} muted playsInline preload="auto"
        loop={fit === "loop"}
        sx={{ ...layer, width: "100%", height: "100%", objectFit: "cover" }}
      />
      {hint}
    </>
  )
})

// A sized preset's caption scale against the frame's SHORT side: the burn uses
// font_size_portrait on 9:16 and font_size_landscape on 16:9 / 1:1 (both on a
// 1080-px short side), so the preview must pick the same one — on landscape it
// showed Pop ~37% bigger than it burns (release audit 2026-10-01). Older
// presets keep the fixed scale.
function captionScale(st, box) {
  if (!st.size) return 0.068
  const portrait = box.h > box.w
  return (portrait ? st.size : (st.sizeLandscape || st.size)) / 1080
}

function CaptionOverlay({ scene, local, styleId, box, tailGap, words }) {
  const st = (CAPTION_STYLES.find((s) => s.value === styleId) || CAPTION_STYLES.find((s) => s.value === "viral")).preview
  const line = scene.line || ""
  // CJK scripts highlight per character; everything else per word.
  const cjk = /[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]/.test(line) && !/\s/.test(line.trim())
  const timed = !cjk && Array.isArray(words) && words.length > 0
  const tokens = timed ? words.map((w) => w.word) : cjk ? Array.from(line) : line.split(/\s+/).filter(Boolean)
  if (!tokens.length) return null
  const speak = Math.max(0.3, scene.dur - tailGap)
  const frac = Math.min(0.999, Math.max(0, local / speak))
  // With the take's word timings (D12) the highlight follows the voice;
  // without them it is spread evenly across the line.
  const idx = timed
    ? words.reduce((found, w, i) => (w.start <= local ? i : found), 0)
    : Math.min(tokens.length - 1, Math.floor(frac * tokens.length))
  const size = cjk ? Math.max(6, (st.words || 4) * 2) : Math.max(1, st.words || 4)
  const g0 = Math.floor(idx / size) * size
  const group = tokens.slice(g0, g0 + size)
  const px = Math.max(12, Math.min(box.w, box.h) * captionScale(st, box))
  const o = st.outline || "#000"
  return (
    <Box data-testid="caption-overlay" sx={{
      position: "absolute", left: "6%", right: "6%", bottom: "16%", textAlign: "center",
      fontFamily: st.font, fontWeight: st.weight || 700, fontSize: px, lineHeight: 1.2,
      textShadow: `0 0 3px ${o}, 0 0 3px ${o}, 2px 2px 0 ${o}`, pointerEvents: "none",
    }}>
      {group.map((w, i) => (
        <span key={`${g0}-${i}`} style={{ color: g0 + i === idx ? st.highlight : st.text }}>
          {st.upper ? String(w).toUpperCase() : w}{cjk ? "" : " "}
        </span>
      ))}
    </Box>
  )
}

function PreviewLayer({ projectId, preview, onClose, onUse, selectedIndex, filter }) {
  const candidate = useCandidateUrl(projectId, preview.url ? null : preview.candidate)
  const url = preview.url || candidate.url
  return (
    <>
      {url ? (
        preview.image ? (
          <Box component="img" key={url} src={url} alt=""
            sx={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover", filter }} />
        ) : (
          <Box component="video" key={url} src={url} autoPlay muted loop playsInline
            sx={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover", filter }} />
        )
      ) : candidate.failed ? (
        <Stack alignItems="center" justifyContent="center" spacing={1} sx={{ position: "absolute", inset: 0, color: "grey.300", px: 3, textAlign: "center" }}>
          <Typography variant="caption">Couldn’t load this preview — check your connection.</Typography>
          <Button size="small" variant="outlined" onClick={candidate.retry}
            sx={{ color: "grey.200", borderColor: "grey.600", textTransform: "none" }}>Try again</Button>
        </Stack>
      ) : (
        <Stack alignItems="center" justifyContent="center" sx={{ position: "absolute", inset: 0 }}>
          <CircularProgress size={20} sx={{ color: "grey.500" }} />
        </Stack>
      )}
      <Stack direction="row" alignItems="center" justifyContent="space-between"
        sx={{ position: "absolute", top: 8, left: 8, right: 8 }}>
        <Stack spacing={0.5} sx={{ maxWidth: "78%", alignItems: "flex-start" }}>
          <Chip size="small" label={`Previewing · ${preview.label || preview.query}`}
            sx={{ bgcolor: "rgba(0,0,0,0.62)", color: "#fff", maxWidth: "100%", fontWeight: 600 }} />
          {/* The render keeps a clip's PICTURE only (studio_render._ENCODE is
              -an): the soundtrack is the voiceover + music bed. The preview is
              muted to match — say so, or a silent preview of the user's own
              clip reads as a broken player (manual test 2026-09-19). */}
          {!preview.image && (
            <Chip size="small" icon={<VolumeOffRoundedIcon sx={{ fontSize: 14, color: "#fff !important" }} />}
              label="Muted — Smart Video uses your voiceover and music, not the clip's sound"
              sx={{ bgcolor: "rgba(0,0,0,0.55)", color: "#fff", maxWidth: "100%", height: "auto",
                    "& .MuiChip-label": { whiteSpace: "normal", py: 0.25 } }} />
          )}
        </Stack>
        <IconButton size="small" aria-label="Close preview" onClick={onClose}
          sx={{ color: "#fff", bgcolor: "rgba(0,0,0,0.55)", "&:hover": { bgcolor: "rgba(0,0,0,0.75)" } }}>
          <CloseRoundedIcon fontSize="small" />
        </IconButton>
      </Stack>
      {onUse && (
        <Button
          variant="contained" size="small" onClick={onUse} disabled={selectedIndex == null}
          sx={{ position: "absolute", bottom: 14, left: "50%", transform: "translateX(-50%)", textTransform: "none", fontWeight: 700, whiteSpace: "nowrap" }}
        >
          {selectedIndex == null ? "Select a scene in the timeline to use this" : `Use for scene ${selectedIndex + 1}`}
        </Button>
      )}
    </>
  )
}

// ── Playhead subscribers ──────────────────────────────────────────────
// The parts below are the only ones that re-render as the playhead moves
// (playheadStore.js); SequencePlayer itself re-renders at scene boundaries.

// The opening title the render burns for its first HOOK_SECONDS
// (caption_service Hook style: top-centre, white, thick black edge).
function HookOverlay({ t, text, styleId, box }) {
  if (!text || t >= HOOK_SECONDS) return null
  const st = (CAPTION_STYLES.find((s) => s.value === styleId) || CAPTION_STYLES.find((s) => s.value === "viral")).preview
  const scale = st.size ? captionScale(st, box) * 1.3 : 0.068 * 1.6
  const px = Math.max(14, Math.min(box.w, box.h) * scale)
  return (
    <Box data-testid="hook-overlay" sx={{
      position: "absolute", left: "6%", right: "6%", top: "11%", textAlign: "center",
      fontFamily: st.font, fontWeight: 800, fontSize: px, lineHeight: 1.1, color: "#fff",
      textShadow: "0 0 4px #000, 0 0 4px #000, 3px 3px 0 #000", pointerEvents: "none",
      // Fade OUT only: the burn fades in over 0.3 s, but a player paused at
      // 0:00 (where every editor opens) must show the title, not nothing.
      opacity: t > HOOK_SECONDS - 0.5 ? Math.max(0, (HOOK_SECONDS - t) / 0.5) : 1,
    }}>
      {text}
    </Box>
  )
}

// The pictures (current + preloaded next) and the caption overlay.
function FrameLayers({ clock, projectId, cur, next, playing, filter, captions, box, tailGap, words, hookText, hookAuto, shots }) {
  const t = usePlayhead(clock)
  const local = cur ? Math.max(0, t - cur.start) : 0
  return (
    <>
      <Box sx={{ position: "absolute", inset: 0, filter }}>
        {[cur, next].filter(Boolean).map((sc) => (
          <SceneVisual
            key={sc.uid} projectId={projectId} scene={sc}
            local={sc === cur ? local : 0}
            playing={playing && sc === cur}
            paused={!playing && sc === cur}
            hidden={sc !== cur}
            shot={shots?.[sc.uid] || null}
          />
        ))}
      </Box>
      {captions?.enabled && cur && box.w > 0 && !(hookAuto && t < HOOK_SECONDS) && (
        <CaptionOverlay scene={cur} local={local} styleId={captions.style} box={box} tailGap={tailGap} words={words} />
      )}
      {box.w > 0 && <HookOverlay t={t} text={hookText} styleId={captions?.style} box={box} />}
    </>
  )
}

// The voice take and the music bed, kept in step with the playhead.
function PlaybackAudio({ clock, cur, total, playing, previewing, voiceMuted, musicMuted, takeKey, voiced, music, musicUrl }) {
  const t = usePlayhead(clock)
  const local = cur ? Math.max(0, t - cur.start) : 0

  // ── Voice: the scene's real take ─────────────────────────────
  // Only a READY take matches its line (D27); a stale scene plays silent with a
  // "line changed" badge. The take plays from the scene-local time and stops at
  // its own end, so the pause after the line is silence, exactly as rendered.
  const voiceRef = useRef(null)
  useEffect(() => {
    const a = voiceRef.current
    if (!a) return
    const speaking = playing && !previewing && !voiceMuted && voiced
    const d = cur?.voice?.duration || (Number.isFinite(a.duration) ? a.duration : 0)
    if (!speaking || (d && local >= d)) { if (!a.paused) a.pause(); return }
    // Resync only once the take is loaded and not mid-seek: seeking a take that
    // is still buffering (it just landed) restarts the load every tick and it
    // never starts.
    if (a.readyState >= 1 && !a.seeking && Math.abs(a.currentTime - local) > 0.25) {
      try { a.currentTime = local } catch { /* not ready */ }
    }
    if (a.paused) a.play().catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [takeKey, playing, previewing, voiceMuted, voiced, local])

  // ── Music bed ────────────────────────────────────────────────
  const audioRef = useRef(null)
  // D13: the bed sits at volume_db under the voice (gain = 10^(dB/20), exactly
  // what the render mixes) with fade_s in at 0 and out at the end.
  useEffect(() => {
    const a = audioRef.current
    if (!a) return
    const gain = Math.pow(10, (music.volume_db ?? -14) / 20)
    const fade = Math.max(0, music.fade_s ?? 0.5)
    const env = fade > 0 ? Math.max(0, Math.min(1, t / fade, (total - t) / fade)) : 1
    a.volume = Math.max(0, Math.min(1, gain * env))
  }, [music.volume_db, music.fade_s, musicUrl, t, total])
  useEffect(() => {
    const a = audioRef.current
    if (!a) return
    if (playing && !previewing && !musicMuted) a.play().catch(() => {})
    else a.pause()
  }, [playing, previewing, musicMuted, musicUrl])
  useEffect(() => {
    const a = audioRef.current
    if (!a || !musicUrl) return
    const d = a.duration
    const target = (music.offset_s || 0) + t
    const tt = Number.isFinite(d) && d > 0 ? target % d : target
    if (Math.abs(a.currentTime - tt) > 0.8) {
      try { a.currentTime = tt } catch { /* not ready */ }
    }
  }, [t, musicUrl, music.offset_s])

  return (
    <>
      {musicUrl && <audio ref={audioRef} src={musicUrl} loop preload="auto" />}
      {takeKey && <audio ref={voiceRef} key={takeKey} src={voiceUrl(takeKey)} preload="auto" />}
    </>
  )
}

// The scrub bar is a real slider (audit 2026-09-25): it was a click-only div —
// no drag, not focusable, invisible to a screen reader. Now it drags with the
// pointer (captured, so leaving the bar mid-drag keeps scrubbing), takes
// ←/→ (1 s), PageUp/PageDown (5 s), Home/End, and announces its position.
// Handled keys stop here: the editor's window-level ←/→ step whole scenes.
const SCRUB_STEP_S = 1
const SCRUB_PAGE_S = 5

export function ScrubBar({ clock, total, onSeek }) {
  const t = usePlayhead(clock)
  const pct = total > 0 ? Math.min(100, (t / total) * 100) : 0
  const dragging = useRef(false)
  const clamp = (v) => Math.max(0, Math.min(total, v))
  const fromPointer = (e) => {
    const r = e.currentTarget.getBoundingClientRect()
    if (!r.width) return null
    return clamp(((e.clientX - r.left) / r.width) * total)
  }
  const onKeyDown = (e) => {
    const now = clock.get()
    let to = null
    switch (e.key) {
      case "ArrowLeft": case "ArrowDown": to = now - SCRUB_STEP_S; break
      case "ArrowRight": case "ArrowUp": to = now + SCRUB_STEP_S; break
      case "PageDown": to = now - SCRUB_PAGE_S; break
      case "PageUp": to = now + SCRUB_PAGE_S; break
      case "Home": to = 0; break
      case "End": to = total; break
      default: return
    }
    e.preventDefault()
    e.stopPropagation()
    onSeek?.(clamp(to))
  }
  return (
    <Box
      role="slider" tabIndex={total > 0 ? 0 : -1}
      aria-label="Playback position"
      aria-valuemin={0}
      aria-valuemax={Math.round(total * 10) / 10}
      aria-valuenow={Math.round(Math.min(t, total) * 10) / 10}
      aria-valuetext={`${formatClock(t)} of ${formatClock(total)}`}
      aria-disabled={total > 0 ? undefined : true}
      onKeyDown={onKeyDown}
      onPointerDown={(e) => {
        if (e.button !== 0 || !(total > 0)) return
        dragging.current = true
        e.currentTarget.setPointerCapture?.(e.pointerId)
        const v = fromPointer(e)
        if (v != null) onSeek?.(v)
      }}
      onPointerMove={(e) => {
        if (!dragging.current) return
        const v = fromPointer(e)
        if (v != null && v !== clock.get()) onSeek?.(v)
      }}
      onPointerUp={(e) => { dragging.current = false; e.currentTarget.releasePointerCapture?.(e.pointerId) }}
      onPointerCancel={() => { dragging.current = false }}
      sx={(th) => ({
        mx: 2, height: 14, display: "flex", alignItems: "center", cursor: "pointer",
        touchAction: "none", outline: "none", borderRadius: 1,
        "&:focus-visible": { boxShadow: `0 0 0 2px ${alpha(th.palette.primary.main, 0.7)}` },
        "&:hover .vm-scrub-thumb, &:focus-visible .vm-scrub-thumb": { transform: "translate(-50%, -50%) scale(1)" },
        "&:hover .vm-scrub-track, &:focus-visible .vm-scrub-track": { height: 5 },
      })}
    >
      <Box className="vm-scrub-track" sx={{ position: "relative", width: "100%", height: 3, borderRadius: 2, bgcolor: "rgba(255,255,255,0.14)", transition: "height 120ms" }}>
        <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: `${pct}%`, borderRadius: 2, background: "var(--vm-scrub, #1976d2)" }} />
        <Box className="vm-scrub-thumb" style={{ left: `${pct}%` }} sx={{
          position: "absolute", top: "50%", width: 12, height: 12, borderRadius: "50%",
          bgcolor: "var(--vm-scrub, #1976d2)", boxShadow: "0 0 0 2px rgba(0,0,0,0.35)",
          transform: "translate(-50%, -50%) scale(0)", transition: "transform 120ms", pointerEvents: "none",
        }} />
      </Box>
    </Box>
  )
}

function ClockText({ clock, total }) {
  // Tenths of a second is all the clock shows — re-render only when THAT changes.
  const tenths = usePlayhead(clock, (t) => Math.floor(t * 10))
  return (
    <Typography variant="caption" sx={{ fontVariantNumeric: "tabular-nums", ml: 1, opacity: 0.9, whiteSpace: "nowrap" }}>
      {formatClock(tenths / 10, { decimals: 1 })} / {formatClock(total, { decimals: 1 })}
    </Typography>
  )
}

// `playhead` is a playhead store (playheadStore.js) — or a plain number, for
// callers that don't play (tests). With a store, the player drives it
// directly; `onTick` is still called for callers that want the value.
export default function SequencePlayer({
  timeline, settings, aspectRatio, playhead, playing, onTick, onPlayingChange, onSeek,
  preview, onClosePreview, onUsePreview, selectedIndex,
  voiceMuted, onToggleVoice, musicMuted, onToggleMusic, loopScene, onToggleLoop,
  onPrev, onNext, onTogglePlay, projectId, playDisabled = false,
}) {
  const { scenes, total } = timeline
  const shots = useMemo(() => secondShots(scenes), [scenes])
  const clock = usePlayheadStore(playhead)
  const [boxRef, box] = useFitBox(aspectValue(aspectRatio))
  const frameRef = useRef(null)
  // Fullscreen takes the WHOLE player — picture, scrub bar and transport —
  // not just the frame. Fullscreening the frame alone left no way to pause,
  // seek or scrub (the controls are siblings, so they stayed behind on the
  // page), and it broke the captions: `box` is measured by useFitBox on the
  // container BELOW, which never resized, so the overlay kept its windowed
  // font size against a full-screen picture. With the root fullscreened that
  // container is inside it, the ResizeObserver fires, and everything scales.
  const rootRef = useRef(null)
  const [isFull, setIsFull] = useState(false)
  const [transportRef, narrow] = useNarrow(TRANSPORT_FULL_PX)
  const [moreAnchor, setMoreAnchor] = useState(null)
  useEffect(() => {
    const onChange = () => setIsFull(document.fullscreenElement === rootRef.current)
    document.addEventListener("fullscreenchange", onChange)
    return () => document.removeEventListener("fullscreenchange", onChange)
  }, [])
  const toggleFullscreen = () => {
    if (document.fullscreenElement) document.exitFullscreen?.()
    else rootRef.current?.requestFullscreen?.()
  }
  // Which scene is under the playhead — changes at scene boundaries only.
  const curIndex = usePlayhead(clock, (t) => sceneAt(scenes, t)?.index ?? -1)
  const cur = curIndex >= 0 ? scenes.find((s) => s.index === curIndex) || null : null
  const next = cur ? scenes[cur.index + 1] : null
  const tailGap = settings?.tail_gap_s ?? 0.15
  const filter = STYLE_FILTER[settings?.visual_style] || "none"

  const curRef = useRef(cur); curRef.current = cur
  const tickRef = useRef(onTick); tickRef.current = onTick
  const stopRef = useRef(onPlayingChange); stopRef.current = onPlayingChange
  const loopRef = useRef(loopScene); loopRef.current = loopScene

  // ── Clock ────────────────────────────────────────────────────
  // The clock's own position runs at frame rate in a local; the store hears
  // about it ~20×/s (PUSH_MS). The <video> plays natively, so the picture
  // stays smooth. A store value that is NOT one of our own pushes is an
  // external seek (the ruler, ←/→, a scene click), and moves the clock.
  useEffect(() => {
    if (!playing || preview) return undefined
    const PUSH_MS = 50
    let raf = 0
    let last = performance.now()
    let lastPush = 0
    let pos = clock.get()
    let pushed = pos
    const push = (t) => { pushed = t; clock.set(t); tickRef.current?.(t) }
    const step = (now) => {
      // rAF's timestamp is the frame START and can precede the performance.now()
      // taken above — never let the clock step backwards into the previous scene.
      const dt = Math.max(0, Math.min(0.1, (now - last) / 1000))
      last = now
      const ext = clock.get()
      if (ext !== pushed) { pos = ext; pushed = ext }
      let t = pos + dt
      const c = curRef.current
      if (loopRef.current && c && t >= c.start + c.dur) { t = c.start; lastPush = 0 }
      if (t >= total) { pos = total; push(total); stopRef.current?.(false); return }
      pos = t
      if (now - lastPush >= PUSH_MS) { lastPush = now; push(t) }
      raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => {
      cancelAnimationFrame(raf)
      // Land exactly where the clock stopped, not up to PUSH_MS behind it —
      // unless someone seeked meanwhile (their position wins).
      if (clock.get() === pushed && pos !== pushed) push(pos)
    }
  }, [playing, preview, total, clock])

  const voiced = !!cur && cur.voice?.state === "ready" && !!cur.voice?.take_key
  const takeKey = voiced ? cur.voice.take_key : null

  // Word timings for the caption overlay (D12) — the take's own, fetched once.
  const [words, setWords] = useState(null)
  useEffect(() => {
    let alive = true
    setWords(null)
    if (takeKey) takeMeta(takeKey).then((m) => { if (alive) setWords(m?.words || null) })
    return () => { alive = false }
  }, [takeKey])

  // ── Music bed ────────────────────────────────────────────────
  const music = settings?.music || {}
  const musicUrl = !music.enabled || !music.track ? null
    : `/api/music/stream/${encodeURIComponent(music.track)}`

  const musicTip = `Music: ${musicLabel(music)}`

  const iconSx = { color: "inherit", "&:hover": { bgcolor: "rgba(255,255,255,0.08)" } }

  return (
    <Box ref={rootRef} sx={(t) => ({
      flex: 1, minHeight: 0, display: "flex", flexDirection: "column", bgcolor: "#0d0e12", color: "#e9e9ec",
      "--vm-scrub": t.palette.primary.main,
    })}>
      <Box ref={boxRef} sx={{ flex: 1, minHeight: 0, m: 2, mb: 1, position: "relative" }}>
        <Box
          ref={frameRef}
          sx={{
            position: "absolute", left: "50%", top: "50%", transform: "translate(-50%, -50%)",
            width: box.w, height: box.h, overflow: "hidden", borderRadius: 1.5, bgcolor: "#000",
            boxShadow: "0 16px 48px rgba(0,0,0,0.5)",
          }}
          onClick={() => { if (!preview) onTogglePlay?.() }}
        >
          {preview ? (
            <PreviewLayer
              projectId={projectId} preview={preview} onClose={onClosePreview} onUse={onUsePreview}
              selectedIndex={selectedIndex} filter={filter}
            />
          ) : scenes.length === 0 ? null : (
            <>
              <FrameLayers
                clock={clock} projectId={projectId} cur={cur} next={next} playing={playing}
                filter={filter} captions={settings?.captions} box={box} tailGap={tailGap} words={words}
                hookText={hookTitle(settings, scenes)}
                hookAuto={hookIsAuto(settings, scenes)}
                shots={shots}
              />
              {cur && (
                <Stack direction="row" spacing={0.75} sx={{ position: "absolute", top: 8, left: 8 }}>
                  <Chip size="small" label={`Scene ${cur.index + 1} / ${scenes.length}`}
                    sx={{ height: 20, fontSize: 11, fontWeight: 700, bgcolor: "rgba(0,0,0,0.55)", color: "#fff" }} />
                  {!voiced && (
                    <Chip size="small" label={SILENT_LABEL[cur.voice?.state || "none"] || SILENT_LABEL.none}
                      sx={{ height: 20, fontSize: 11, bgcolor: "rgba(0,0,0,0.55)", color: "#ffd48a" }} />
                  )}
                </Stack>
              )}
            </>
          )}
        </Box>
      </Box>

      {/* Scrub bar */}
      <ScrubBar clock={clock} total={total} onSeek={onSeek} />

      {/* One row, never two. With the scene panel open the centre column
          narrows; a wrapping row of ten controls dropped Info + Fullscreen onto
          a second line (and, earlier, past the column's edge — harness
          2026-09-21). Narrow, the secondary toggles fold into a "More" menu;
          play, the scene steps, the clock and Fullscreen always stay. */}
      <Stack
        ref={transportRef}
        direction="row" alignItems="center" spacing={0.25}
        sx={{ px: 1.5, pb: 1, pt: 0.25, flexWrap: "nowrap", minWidth: 0, overflow: "hidden" }}
      >
        <Tooltip title="Previous scene (←)">
          <IconButton size="small" aria-label="Previous scene" onClick={onPrev} sx={iconSx}><SkipPreviousRoundedIcon /></IconButton>
        </Tooltip>
        <Tooltip title={playing ? "Pause (Space)" : playDisabled ? "The first scene is still being voiced" : "Play (Space)"}>
          <span>
            <IconButton onClick={onTogglePlay} disabled={playDisabled && !playing} sx={{ ...iconSx, "&.Mui-disabled": { color: "rgba(255,255,255,0.3)" } }} aria-label={playing ? "Pause" : "Play"}>
              {playing ? <PauseRoundedIcon /> : <PlayArrowRoundedIcon />}
            </IconButton>
          </span>
        </Tooltip>
        <Tooltip title="Next scene (→)">
          <IconButton size="small" aria-label="Next scene" onClick={onNext} sx={iconSx}><SkipNextRoundedIcon /></IconButton>
        </Tooltip>
        <ClockText clock={clock} total={total} />
        <Box sx={{ flex: 1, minWidth: 4 }} />
        {narrow ? (
          <>
            <Tooltip title="More player controls">
              <IconButton size="small" aria-label="More player controls" aria-haspopup="menu"
                aria-expanded={moreAnchor ? "true" : undefined}
                onClick={(e) => setMoreAnchor(e.currentTarget)} sx={iconSx}>
                <MoreHorizRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            {/* Portalled into the player root: in fullscreen only that root is
                on screen — a menu portalled to <body> would open invisibly
                behind it. (Not disablePortal: that nests the modal inside the
                app root, which the modal then aria-hides, menu and all.) */}
            <Menu anchorEl={moreAnchor} open={!!moreAnchor} onClose={() => setMoreAnchor(null)}
              container={() => rootRef.current}
              anchorOrigin={{ vertical: "top", horizontal: "right" }} transformOrigin={{ vertical: "bottom", horizontal: "right" }}>
              <MenuItem onClick={() => { onToggleVoice?.() }}>
                <ListItemIcon>{voiceMuted ? <VoiceOverOffOutlinedIcon fontSize="small" /> : <RecordVoiceOverOutlinedIcon fontSize="small" />}</ListItemIcon>
                <ListItemText>{voiceMuted ? "Unmute voice" : "Mute voice"}</ListItemText>
              </MenuItem>
              <MenuItem onClick={() => { onToggleMusic?.() }} disabled={!musicUrl}>
                <ListItemIcon>{musicMuted || !musicUrl ? <MusicOffOutlinedIcon fontSize="small" /> : <MusicNoteOutlinedIcon fontSize="small" />}</ListItemIcon>
                <ListItemText secondary={musicUrl ? null : musicTip}>{musicMuted ? "Unmute music" : "Mute music"}</ListItemText>
              </MenuItem>
              <MenuItem onClick={() => { onToggleLoop?.() }} selected={!!loopScene}>
                <ListItemIcon><RepeatOneRoundedIcon fontSize="small" /></ListItemIcon>
                <ListItemText>{loopScene ? "Stop looping this scene" : "Loop the current scene"}</ListItemText>
              </MenuItem>
              <MenuItem disabled sx={{ "&.Mui-disabled": { opacity: 1 }, whiteSpace: "normal", maxWidth: 280 }}>
                <ListItemIcon><InfoOutlinedIcon fontSize="small" /></ListItemIcon>
                <ListItemText secondary={PREVIEW_NOTE} slotProps={{ secondary: { sx: { whiteSpace: "normal" } } }}>About the preview</ListItemText>
              </MenuItem>
            </Menu>
          </>
        ) : (
          <>
            <Tooltip title={voiceMuted ? "Unmute voice" : "Mute voice"}>
              <IconButton size="small" aria-label={voiceMuted ? "Unmute voice" : "Mute voice"} onClick={onToggleVoice} sx={iconSx}>
                {voiceMuted ? <VoiceOverOffOutlinedIcon fontSize="small" /> : <RecordVoiceOverOutlinedIcon fontSize="small" />}
              </IconButton>
            </Tooltip>
            <Tooltip title={musicUrl ? (musicMuted ? "Unmute music" : "Mute music") : musicTip}>
              <span>
                <IconButton size="small" aria-label={musicMuted ? "Unmute music" : "Mute music"} onClick={onToggleMusic} disabled={!musicUrl} sx={{ ...iconSx, "&.Mui-disabled": { color: "rgba(255,255,255,0.25)" } }}>
                  {musicMuted || !musicUrl ? <MusicOffOutlinedIcon fontSize="small" /> : <MusicNoteOutlinedIcon fontSize="small" />}
                </IconButton>
              </span>
            </Tooltip>
            <Tooltip title={loopScene ? "Stop looping this scene" : "Loop the current scene"}>
              <IconButton size="small" aria-label={loopScene ? "Stop looping this scene" : "Loop the current scene"} onClick={onToggleLoop} sx={{ ...iconSx, color: loopScene ? "primary.light" : "inherit" }}>
                <RepeatOneRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <Tooltip title={PREVIEW_NOTE}>
              <IconButton size="small" aria-label="About the preview" sx={iconSx}><InfoOutlinedIcon fontSize="small" /></IconButton>
            </Tooltip>
          </>
        )}
        <Tooltip title={isFull ? "Exit fullscreen (Esc)" : "Fullscreen"}>
          <IconButton size="small" aria-label={isFull ? "Exit fullscreen" : "Fullscreen"}
            onClick={toggleFullscreen} sx={iconSx}>
            {isFull ? <FullscreenExitRoundedIcon fontSize="small" /> : <FullscreenRoundedIcon fontSize="small" />}
          </IconButton>
        </Tooltip>
      </Stack>
      <PlaybackAudio
        clock={clock} cur={cur} total={total} playing={playing} previewing={!!preview}
        voiceMuted={voiceMuted} musicMuted={musicMuted} takeKey={takeKey} voiced={voiced}
        music={music} musicUrl={musicUrl}
      />
    </Box>
  )
}
