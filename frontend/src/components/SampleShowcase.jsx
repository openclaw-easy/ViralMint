// SampleShowcase — a vivid, auto-playing strip of "start from a sample" cards
// that replaces the cold-form first impression on creation pages (PivotPlan
// §9/§11 — "show value, don't explain it").
//
// Each card layers, bottom → top:
//   1. a CSS gradient (instant, always paints),
//   2. a CACHED Pexels loop (real license-cleared footage) that fades in once
//      the backend has it — GET /api/showcase/clip?q=… builds + caches it once,
//      and 404s cleanly (no Pexels key / offline) so we just keep the gradient,
//   3. a transparent canvas drawing the word-by-word caption (+ subtle motion
//      when there's no footage) so it reads like an actual ViralMint output.
//
// Clicking a card fires onUse(sample) — the parent loads the sample's ready
// script + settings, so the user can Generate in one click. Collapsible +
// persisted (vm_showcase_open); when collapsed the cards unmount (no rAF / no
// video fetch).
import { useEffect, useRef, useState } from "react"
import { Box, Typography, IconButton, Tooltip } from "@mui/material"
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome"
import ChevronLeftIcon from "@mui/icons-material/ChevronLeft"
import BoltIcon from "@mui/icons-material/Bolt"
import http from "../api/http"
import { scrollOnHover } from "../utils/scrollbar"

const STORE_KEY = "vm_showcase_open"
const CARD_W = 82
const CARD_H = Math.round((CARD_W * 16) / 9) // 146 — 9:16
// Open-column width: 2 cards + gap + padding + scrollbar.
const COL_W = 200

// ── Caption overlay canvas (one per card) ──────────────────────────
// Draws the word-by-word caption (TikTok pop-in) over a transparent canvas.
// When `solid` (no real footage behind), it also paints the animated gradient
// + drifting light so the card still feels alive. When footage is playing it
// stays transparent so the video shows through — only the vignette + caption.
function CaptionCanvas({ cA, cB, text, solid, w = CARD_W }) {
  const ref = useRef(null)
  const h = Math.round((w * 16) / 9)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    canvas.width = w * dpr
    canvas.height = h * dpr
    const ctx = canvas.getContext("2d")
    ctx.scale(dpr, dpr)
    let raf = 0
    const t0 = performance.now()
    const words = (text || "").split(/\s+/).filter(Boolean)

    const draw = (now) => {
      const t = (now - t0) / 1000
      ctx.clearRect(0, 0, w, h)

      if (solid) {
        // Animated gradient backdrop + drifting highlights (no footage case).
        const zoom = 1.06 + Math.sin(t * 0.18) * 0.05
        ctx.save()
        ctx.translate(w * Math.sin(t * 0.12) * 0.06, h * Math.cos(t * 0.1) * 0.03)
        ctx.scale(zoom, zoom)
        const grad = ctx.createLinearGradient(0, 0, w, h)
        grad.addColorStop(0, cA)
        grad.addColorStop(1, cB)
        ctx.fillStyle = grad
        ctx.fillRect(-w * 0.15, -h * 0.15, w * 1.3, h * 1.3)
        for (let i = 0; i < 2; i++) {
          const ph = t * 0.35 + i * 2.4
          const cx = w * (0.3 + 0.45 * Math.sin(ph))
          const cy = h * (0.3 + 0.45 * Math.cos(ph * 0.8))
          const r = 30 + Math.sin(ph * 1.3) * 14
          const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r)
          g.addColorStop(0, "rgba(255,255,255,0.16)")
          g.addColorStop(1, "rgba(255,255,255,0)")
          ctx.fillStyle = g
          ctx.fillRect(cx - r, cy - r, r * 2, r * 2)
        }
        ctx.restore()
      }

      // Bottom vignette so the caption reads on any backdrop (gradient or video).
      const vig = ctx.createLinearGradient(0, h * 0.45, 0, h)
      vig.addColorStop(0, "rgba(0,0,0,0)")
      vig.addColorStop(1, "rgba(0,0,0,0.7)")
      ctx.fillStyle = vig
      ctx.fillRect(0, h * 0.45, w, h * 0.55)

      // Word-by-word caption (1–2 words), TikTok-style, cycling with a pop-in.
      if (words.length) {
        const cycle = words.length + 3
        const idx = Math.floor((t * 1.9) % cycle)
        const cur = words[Math.min(idx, words.length - 1)] || ""
        const nxt = words[Math.min(idx + 1, words.length - 1)]
        const phrase = idx < words.length ? (nxt && nxt !== cur ? `${cur} ${nxt}` : cur) : ""
        if (phrase) {
          ctx.font = "800 13px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
          ctx.textAlign = "center"
          ctx.textBaseline = "middle"
          const local = (t * 1.9) % 1
          const pop = 1 + Math.max(0, 0.25 - local) * 1.6
          ctx.save()
          ctx.translate(w / 2, h * 0.74)
          ctx.scale(pop, pop)
          ctx.lineWidth = 3
          ctx.strokeStyle = "rgba(0,0,0,0.85)"
          ctx.strokeText(phrase.toUpperCase(), 0, 0)
          ctx.fillStyle = "#FFE14D"
          ctx.fillText(phrase.toUpperCase(), 0, 0)
          ctx.restore()
        }
      }

      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [cA, cB, text, solid, w, h])

  return (
    <canvas
      ref={ref}
      style={{ width: w, height: h, display: "block", background: "transparent" }}
    />
  )
}

function SampleCard({ sample, onUse }) {
  const [clipUrl, setClipUrl] = useState(null)  // public Pexels CDN url (cloud-resolved)
  const [ready, setReady] = useState(false)     // first frame decoded → footage visible
  const [failed, setFailed] = useState(false)
  const [inView, setInView] = useState(false)
  const cardRef = useRef(null)
  const videoRef = useRef(null)
  const hasVideo = !!clipUrl && !failed
  const showVideo = hasVideo && ready         // footage now covers the gradient
  const q = sample.bgQuery || sample.niche

  // Ask the desktop (→ cloud, key stays server-side) for a real clip to show
  // behind the caption. No url → keep the procedural gradient.
  useEffect(() => {
    let cancelled = false
    http.get(`/api/showcase/clip?q=${encodeURIComponent(q)}`)
      .then(({ data }) => { if (!cancelled && data?.url) setClipUrl(data.url) })
      .catch(() => { /* procedural fallback */ })
    return () => { cancelled = true }
  }, [q])

  // Show the real footage at rest (not a flat color block), but AUTOPLAY only
  // while the card is on-screen so we never decode all 10 clips at once —
  // off-screen cards pause on a still frame. (Was hover-only, which hid the
  // content behind the gradient until you moused over each card.)
  useEffect(() => {
    const el = cardRef.current
    if (!el) return
    const io = new IntersectionObserver(([e]) => setInView(e.isIntersecting), { threshold: 0.35 })
    io.observe(el)
    return () => io.disconnect()
  }, [])
  useEffect(() => {
    const v = videoRef.current
    if (!v) return
    if (inView) v.play?.().catch(() => {})
    else v.pause?.()
  }, [inView, clipUrl])

  return (
    <Box
      ref={cardRef}
      role="button"
      tabIndex={0}
      onClick={() => onUse(sample)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onUse(sample) } }}
      sx={{
        flexShrink: 0,
        cursor: "pointer",
        width: CARD_W,
        outline: "none",
        transition: "transform 0.16s ease",
        "& .vm-thumb": {
          boxShadow: "0 6px 18px rgba(0,0,0,0.32), 0 0 0 1px rgba(255,255,255,0.06)",
          transition: "box-shadow 0.16s ease",
        },
        "& .vm-cta": { opacity: 0, transition: "opacity 0.16s ease" },
        "&:hover, &:focus-visible": { transform: "translateY(-3px) scale(1.03)" },
        "&:hover .vm-thumb, &:focus-visible .vm-thumb": {
          boxShadow: "0 12px 30px rgba(0,0,0,0.45), 0 0 0 1px rgba(255,255,255,0.14)",
        },
        "&:hover .vm-cta, &:focus-visible .vm-cta": { opacity: 1 },
      }}
    >
      {/* Thumb (9:16) — layered gradient / footage / caption */}
      <Box
        className="vm-thumb"
        sx={{ position: "relative", width: CARD_W, height: CARD_H, borderRadius: "10px", overflow: "hidden", bgcolor: "#000" }}
      >
        {/* 1. instant gradient base */}
        <Box sx={{ position: "absolute", inset: 0, background: `linear-gradient(160deg, ${sample.cA}, ${sample.cB})` }} />

        {/* 2. real footage (cloud-resolved Pexels CDN) — HOVER-play only,
            same pattern as the AI Video Studio's TemplateGallery: the video
            mounts on mouse-enter and unmounts on leave, so a rail of samples
            never plays (or downloads) N clips at once. */}
        {hasVideo && (
          <video
            ref={videoRef}
            src={clipUrl}
            muted
            loop
            playsInline
            preload="metadata"
            onLoadedData={() => setReady(true)}
            onError={() => setFailed(true)}
            style={{
              position: "absolute", inset: 0, width: "100%", height: "100%",
              objectFit: "cover", opacity: showVideo ? 1 : 0, transition: "opacity 0.5s ease",
            }}
          />
        )}

        {/* 3. caption overlay (transparent over footage; animated gradient when none) */}
        <Box sx={{ position: "absolute", inset: 0 }}>
          <CaptionCanvas cA={sample.cA} cB={sample.cB} text={sample.hook} solid={!showVideo} />
        </Box>

        {/* hover CTA */}
        <Box
          className="vm-cta"
          sx={{
            position: "absolute", inset: 0,
            display: "flex", alignItems: "center", justifyContent: "center",
            background: "rgba(0,0,0,0.45)", backdropFilter: "blur(1px)",
            pointerEvents: "none",
          }}
        >
          <Box sx={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 0.5, color: "#fff", textAlign: "center", px: 0.5 }}>
            <BoltIcon sx={{ fontSize: 20, color: "#FFE14D" }} />
            <Typography sx={{ fontSize: "0.6rem", fontWeight: 800, lineHeight: 1.1 }}>
              Make one<br />like this
            </Typography>
          </Box>
        </Box>
      </Box>

      {/* label under the thumb */}
      <Box sx={{ pt: 0.5 }}>
        <Typography noWrap sx={{ fontSize: "0.66rem", fontWeight: 700, lineHeight: 1.2 }}>
          {sample.label}
        </Typography>
        <Typography noWrap sx={{ fontSize: "0.58rem", color: "text.secondary", lineHeight: 1.2 }}>
          {sample.niche}
        </Typography>
      </Box>
    </Box>
  )
}

/**
 * @param {object[]} samples  array of sample defs (see data/sampleShowcase.js)
 * @param {(sample) => void} onUse  called when a card is clicked
 * @param {string} [title]
 */
export default function SampleShowcase({ samples = [], onUse, title = "Start from a sample", open: openProp, onToggle }) {
  // Controlled when the parent passes `open` (it reserves preview-gutter space
  // for the panel); otherwise self-manage + persist.
  const [openState, setOpenState] = useState(() => {
    try { return localStorage.getItem(STORE_KEY) !== "false" } catch { return true }
  })
  const controlled = openProp !== undefined
  const open = controlled ? openProp : openState

  const toggle = () => {
    if (controlled) { onToggle?.(); return }
    setOpenState((v) => {
      const next = !v
      try { localStorage.setItem(STORE_KEY, String(next)) } catch { /* ignore */ }
      return next
    })
  }

  if (!samples.length) return null

  // Collapsed → a slim in-flow rail with a rotated "Start from a sample"
  // label, so it never overlaps and the center reclaims the width.
  if (!open) {
    return (
      <Box
        sx={{
          width: 40, flexShrink: 0, borderRight: 1, borderColor: "divider",
          display: "flex", flexDirection: "column", alignItems: "center",
          pt: 1.25, gap: 0.5, cursor: "pointer",
          "&:hover": { bgcolor: "action.hover" },
        }}
        onClick={toggle}
      >
        <Tooltip title="Show sample videos" placement="right">
          <IconButton size="small" aria-label="Show samples" sx={{ p: 0.5 }}>
            <AutoAwesomeIcon sx={{ fontSize: 18, color: "#C44CE3" }} />
          </IconButton>
        </Tooltip>
        <Typography
          sx={{
            writingMode: "vertical-rl", transform: "rotate(180deg)",
            fontSize: "0.68rem", fontWeight: 800, color: "text.secondary",
            letterSpacing: "0.03em", mt: 0.5, userSelect: "none",
          }}
        >
          {title}
        </Typography>
      </Box>
    )
  }

  // Open → an in-flow LEFTMOST column (its own flex child, no overlap): a
  // 2-per-row, vertically-scrollable grid of cards. The samples get the room
  // (10 cards) while the Visual Style rail stays slim beside it.
  return (
    <Box
      sx={{
        width: COL_W, flexShrink: 0, height: "100%",
        borderRight: 1, borderColor: "divider",
        display: "flex", flexDirection: "column",
        px: 1, pt: 0.75, pb: 0.5,
      }}
    >
      {/* Header */}
      <Box sx={{ display: "flex", alignItems: "center", gap: 0.5, mb: 0.75, flexShrink: 0 }}>
        <AutoAwesomeIcon sx={{ fontSize: 15, color: "#C44CE3" }} />
        <Typography noWrap sx={{ fontSize: "0.72rem", fontWeight: 800, lineHeight: 1.1, flex: 1, minWidth: 0 }}>{title}</Typography>
        <Tooltip title="Collapse" placement="right">
          <IconButton size="small" onClick={toggle} aria-label="Collapse samples" sx={{ p: 0.25 }}>
            <ChevronLeftIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </Box>

      {/* 2-per-row, vertically-scrollable grid */}
      <Box
        sx={{
          display: "grid", gridTemplateColumns: `repeat(2, ${CARD_W}px)`, gap: 1.25,
          overflowY: "auto", flex: 1, pr: 0.5, alignContent: "start",
          ...scrollOnHover,
        }}
      >
        {samples.map((s) => (
          <SampleCard key={s.id} sample={s} onUse={onUse} />
        ))}
      </Box>
    </Box>
  )
}
