import { useRef, useEffect } from "react"
import { Box, Typography, Paper, Tooltip } from "@mui/material"
import { glassSelectedGlow } from "../../utils/glassFx"

// ── Visual style palettes — id maps directly to the backend's
//    `visual_style` param (cinematic/vlog/card/educational/documentary).
export const STYLES = [
  { id: "cinematic",   label: "Cinematic",   tag: "Movie-trailer feel",   cA: "#1B5E5F", cB: "#C2552E" },
  { id: "vlog",        label: "Vlog",        tag: "Personal · handheld",  cA: "#C58A4A", cB: "#5C3E20" },
  { id: "card",        label: "Quote / Card",tag: "Text moments · moody", cA: "#1E1B2E", cB: "#0A0810" },
  { id: "educational", label: "Educational", tag: "Clean · diagram",      cA: "#2B4566", cB: "#B7D2E8" },
  { id: "documentary", label: "Documentary", tag: "Factual · grain",      cA: "#4A5568", cB: "#1A202C" },
  { id: "noir",        label: "Noir",        tag: "B&W · high contrast",  cA: "#2A2A2E", cB: "#0B0B0D" },
  { id: "neon",        label: "Neon",        tag: "Cyberpunk · vivid",    cA: "#12063A", cB: "#3A0F58" },
  { id: "vintage",     label: "Vintage",     tag: "Retro · faded film",   cA: "#C9A46B", cB: "#6B4E32" },
]

// ── Procedural live preview canvas ─────────────────────────────────
export function PreviewCanvas({ paletteA, paletteB, captionText, aspectRatio }) {
  const canvasRef = useRef(null)
  const isVertical = aspectRatio === "9:16"
  const isSquare = aspectRatio === "1:1"
  const W = isSquare ? 460 : isVertical ? 320 : 568
  const H = isSquare ? 460 : isVertical ? 568 : 320

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext("2d")
    let raf = 0
    const t0 = performance.now()
    const words = (captionText || "Your script will animate here").split(/\s+/).filter(Boolean)

    const draw = (now) => {
      const t = (now - t0) / 1000

      const pan = Math.sin(t * 0.15) * 0.10
      const zoom = 1.04 + Math.sin(t * 0.10) * 0.04
      ctx.save()
      ctx.translate(W * pan, H * pan * 0.4)
      ctx.scale(zoom, zoom)

      const grad = ctx.createLinearGradient(0, 0, W, H)
      grad.addColorStop(0, paletteA)
      grad.addColorStop(1, paletteB)
      ctx.fillStyle = grad
      ctx.fillRect(-W * 0.1, -H * 0.1, W * 1.2, H * 1.2)

      for (let i = 0; i < 3; i++) {
        const phase = t * 0.3 + i * 2.1
        const cx = W * (0.3 + 0.4 * Math.sin(phase))
        const cy = H * (0.3 + 0.4 * Math.cos(phase * 0.8))
        const r = 80 + Math.sin(phase * 1.4) * 30
        const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r)
        g.addColorStop(0, "rgba(255,255,255,0.12)")
        g.addColorStop(1, "rgba(255,255,255,0)")
        ctx.fillStyle = g
        ctx.fillRect(cx - r, cy - r, r * 2, r * 2)
      }
      ctx.restore()

      const topVig = ctx.createLinearGradient(0, 0, 0, H * 0.35)
      topVig.addColorStop(0, "rgba(0,0,0,0.45)")
      topVig.addColorStop(1, "rgba(0,0,0,0)")
      ctx.fillStyle = topVig
      ctx.fillRect(0, 0, W, H * 0.35)
      const botVig = ctx.createLinearGradient(0, H * 0.55, 0, H)
      botVig.addColorStop(0, "rgba(0,0,0,0)")
      botVig.addColorStop(1, "rgba(0,0,0,0.65)")
      ctx.fillStyle = botVig
      ctx.fillRect(0, H * 0.55, W, H * 0.45)

      const wordsShown = Math.min(words.length, Math.floor((t * 2.3) % (words.length + 4)))
      const visible = words.slice(0, wordsShown)
      ctx.font = "700 22px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
      ctx.textBaseline = "top"
      ctx.textAlign = "left"

      const maxChars = isVertical ? 26 : 48
      const lines = []
      let cur = ""
      for (const w of visible) {
        if ((cur + " " + w).trim().length > maxChars) {
          if (cur) lines.push(cur)
          cur = w
        } else {
          cur = (cur + " " + w).trim()
        }
      }
      if (cur) lines.push(cur)
      const showLines = lines.slice(-3)

      const lineH = 30
      const blockH = showLines.length * lineH
      const baseY = H * 0.78 - blockH / 2

      const lastWord = visible[visible.length - 1] || ""
      showLines.forEach((line, i) => {
        const y = baseY + i * lineH
        const isLastLine = i === showLines.length - 1
        const w = ctx.measureText(line).width
        const x = (W - w) / 2

        ctx.fillStyle = "rgba(0,0,0,0.8)"
        ctx.fillText(line, x + 2, y + 2)

        if (isLastLine && lastWord && line.endsWith(lastWord)) {
          ctx.fillStyle = "#FFFFFF"
          ctx.fillText(line, x, y)
          const lastW = ctx.measureText(lastWord).width
          ctx.fillStyle = "#FFD93D"
          ctx.fillText(lastWord, x + w - lastW, y)
        } else {
          ctx.fillStyle = "#FFFFFF"
          ctx.fillText(line, x, y)
        }
      })

      ctx.fillStyle = "#FF4747"
      ctx.beginPath(); ctx.arc(20, 20, 5, 0, Math.PI * 2); ctx.fill()
      ctx.fillStyle = "#FFFFFF"
      ctx.font = "700 11px -apple-system, sans-serif"
      ctx.fillText("LIVE PREVIEW", 32, 13)

      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [paletteA, paletteB, captionText, isVertical, W, H])

  // Fit-contain the canvas directly (no shrink-wrap wrapper). An inline-block
  // wrapper sizes to the canvas's intrinsic W×H, so maxHeight:100% on the
  // canvas resolves against that intrinsic height and never shrinks — the 9:16
  // frame (320×568) then overflows its flex cell and clips. Mirroring the
  // <video> replay branch below: the canvas IS the replaced element, capped by
  // max-width/height:100% with width/height:auto so it scales to its cell while
  // preserving aspect ratio.
  return (
    <canvas
      ref={canvasRef}
      width={W}
      height={H}
      style={{
        display: "block",
        // Contain within the cell (maxHeight:100% fixes the 9:16 overflow), but
        // also cap to a fixed envelope so a wide cell doesn't blow the 16:9
        // frame up to its full 568px intrinsic ("too wide"). 9:16 is height-led
        // (≤460px tall → ~259w), 16:9 is width-led (≤460px wide → ~259h): both
        // read as the same tasteful centered card regardless of cell size.
        maxWidth: "min(100%, 460px)",
        maxHeight: "min(100%, 460px)",
        width: "auto",
        height: "auto",
        objectFit: "contain",
        borderRadius: 12,
        backgroundColor: "#000",
        boxShadow: "0 24px 64px rgba(0,0,0,0.45), 0 0 0 1px rgba(255,255,255,0.08), 0 0 80px rgba(99,179,237,0.15)",
      }}
    />
  )
}

/**
 * Per-style procedural preview — Netflix-genre-tile pattern. Every swatch
 * shows the SAME recognizable scene (sky · sun · horizon · ground · subject
 * silhouette) so the user can read the swatches like-for-like. Each style
 * then applies its own grade + treatment + overlay, mirroring what the
 * underlying ffmpeg/LUT pipeline actually does on real footage.
 *
 * The shared scene means: instead of comparing five abstract gradients, the
 * user is comparing "the same shot in five different visual treatments" —
 * which is exactly what the visual_style parameter does at render time.
 */
function StyleSwatchPreview({ style, w = 178, h = 100 }) {
  const canvasRef = useRef(null)
  const W = w, H = h

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext("2d")
    let raf = 0
    const t0 = performance.now()

    // ── Style-specific tone presets ────────────────────
    // Sky / ground / sun colors are tuned to each style so the same scene
    // reads as a sunset-cinema frame OR a sunny-vlog frame OR a moody-card
    // frame. All driven by the style.cA / style.cB base palette plus a
    // per-style modifier.
    const tones = {
      cinematic:   { skyTop: "#0E2A4B", skyBot: "#C2552E", ground: "#1A0E10", sun: "#FFB060", sunY: 0.55, sunR: 16, sunAlpha: 0.95 },
      vlog:        { skyTop: "#FCD897", skyBot: "#FFE8B9", ground: "#7B5530", sun: "#FFE07A", sunY: 0.30, sunR: 14, sunAlpha: 0.85 },
      card:        { skyTop: "#1E1B2E", skyBot: "#0A0810", ground: "#050308", sun: "#7A6FA0", sunY: 0.40, sunR:  9, sunAlpha: 0.55 },
      educational: { skyTop: "#A8C8F0", skyBot: "#E8F1FB", ground: "#D7DCE6", sun: "#FFFFFF", sunY: 0.32, sunR: 13, sunAlpha: 0.95 },
      documentary: { skyTop: "#5C6A7D", skyBot: "#9BA6B5", ground: "#4B4640", sun: "#D8D2C2", sunY: 0.42, sunR: 11, sunAlpha: 0.65 },
      noir:        { skyTop: "#3A3A40", skyBot: "#1B1B1F", ground: "#0A0A0C", sun: "#E8E8EC", sunY: 0.38, sunR: 12, sunAlpha: 0.50 },
      neon:        { skyTop: "#12063A", skyBot: "#3A0F58", ground: "#0A0518", sun: "#FF5BD8", sunY: 0.45, sunR: 14, sunAlpha: 0.90 },
      vintage:     { skyTop: "#E8C48A", skyBot: "#D9A05E", ground: "#5E4326", sun: "#FFE9B0", sunY: 0.35, sunR: 15, sunAlpha: 0.80 },
    }
    const tone = tones[style.id] || tones.cinematic

    const drawScene = (t, panX = 0, panY = 0, zoom = 1) => {
      ctx.save()
      // Pan/zoom mimics the Ken-Burns motion the pipeline actually applies.
      ctx.translate(W * panX, H * panY)
      ctx.scale(zoom, zoom)

      // Sky gradient (top half of frame)
      const sky = ctx.createLinearGradient(0, 0, 0, H * 0.7)
      sky.addColorStop(0, tone.skyTop)
      sky.addColorStop(1, tone.skyBot)
      ctx.fillStyle = sky
      ctx.fillRect(-10, -10, W + 20, H * 0.7)

      // Ground gradient (bottom)
      const ground = ctx.createLinearGradient(0, H * 0.7, 0, H + 10)
      ground.addColorStop(0, tone.ground)
      ground.addColorStop(1, "rgba(0,0,0,0.7)")
      ctx.fillStyle = ground
      ctx.fillRect(-10, H * 0.65, W + 20, H * 0.5)

      // Sun / moon disc — soft radial glow halo
      const sunX = W * 0.62
      const sunY = H * tone.sunY
      const g = ctx.createRadialGradient(sunX, sunY, 0, sunX, sunY, tone.sunR * 3)
      g.addColorStop(0, tone.sun)
      g.addColorStop(1, "rgba(0,0,0,0)")
      ctx.fillStyle = g
      ctx.fillRect(sunX - tone.sunR * 3, sunY - tone.sunR * 3, tone.sunR * 6, tone.sunR * 6)
      // Crisp sun core
      ctx.fillStyle = tone.sun
      ctx.globalAlpha = tone.sunAlpha
      ctx.beginPath()
      ctx.arc(sunX, sunY, tone.sunR, 0, Math.PI * 2)
      ctx.fill()
      ctx.globalAlpha = 1

      // Mountain ridge silhouette — horizon detail so the scene reads as
      // a real landscape and not just a gradient.
      ctx.fillStyle = "rgba(0,0,0,0.55)"
      ctx.beginPath()
      ctx.moveTo(-10, H * 0.7)
      ctx.lineTo(W * 0.18, H * 0.55)
      ctx.lineTo(W * 0.32, H * 0.62)
      ctx.lineTo(W * 0.48, H * 0.50)
      ctx.lineTo(W * 0.66, H * 0.60)
      ctx.lineTo(W * 0.82, H * 0.53)
      ctx.lineTo(W + 10, H * 0.62)
      ctx.lineTo(W + 10, H * 0.72)
      ctx.lineTo(-10, H * 0.72)
      ctx.closePath()
      ctx.fill()

      // Subject silhouette — a person standing left-of-center on the ridge.
      // Universal "this is a video about a person/topic" signal.
      ctx.fillStyle = "rgba(0,0,0,0.92)"
      const px = W * 0.30, py = H * 0.66
      ctx.beginPath()
      ctx.arc(px, py - 9, 3.2, 0, Math.PI * 2)         // head
      ctx.fill()
      ctx.fillRect(px - 2.2, py - 6, 4.4, 9)            // torso
      ctx.fillRect(px - 2.4, py + 3, 1.6, 6)            // legs
      ctx.fillRect(px + 0.6, py + 3, 1.6, 6)

      ctx.restore()
    }

    const draw = (now) => {
      const t = (now - t0) / 1000
      ctx.clearRect(0, 0, W, H)

      if (style.id === "cinematic") {
        // Slow Ken-Burns zoom + diagonal lens-flare sweep
        const pan = Math.sin(t * 0.35) * 0.04
        drawScene(t, pan, 0, 1.08 + Math.sin(t * 0.2) * 0.03)
        const flareX = ((t * 0.20) % 1.4 - 0.2) * W
        const flareW = W * 0.45
        const g = ctx.createLinearGradient(flareX, 0, flareX + flareW, H)
        g.addColorStop(0, "rgba(255,255,255,0)")
        g.addColorStop(0.5, "rgba(255,210,160,0.32)")
        g.addColorStop(1, "rgba(255,255,255,0)")
        ctx.fillStyle = g
        ctx.fillRect(flareX, 0, flareW, H)
      } else if (style.id === "vlog") {
        // Handheld jitter + warm sun bokeh
        const jx = (Math.sin(t * 6.2) + Math.sin(t * 9.7)) * 0.006
        const jy = (Math.cos(t * 5.8) + Math.sin(t * 7.3)) * 0.008
        drawScene(t, jx, jy, 1.04)
        // Soft bokeh circle drifting
        for (let i = 0; i < 2; i++) {
          const phase = t * 0.4 + i * 2.1
          const cx = W * (0.20 + 0.5 * (Math.sin(phase) * 0.5 + 0.5))
          const cy = H * 0.25 + i * 18
          const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, 18)
          g.addColorStop(0, "rgba(255,230,170,0.45)")
          g.addColorStop(1, "rgba(255,200,120,0)")
          ctx.fillStyle = g
          ctx.fillRect(cx - 18, cy - 18, 36, 36)
        }
      } else if (style.id === "card") {
        // Static composition + pulsing quotation marks overlaying the scene
        drawScene(t, 0, 0, 1.02)
        // Dark vignette to push the moody feel
        const vg = ctx.createRadialGradient(W / 2, H / 2, H * 0.2, W / 2, H / 2, H * 0.8)
        vg.addColorStop(0, "rgba(0,0,0,0)")
        vg.addColorStop(1, "rgba(0,0,0,0.55)")
        ctx.fillStyle = vg
        ctx.fillRect(0, 0, W, H)
        const a = 0.55 + Math.sin(t * 1.8) * 0.20
        ctx.font = "700 40px Georgia, serif"
        ctx.fillStyle = `rgba(255,255,255,${a})`
        ctx.textAlign = "center"
        ctx.fillText("“", W * 0.5, H * 0.55)
        ctx.font = "600 9px -apple-system, sans-serif"
        ctx.fillStyle = "rgba(255,255,255,0.75)"
        ctx.fillText("text-card moment", W / 2, H * 0.80)
        ctx.textAlign = "left"
      } else if (style.id === "educational") {
        // Clean, bright scene + a small concept arrow + label
        drawScene(t, Math.sin(t * 0.25) * 0.015, 0, 1.03)
        // Concept-arrow overlay — drawn over the subject like a textbook diagram
        ctx.strokeStyle = "rgba(255,255,255,0.92)"
        ctx.fillStyle = "rgba(255,255,255,0.92)"
        ctx.lineWidth = 1.5
        ctx.beginPath()
        ctx.moveTo(W * 0.52, H * 0.30)
        ctx.lineTo(W * 0.36, H * 0.58)
        ctx.stroke()
        // Arrowhead
        ctx.beginPath()
        ctx.moveTo(W * 0.36, H * 0.58)
        ctx.lineTo(W * 0.42, H * 0.54)
        ctx.lineTo(W * 0.40, H * 0.61)
        ctx.closePath()
        ctx.fill()
        // Label
        ctx.font = "700 8px -apple-system, sans-serif"
        ctx.fillStyle = "rgba(255,255,255,0.95)"
        ctx.fillText("Subject", W * 0.54, H * 0.30)
      } else if (style.id === "documentary") {
        // Neutral pan + film grain overlay (the killer documentary tell)
        drawScene(t, Math.sin(t * 0.18) * 0.04, 0, 1.04)
        const imgData = ctx.getImageData(0, 0, W, H)
        const d = imgData.data
        // Grain at every 2nd pixel — perf-conscious
        for (let i = 0; i < d.length; i += 8) {
          const n = (Math.random() - 0.5) * 24
          d[i]     = Math.max(0, Math.min(255, d[i]     + n))
          d[i + 1] = Math.max(0, Math.min(255, d[i + 1] + n))
          d[i + 2] = Math.max(0, Math.min(255, d[i + 2] + n))
        }
        ctx.putImageData(imgData, 0, 0)
      } else if (style.id === "noir") {
        // Slow push-in, full desaturate, venetian light bars + hard vignette
        drawScene(t, 0, 0, 1.05 + Math.sin(t * 0.15) * 0.02)
        ctx.save()
        ctx.globalCompositeOperation = "saturation"
        ctx.fillStyle = "#808080"           // kills all chroma (the B&W tell)
        ctx.fillRect(0, 0, W, H)
        ctx.restore()
        // Diagonal blind-light bars sweeping slowly
        ctx.save()
        ctx.translate(W * 0.5, H * 0.5)
        ctx.rotate(-0.5)
        const off = (t * 6) % 26
        ctx.fillStyle = "rgba(255,255,255,0.07)"
        for (let x = -W; x < W; x += 26) ctx.fillRect(x + off, -H, 10, H * 2)
        ctx.restore()
        const nvg = ctx.createRadialGradient(W / 2, H / 2, H * 0.25, W / 2, H / 2, H * 0.75)
        nvg.addColorStop(0, "rgba(0,0,0,0)")
        nvg.addColorStop(1, "rgba(0,0,0,0.65)")
        ctx.fillStyle = nvg
        ctx.fillRect(0, 0, W, H)
      } else if (style.id === "neon") {
        // Vivid dusk + glowing horizon line + a cyan scanline drifting up
        drawScene(t, Math.sin(t * 0.3) * 0.02, 0, 1.06)
        const hy = H * 0.65
        const hg = ctx.createLinearGradient(0, hy - 7, 0, hy + 7)
        hg.addColorStop(0, "rgba(255,91,216,0)")
        hg.addColorStop(0.5, `rgba(255,91,216,${0.75 + Math.sin(t * 2.4) * 0.2})`)
        hg.addColorStop(1, "rgba(255,91,216,0)")
        ctx.fillStyle = hg
        ctx.fillRect(0, hy - 7, W, 14)
        const sy = H - ((t * 14) % (H + 20))
        const sg = ctx.createLinearGradient(0, sy - 5, 0, sy + 5)
        sg.addColorStop(0, "rgba(80,240,255,0)")
        sg.addColorStop(0.5, "rgba(80,240,255,0.22)")
        sg.addColorStop(1, "rgba(80,240,255,0)")
        ctx.fillStyle = sg
        ctx.fillRect(0, sy - 5, W, 10)
      } else if (style.id === "vintage") {
        // Super-8 feel: warm wash, projector flicker, dust specks, faded corners
        const flicker = 1 + Math.sin(t * 11) * 0.02 + Math.sin(t * 3.3) * 0.01
        ctx.save()
        ctx.globalAlpha = Math.min(1, flicker)
        drawScene(t, Math.sin(t * 0.22) * 0.02, 0, 1.03)
        ctx.restore()
        ctx.fillStyle = "rgba(224,178,110,0.18)"   // warm sepia wash
        ctx.fillRect(0, 0, W, H)
        // Dust specks (deterministic wander so they don't strobe)
        ctx.fillStyle = "rgba(255,250,235,0.55)"
        for (let i = 0; i < 3; i++) {
          const px = W * ((Math.sin(t * 1.7 + i * 2.4) + 1) / 2)
          const py = H * ((Math.cos(t * 2.3 + i * 1.7) + 1) / 2)
          ctx.fillRect(px, py, 1.5, 1.5)
        }
        const fvg = ctx.createRadialGradient(W / 2, H / 2, H * 0.3, W / 2, H / 2, H * 0.85)
        fvg.addColorStop(0, "rgba(0,0,0,0)")
        fvg.addColorStop(1, "rgba(60,40,20,0.45)")
        ctx.fillStyle = fvg
        ctx.fillRect(0, 0, W, H)
      } else {
        drawScene(t)
      }

      // Subtle top sheen so every swatch reads as "media", not paint
      const sheen = ctx.createLinearGradient(0, 0, 0, H * 0.4)
      sheen.addColorStop(0, "rgba(255,255,255,0.10)")
      sheen.addColorStop(1, "rgba(255,255,255,0)")
      ctx.fillStyle = sheen
      ctx.fillRect(0, 0, W, H * 0.4)

      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [style])

  return <canvas ref={canvasRef} width={W} height={H} style={{ display: "block", width: "100%", height: H }} />
}

// `mini` = a small horizontal-row swatch (used in the Config column's Visual
// Style panel); `compact` = the older slim-rail size; default = large.
export function StyleSwatch({ style, selected, onClick, compact, mini }) {
  const small = compact || mini
  const pw = mini ? 56 : compact ? 88 : 178
  const ph = mini ? 32 : compact ? 50 : 100
  const swatch = (
    <Paper
      elevation={0}
      onClick={onClick}
      sx={(theme) => ({
        cursor: "pointer",
        borderRadius: 2,
        border: 1,
        borderColor: selected ? "primary.main" : "divider",
        overflow: "hidden",
        transition: "transform .18s ease, box-shadow .18s ease",
        position: "relative",
        flexShrink: 0,
        ...(selected && glassSelectedGlow),
        "&:hover": {
          transform: "translateY(-2px)",
          boxShadow: theme.palette.mode === "dark"
            ? "0 6px 20px rgba(0,0,0,0.4), 0 0 24px rgba(99,179,237,0.18)"
            : "0 6px 20px rgba(99,99,160,0.15)",
        },
      })}
    >
      <StyleSwatchPreview style={style} w={pw} h={ph} />
      <Box sx={{ px: small ? 0.5 : 1, pt: small ? 0.35 : 0.75, pb: small ? 0.45 : 1 }}>
        <Typography
          noWrap={!mini}
          sx={{
            fontWeight: 700, lineHeight: 1.15,
            fontSize: mini ? "0.56rem" : compact ? "0.7rem" : "0.8rem",
            textAlign: mini ? "center" : "left",
          }}
        >
          {style.label}
        </Typography>
        {!small && (
          <Typography
            variant="caption"
            sx={{ color: "text.secondary", fontSize: "0.68rem", lineHeight: 1.3, display: "block", mt: 0.25 }}
          >
            {style.tag}
          </Typography>
        )}
      </Box>
    </Paper>
  )
  // Small variants drop the tag line — surface it on hover instead.
  return small ? <Tooltip title={style.tag} placement={mini ? "top" : "right"} arrow>{swatch}</Tooltip> : swatch
}

