import { useState, useEffect, useRef } from "react"
import { useNavigate } from "react-router-dom"
import {
  Box, Typography, Button, Stack, TextField, Tooltip, Paper, Chip, Fade,
} from "@mui/material"
import PhotoLibraryIcon from "@mui/icons-material/PhotoLibrary"
import MovieCreationIcon from "@mui/icons-material/MovieCreation"
import FolderOpenIcon from "@mui/icons-material/FolderOpen"
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome"
import AutoFixHighIcon from "@mui/icons-material/AutoFixHigh"
import DragHandleIcon from "@mui/icons-material/DragHandle"
import useAppStore from "../store/appStore"
import useSettings from "../hooks/useSettings"
import useRemoteConfig from "../hooks/useRemoteConfig"
import useScriptGeneration from "../hooks/useScriptGeneration"
import useSourceVideo from "../hooks/useSourceVideo"
import http from "../api/http"
import { glassPanelSx } from "../utils/glassFx"

import PageHero from "../components/PageHero"
import SampleShowcase from "../components/SampleShowcase"
import { STYLES, PreviewCanvas } from "../components/create/SmartVideoStyles"
import { CompactSourcePanel } from "../components/create/SmartVideoPanels"
import StudioConfigRail from "../components/create/StudioConfigRail"
import EstimatedCost from "../components/create/EstimatedCost"
import ActiveJobsBanner from "../components/create/ActiveJobsBanner"
import TemplateGallery from "../components/create/TemplateGallery"
import SMART_VIDEO_SAMPLES from "../data/sampleShowcase"

const STYLE_SESSION_KEY = "vm_stock_visual_style"
const SHOWCASE_OPEN_KEY = "vm_stock_showcase_open"

export default function StockVideo() {
  const navigate = useNavigate()
  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const { settings } = useSettings()

  const { data: TTS_PROVIDERS } = useRemoteConfig("tts_providers")
  const { data: CAPTION_STYLES } = useRemoteConfig("caption_styles")
  const { data: MUSIC_GENRES } = useRemoteConfig("music_genres")

  const { source, sourceId } = useSourceVideo()
  const {
    script, setScript, scriptInstructions, setScriptInstructions,
    scriptLoading, handleGenerateScript, handlePolishScript,
  } = useScriptGeneration()

  // ── Config state ────────────────────────────────────────────────
  const [aspectRatio, setAspectRatio] = useState("9:16")
  const [operation, setOperation] = useState("t2v")
  const [startImage, setStartImage] = useState(null)
  const [ttsProvider, setTtsProvider] = useState("edge_tts")
  const [captionEnabled, setCaptionEnabled] = useState(true)
  const [captionStyle, setCaptionStyle] = useState("viral")
  const [musicEnabled, setMusicEnabled] = useState(true)
  const [musicGenre, setMusicGenre] = useState("lofi")
  const [visualStyle, setVisualStyle] = useState(() => sessionStorage.getItem(STYLE_SESSION_KEY) || "cinematic")
  const [transitionStyle, setTransitionStyle] = useState("auto")
  const [generating, setGenerating] = useState(false)

  // Center preview: null → procedural PreviewCanvas; a URL → play that video.
  const [previewVideoUrl, setPreviewVideoUrl] = useState(null)

  // Resizable script panel (drag its top edge).
  const [scriptPanelHeight, setScriptPanelHeight] = useState(340)
  const scriptHeightRef = useRef(340)

  // Left showcase column open/collapsed (persisted).
  const [showcaseOpen, setShowcaseOpen] = useState(() => sessionStorage.getItem(SHOWCASE_OPEN_KEY) !== "0")

  useEffect(() => { sessionStorage.setItem(STYLE_SESSION_KEY, visualStyle) }, [visualStyle])
  useEffect(() => { sessionStorage.setItem(SHOWCASE_OPEN_KEY, showcaseOpen ? "1" : "0") }, [showcaseOpen])

  useEffect(() => {
    if (settings) {
      setTtsProvider(settings.tts_provider || "edge_tts")
      setCaptionEnabled(settings.caption_enabled !== false)
      setCaptionStyle(settings.caption_style || "viral")
      setMusicEnabled(settings.music_enabled !== false)
      setMusicGenre(settings.music_genre || "lofi")
    }
  }, [settings])

  const palette = STYLES.find((s) => s.id === visualStyle) || STYLES[0]

  const onScriptResizeStart = (e) => {
    e.preventDefault()
    const startY = e.clientY
    const startH = scriptHeightRef.current
    const onMove = (ev) => {
      const dy = startY - ev.clientY // drag up → taller script
      const h = Math.max(180, Math.min(560, startH + dy))
      scriptHeightRef.current = h
      setScriptPanelHeight(h)
    }
    const onUp = () => {
      window.removeEventListener("mousemove", onMove)
      window.removeEventListener("mouseup", onUp)
    }
    window.addEventListener("mousemove", onMove)
    window.addEventListener("mouseup", onUp)
  }

  const applySample = (s) => {
    setScript(s.script || "")
    setScriptInstructions("")
    const a = s.apply || {}
    if (a.captionStyle) setCaptionStyle(a.captionStyle)
    if (a.musicGenre) setMusicGenre(a.musicGenre)
    if (a.aspectRatio) setAspectRatio(a.aspectRatio)
    if (a.visualStyle) setVisualStyle(a.visualStyle)
    setPreviewVideoUrl(null)
    showSnackbar(`Loaded "${s.label}" — tweak the script or hit Generate`, "success")
  }

  const handleApplyTemplate = (defaults) => {
    if (defaults.captionStyle) setCaptionStyle(defaults.captionStyle)
    if (defaults.musicGenre) setMusicGenre(defaults.musicGenre)
    if (defaults.aspectRatio) setAspectRatio(defaults.aspectRatio)
    if (defaults.visualStyle) setVisualStyle(defaults.visualStyle)
    if (defaults.scriptInstructions) setScriptInstructions(defaults.scriptInstructions)
  }

  const handleGenerate = async () => {
    if (!script?.trim()) {
      showSnackbar("Please write a script or generate one with AI first", "warning")
      return
    }
    setGenerating(true)
    try {
      const body = {
        script: script.trim(),
        aspect_ratio: aspectRatio,
        visual_style: visualStyle,
        transition_style: transitionStyle,
        tts_provider: ttsProvider,
        caption_enabled: captionEnabled,
        caption_style: captionEnabled ? captionStyle : undefined,
        music_enabled: musicEnabled,
        music_genre: musicEnabled ? musicGenre : undefined,
        source_id: sourceId || undefined,
        start_image: operation === "i2v" ? startImage : undefined,
      }
      await http.post("/api/generate/stock", body)
      showSnackbar("Stock video generation started!", "success")
      navigate("/videos?tab=generated")
    } catch (err) {
      showSnackbar(err.response?.data?.detail || err.message, "error")
    } finally {
      setGenerating(false)
    }
  }

  const audioProps = {
    ttsProvider, setTtsProvider, captionEnabled, setCaptionEnabled,
    captionStyle, setCaptionStyle, musicEnabled, setMusicEnabled, musicGenre, setMusicGenre,
    TTS_PROVIDERS, CAPTION_STYLES, MUSIC_GENRES, script,
  }

  const openFolder = () =>
    http.post("/api/settings/open-folder", { folder: "generated" })
      .catch(() => showSnackbar("Could not open folder", "error"))

  return (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <PageHero
        icon={<PhotoLibraryIcon />}
        title="Smart Video"
        subtitle="Turn a script into a captioned short — Pexels footage matched to every line"
        accentColor="#2E9E6B"
        actions={
          <Stack direction="row" spacing={1} alignItems="center">
            <Tooltip title="Open generated folder">
              <Button size="small" variant="outlined" sx={{ minWidth: 0, px: 1 }} onClick={openFolder}>
                <FolderOpenIcon fontSize="small" />
              </Button>
            </Tooltip>
            <Button
              variant="contained" size="medium"
              disabled={generating || !script?.trim()}
              onClick={handleGenerate}
              startIcon={<MovieCreationIcon />}
              sx={{ borderRadius: 2, fontWeight: 600, textTransform: "none", px: 2.5 }}
            >
              {generating ? "Starting…" : "Generate"}
            </Button>
          </Stack>
        }
      />

      <ActiveJobsBanner />

      <Box sx={{ px: 3, pt: 1.5, pb: 0.5, flexShrink: 0 }}>
        <TemplateGallery mode="stock" onApply={handleApplyTemplate} />
      </Box>

      {/* ── Studio: showcase · preview+script · config rail ─────────── */}
      <Box sx={{ flex: 1, display: "flex", overflow: "hidden", gap: 0, px: 2, pb: 2, minHeight: 0 }}>
        {/* Left — sample showcase */}
        <SampleShowcase
          samples={SMART_VIDEO_SAMPLES}
          onUse={applySample}
          open={showcaseOpen}
          onToggle={() => setShowcaseOpen((o) => !o)}
        />

        {/* Center — live preview + resizable script */}
        <Box sx={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, px: 1.5, minHeight: 0 }}>
          {/* Live preview */}
          <Box sx={{ flex: 1, minHeight: 120, display: "flex", alignItems: "center", justifyContent: "center", position: "relative" }}>
            <Box sx={{
              position: "relative", height: "100%",
              aspectRatio: aspectRatio === "9:16" ? "9 / 16" : "16 / 9",
              maxWidth: "100%", borderRadius: 3, overflow: "hidden",
              boxShadow: "0 12px 40px rgba(0,0,0,0.35)",
            }}>
              {previewVideoUrl ? (
                <video src={previewVideoUrl} autoPlay loop muted playsInline
                  style={{ width: "100%", height: "100%", objectFit: "cover" }} />
              ) : (
                <PreviewCanvas
                  paletteA={palette.cA}
                  paletteB={palette.cB}
                  captionText={script || "Your caption animates here"}
                  aspectRatio={aspectRatio}
                />
              )}
              <Chip
                size="small"
                label={palette.label}
                sx={{ position: "absolute", top: 8, left: 8, bgcolor: "rgba(0,0,0,0.55)", color: "#fff", fontSize: "0.68rem", height: 22 }}
              />
            </Box>
          </Box>

          {/* Drag handle */}
          <Box
            onMouseDown={onScriptResizeStart}
            sx={{
              height: 14, flexShrink: 0, cursor: "ns-resize", display: "flex",
              alignItems: "center", justifyContent: "center", color: "text.disabled",
              "&:hover": { color: "text.secondary" },
            }}
          >
            <DragHandleIcon sx={{ fontSize: 18 }} />
          </Box>

          {/* Script panel */}
          <Paper elevation={0} sx={(t) => ({
            ...glassPanelSx(t), height: scriptPanelHeight, minHeight: 180, flexShrink: 0,
            p: 1.75, borderRadius: 3, display: "flex", flexDirection: "column", position: "relative",
          })}>
            {source && (
              <Box sx={{ mb: 1 }}>
                <CompactSourcePanel source={source} />
              </Box>
            )}

            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1, flexWrap: "wrap", useFlexGap: true, gap: 1 }}>
              <Button
                size="small" variant="outlined" startIcon={<AutoAwesomeIcon />}
                onClick={() => handleGenerateScript(sourceId, aspectRatio)}
                disabled={scriptLoading}
                sx={{ textTransform: "none", borderRadius: 2 }}
              >
                {scriptLoading ? "Writing…" : "AI script"}
              </Button>
              <Button
                size="small" variant="text" startIcon={<AutoFixHighIcon />}
                onClick={handlePolishScript}
                disabled={scriptLoading || !script?.trim()}
                sx={{ textTransform: "none" }}
              >
                Polish
              </Button>
              <Box sx={{ flex: 1 }} />
              <Typography variant="caption" sx={{ color: "text.secondary" }}>
                {script ? `${script.trim().split(/\s+/).filter(Boolean).length} words` : "empty"}
              </Typography>
            </Stack>

            <TextField
              size="small" fullWidth
              placeholder="Optional: tell the AI the angle, tone, or audience…"
              value={scriptInstructions}
              onChange={(e) => setScriptInstructions(e.target.value)}
              sx={{ mb: 1 }}
            />

            <Box sx={{ flex: 1, position: "relative", minHeight: 0 }}>
              <TextField
                multiline fullWidth
                value={script}
                onChange={(e) => setScript(e.target.value)}
                placeholder="Write your script here, or hit “AI script” to draft one…"
                sx={{
                  height: "100%",
                  "& .MuiOutlinedInput-root": { height: "100%", alignItems: "flex-start", fontSize: "0.9rem" },
                  "& textarea": { height: "100% !important", overflow: "auto !important" },
                }}
              />
              <Fade in={scriptLoading}>
                <Box sx={{
                  position: "absolute", inset: 0, borderRadius: 1,
                  bgcolor: "rgba(0,0,0,0.25)", display: "flex", alignItems: "center", justifyContent: "center",
                  pointerEvents: "none",
                }}>
                  <Chip label="Writing your script…" color="primary" />
                </Box>
              </Fade>
            </Box>
          </Paper>
        </Box>

        {/* Right — config rail */}
        <StudioConfigRail
          visualStyle={visualStyle} setVisualStyle={setVisualStyle}
          aspectRatio={aspectRatio} setAspectRatio={setAspectRatio}
          operation={operation} setOperation={setOperation}
          startImage={startImage} setStartImage={setStartImage}
          transitionStyle={transitionStyle} setTransitionStyle={setTransitionStyle}
          audioProps={audioProps} ttsProvider={ttsProvider} script={script}
        />
      </Box>
    </Box>
  )
}
