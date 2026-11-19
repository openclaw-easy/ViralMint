import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { Box, Typography, Stack, CardActionArea, Divider, TextField, InputAdornment } from "@mui/material"
import SearchOutlinedIcon from "@mui/icons-material/SearchOutlined"
import useDocumentTitle from "../hooks/useDocumentTitle"
import PageHero from "../components/PageHero"
import { alpha } from "@mui/material/styles"
import { GlassCard } from "../utils/glassFx"
import BuildOutlinedIcon from "@mui/icons-material/BuildOutlined"
import SubtitlesOutlinedIcon from "@mui/icons-material/SubtitlesOutlined"
import AspectRatioOutlinedIcon from "@mui/icons-material/AspectRatioOutlined"
import GraphicEqOutlinedIcon from "@mui/icons-material/GraphicEqOutlined"
import BrandingWatermarkOutlinedIcon from "@mui/icons-material/BrandingWatermarkOutlined"
import FastRewindOutlinedIcon from "@mui/icons-material/FastRewindOutlined"
import RecordVoiceOverOutlinedIcon from "@mui/icons-material/RecordVoiceOverOutlined"
import GraphicEqIcon from "@mui/icons-material/GraphicEq"
import TransformIcon from "@mui/icons-material/Transform"
import LightbulbOutlinedIcon from "@mui/icons-material/LightbulbOutlined"
import TranslateOutlinedIcon from "@mui/icons-material/TranslateOutlined"
import MovieFilterOutlinedIcon from "@mui/icons-material/MovieFilterOutlined"
import VideocamOutlinedIcon from "@mui/icons-material/VideocamOutlined"
import HeadphonesOutlinedIcon from "@mui/icons-material/HeadphonesOutlined"
import ClosedCaptionOutlinedIcon from "@mui/icons-material/ClosedCaptionOutlined"
import GifOutlinedIcon from "@mui/icons-material/GifOutlined"
import SpeedOutlinedIcon from "@mui/icons-material/SpeedOutlined"
import LabelOutlinedIcon from "@mui/icons-material/LabelOutlined"
import FormatListNumberedIcon from "@mui/icons-material/FormatListNumbered"
import ContentCutOutlinedIcon from "@mui/icons-material/ContentCutOutlined"
import ZoomInMapOutlinedIcon from "@mui/icons-material/ZoomInMapOutlined"
import CompressOutlinedIcon from "@mui/icons-material/CompressOutlined"
import CropOutlinedIcon from "@mui/icons-material/CropOutlined"
import VolumeOffOutlinedIcon from "@mui/icons-material/VolumeOffOutlined"
import DownloadForOfflineOutlinedIcon from "@mui/icons-material/DownloadForOfflineOutlined"
import ArticleOutlinedIcon from "@mui/icons-material/ArticleOutlined"

// ── Catalog ────────────────────────────────────────────────────────────────
//
// Each tool has a `category` that maps into the CATEGORIES array below.
// The open-source build ships the local FFmpeg / Whisper / Edge-TTS tools;
// media-generation tools (AI image / music / video / SFX / thumbnail) are
// not part of this build since they require external media-gen providers.

const TOOLS = [
  // Video — visual / structural edits
  {
    id: "video-download",
    title: "Video Download",
    description: "Paste a link and pull the video down — pick the quality, keep or embed subtitles.",
    icon: <DownloadForOfflineOutlinedIcon />,
    category: "video",
  },
  {
    id: "merge-clips",
    title: "Merge Clips",
    description: "Stitch multiple clips into one video. Auto-crop to your target aspect.",
    icon: <MovieFilterOutlinedIcon />,
    category: "video",
  },
  {
    id: "transform",
    title: "Transform",
    description: "Flip, rotate, loop, or change a clip's volume.",
    icon: <TransformIcon />,
    category: "video",
  },
  {
    // One runner, one job type, one history — but its own card, because
    // "remove audio" is what people search for, not "transform".
    id: "remove-audio",
    title: "Remove Audio",
    description: "Strip the sound from a video and keep the picture untouched.",
    icon: <VolumeOffOutlinedIcon />,
    category: "audio",
    route: "/tools/transform?op=mute",
  },
  {
    id: "compress",
    title: "Compress Video",
    description: "Shrink a file for email, chat, or an upload limit. Pick a size and a strength.",
    icon: <CompressOutlinedIcon />,
    category: "video",
  },
  {
    id: "crop",
    title: "Crop Video",
    description: "Drag a box over the frame and keep just that part. Free-form or a fixed shape.",
    icon: <CropOutlinedIcon />,
    category: "video",
  },
  {
    id: "reframe",
    title: "Reframe to Vertical",
    description: "Convert 16:9 to 9:16 with face-tracking for Shorts/TikTok.",
    icon: <AspectRatioOutlinedIcon />,
    category: "video",
  },
  {
    id: "watermark",
    title: "Add Watermark",
    description: "Brand every export with your logo. Position, opacity, size.",
    icon: <BrandingWatermarkOutlinedIcon />,
    category: "video",
  },
  {
    id: "gif",
    title: "Video → GIF",
    description: "Convert any clip to an animated GIF. Two-pass palette for clean colors.",
    icon: <GifOutlinedIcon />,
    category: "video",
  },
  {
    id: "speed",
    title: "Speed up / Slow down",
    description: "Re-time a video 0.25× to 4×, with optional pitch preservation.",
    icon: <SpeedOutlinedIcon />,
    category: "video",
  },
  {
    id: "trim",
    title: "Trim / Cut",
    description: "Keep just the part you want — set a start and end, drop the rest.",
    icon: <ContentCutOutlinedIcon />,
    category: "video",
  },
  {
    id: "auto-zoom",
    title: "Auto-Zoom",
    description: "Subtle zoom punch-ins on spoken words for extra energy.",
    icon: <ZoomInMapOutlinedIcon />,
    category: "video",
  },

  // Audio — cleanup / processing / voice
  {
    id: "voiceover",
    title: "Voice-over Generator",
    description: "Turn a script into natural TTS audio.",
    icon: <RecordVoiceOverOutlinedIcon />,
    category: "audio",
  },
  {
    id: "audio-enhance",
    title: "Enhance Audio",
    description: "Denoise hiss, normalize loudness, polish speech.",
    icon: <GraphicEqOutlinedIcon />,
    category: "audio",
  },
  {
    id: "remove-silence",
    title: "Silence Remover",
    description: "Auto-cut pauses and dead air.",
    icon: <FastRewindOutlinedIcon />,
    category: "audio",
  },
  {
    id: "music-visualizer",
    title: "Music Visualizer",
    description: "Audio → animated waveform / bars / spectrum video, synced to the sound.",
    icon: <GraphicEqIcon />,
    category: "audio",
  },

  // Subtitles & Insights — text overlays + analysis
  {
    id: "captions",
    title: "Add Captions",
    description: "Burn word-by-word captions. Viral, classic, or bold styles.",
    icon: <SubtitlesOutlinedIcon />,
    category: "subtitles",
  },
  {
    id: "subtitles",
    title: "Export Subtitles",
    description: "Transcribe to a downloadable .srt / .vtt file or plain transcript.",
    icon: <ClosedCaptionOutlinedIcon />,
    category: "subtitles",
  },
  {
    id: "transcript",
    title: "Transcript Downloader",
    description: "Paste a link and grab the captions it already has as .txt / .srt / .vtt — no transcribing.",
    icon: <ArticleOutlinedIcon />,
    category: "subtitles",
  },
  {
    id: "translate",
    title: "Translate + Dub",
    description: "Translate captions to 19+ languages or dub the audio entirely.",
    icon: <TranslateOutlinedIcon />,
    category: "subtitles",
  },
  {
    id: "hook-analysis",
    title: "Hook Detector",
    description: "Score the first 10s of any video + get stronger opening alternatives.",
    icon: <LightbulbOutlinedIcon />,
    category: "subtitles",
  },
  {
    id: "metadata",
    title: "Title / Tags / Description",
    description: "SEO metadata for any video, transcript, or topic — YT + TikTok ready.",
    icon: <LabelOutlinedIcon />,
    category: "subtitles",
  },
  {
    id: "auto-chapters",
    title: "Auto Chapters",
    description: "AI-generated YouTube chapter markers from your long-form video.",
    icon: <FormatListNumberedIcon />,
    category: "subtitles",
  },
]

// ── Category metadata ──────────────────────────────────────────────────────
//
// `accent` keys map to MUI theme palette slots — used for the section
// header's icon background tint and the colored left-border on each card,
// so each section reads at a glance even when scrolling fast.

const CATEGORIES = [
  {
    id: "video",
    label: "Video",
    description: "Visual and structural edits to finished footage.",
    icon: <VideocamOutlinedIcon />,
    accent: "info",
  },
  {
    id: "audio",
    label: "Audio",
    description: "Voice, cleanup, and polish for the soundtrack.",
    icon: <HeadphonesOutlinedIcon />,
    accent: "success",
  },
  {
    id: "subtitles",
    label: "Subtitles & Insights",
    description: "Captions, translation, and analysis overlays.",
    icon: <ClosedCaptionOutlinedIcon />,
    accent: "warning",
  },
]

// ── Components ─────────────────────────────────────────────────────────────

function ToolCard({ tool, onClick }) {
  return (
    <GlassCard sx={{ borderRadius: 2 }}>
      <CardActionArea
        onClick={onClick}
        sx={{
          p: 1.25,
          height: "100%",
          display: "flex",
          flexDirection: "column",
          alignItems: "flex-start",
        }}
      >
        <Stack direction="row" alignItems="center" spacing={1.25} sx={{ mb: 0.5, width: "100%" }}>
          <Box sx={{ color: "primary.main", display: "flex", "& svg": { fontSize: 22 } }}>
            {tool.icon}
          </Box>
          <Typography variant="body2" sx={{ fontWeight: 700, fontSize: "0.9rem", flex: 1 }}>
            {tool.title}
          </Typography>
        </Stack>
        <Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            lineHeight: 1.4,
            fontSize: "0.78rem",
            display: "-webkit-box",
            WebkitBoxOrient: "vertical",
            WebkitLineClamp: 2,
            overflow: "hidden",
          }}
        >
          {tool.description}
        </Typography>
      </CardActionArea>
    </GlassCard>
  )
}

function CategorySection({ category, tools, onPick }) {
  return (
    <Box sx={{ mb: 2 }}>
      {/* Section header */}
      <Stack direction="row" alignItems="center" spacing={1.25} sx={{ mb: 1, px: 0.5 }}>
        <Box sx={{
          width: 32, height: 32,
          borderRadius: 1.25,
          display: "flex", alignItems: "center", justifyContent: "center",
          bgcolor: (t) => alpha(t.palette[category.accent].main, t.palette.mode === "dark" ? 0.22 : 0.13),
          color: `${category.accent}.main`,
          "& svg": { fontSize: 20 },
        }}>
          {category.icon}
        </Box>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Stack direction="row" alignItems="baseline" spacing={1}>
            <Typography variant="subtitle1" sx={{ fontWeight: 700, letterSpacing: -0.2 }}>
              {category.label}
            </Typography>
            <Typography variant="caption" sx={{ color: "text.disabled", fontWeight: 600 }}>
              {tools.length}
            </Typography>
          </Stack>
          <Typography variant="caption" sx={{ color: "text.secondary", display: "block", lineHeight: 1.3 }}>
            {category.description}
          </Typography>
        </Box>
      </Stack>

      {/* Grid */}
      <Box
        sx={{
          display: "grid",
          gap: 1.5,
          gridTemplateColumns: {
            xs: "1fr",
            sm: "repeat(2, 1fr)",
            md: "repeat(3, 1fr)",
            lg: "repeat(3, 1fr)",
          },
        }}
      >
        {tools.map((t) => (
          <ToolCard key={t.id} tool={t} onClick={() => onPick(t.id)} />
        ))}
      </Box>
    </Box>
  )
}

// ── Page ───────────────────────────────────────────────────────────────────

export default function Tools() {
  useDocumentTitle("Tools")
  const navigate = useNavigate()
  const pick = (id) => {
    const tool = TOOLS.find((t) => t.id === id)
    navigate(tool?.route || `/tools/${id}`)
  }

  // Hub search — a flat ranked result grid when a query is present, else the
  // categorized layout (title/description/id all match against the query).
  const [query, setQuery] = useState("")
  const q = query.trim().toLowerCase()
  const matches = q
    ? TOOLS.filter((t) => [t.title, t.description, t.id].some((s) => (s || "").toLowerCase().includes(q)))
    : null

  return (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <PageHero
        icon={<BuildOutlinedIcon sx={{ fontSize: 22 }} />}
        title="Tools"
        subtitle="Single-purpose utilities to refine finished videos"
      />

      {/* Body */}
      <Box sx={{ flex: 1, overflow: "auto", p: { xs: 2, md: 3 } }}>
        <Box sx={{ maxWidth: 1280, mx: "auto" }}>

          {/* Search */}
          <TextField
            fullWidth
            size="small"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search tools — e.g. caption, reframe, speed, translate…"
            sx={{ mb: 2.5 }}
            slotProps={{
              input: {
                startAdornment: (
                  <InputAdornment position="start">
                    <SearchOutlinedIcon fontSize="small" sx={{ color: "text.secondary" }} />
                  </InputAdornment>
                ),
              },
            }}
          />

          {matches ? (
            /* Flat search results */
            matches.length ? (
              <Box>
                <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mb: 1.5 }}>
                  {matches.length} {matches.length === 1 ? "tool" : "tools"} matching “{query.trim()}”
                </Typography>
                <Box sx={{ display: "grid", gap: 1.5, gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)", md: "repeat(3, 1fr)" } }}>
                  {matches.map((t) => (
                    <ToolCard key={t.id} tool={t} onClick={() => pick(t.id)} />
                  ))}
                </Box>
              </Box>
            ) : (
              <Typography variant="body2" sx={{ color: "text.secondary", textAlign: "center", py: 6 }}>
                No tools match “{query.trim()}”. Try “caption”, “audio”, “speed”, or “translate”.
              </Typography>
            )
          ) : (
            <>
              {/* Categories */}
              {CATEGORIES.map((cat, i) => {
                const tools = TOOLS.filter((t) => t.category === cat.id)
                if (!tools.length) return null
                return (
                  <Box key={cat.id}>
                    <CategorySection category={cat} tools={tools} onPick={pick} />
                    {i < CATEGORIES.length - 1 && (
                      <Divider sx={{ mb: 2, opacity: 0.5 }} />
                    )}
                  </Box>
                )
              })}
            </>
          )}
        </Box>
      </Box>
    </Box>
  )
}
