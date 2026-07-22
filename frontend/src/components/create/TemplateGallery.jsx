import { useState, useEffect } from "react"
import { Box, Typography, Stack, Paper, Chip, Button, Collapse, IconButton, CircularProgress, TextField, Tooltip } from "@mui/material"
import ExpandMoreIcon from "@mui/icons-material/ExpandMore"
import ExpandLessIcon from "@mui/icons-material/ExpandLess"
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome"
import TrendingUpIcon from "@mui/icons-material/TrendingUp"
import RefreshIcon from "@mui/icons-material/Refresh"
import CloseIcon from "@mui/icons-material/Close"
import http from "../../api/http"
import useAppStore from "../../store/appStore"

/* ── Static Template Definitions ──────────────────────────────────── */

const STOCK_TEMPLATES = [
  {
    id: "hook_story_cta",
    name: "Hook → Story → CTA",
    desc: "Classic viral structure: grab attention, tell a story, drive action",
    icon: "🎣",
    tags: ["viral", "short-form"],
    defaults: {
      captionStyle: "viral",
      musicGenre: "upbeat",
      aspectRatio: "9:16",
      scriptInstructions: "Write a 60-second script with a strong hook in the first 3 seconds, a compelling story in the middle, and a clear call-to-action at the end. Use short punchy sentences.",
    },
  },
  {
    id: "listicle",
    name: "Top 5 Listicle",
    desc: "Numbered list format that keeps viewers watching for the next item",
    icon: "📋",
    tags: ["educational", "retention"],
    defaults: {
      captionStyle: "bold",
      musicGenre: "lofi",
      aspectRatio: "9:16",
      scriptInstructions: "Write a Top 5 listicle script. Start with 'Number 5...' and count down to Number 1 (the best). Each item gets 10-15 seconds. Add a surprising twist for #1.",
    },
  },
  {
    id: "before_after",
    name: "Before & After",
    desc: "Show transformation — problem vs solution, old vs new",
    icon: "🔄",
    tags: ["transformation", "satisfying"],
    defaults: {
      captionStyle: "karaoke",
      musicGenre: "cinematic",
      aspectRatio: "9:16",
      scriptInstructions: "Write a before-and-after transformation script. First half: paint the problem vividly (pain, frustration). Second half: reveal the solution dramatically. End with the satisfying result.",
    },
  },
  {
    id: "did_you_know",
    name: "Did You Know?",
    desc: "Curiosity-driven educational content that feels like discovery",
    icon: "💡",
    tags: ["educational", "curiosity"],
    defaults: {
      captionStyle: "neon",
      musicGenre: "ambient",
      aspectRatio: "9:16",
      scriptInstructions: "Write an educational 'Did you know?' script. Open with a mind-blowing fact, then explain why it matters. Use surprising statistics. End with 'Follow for more facts like this.'",
    },
  },
  {
    id: "story_time",
    name: "Story Time",
    desc: "Personal narrative that builds emotional connection",
    icon: "📖",
    tags: ["narrative", "emotional"],
    defaults: {
      captionStyle: "classic",
      musicGenre: "lofi",
      aspectRatio: "9:16",
      scriptInstructions: "Write a personal story script in first person. Start with a dramatic moment to hook the viewer. Build tension. Deliver a satisfying conclusion with a lesson learned.",
    },
  },
]

const TEMPLATE_MAP = {
  stock: STOCK_TEMPLATES,
}

/* ── Component ─────────────────────────────────────────────────────── */

export default function TemplateGallery({ mode, onApply, variant }) {
  const drawer = variant === "drawer" // render bare (no collapse header, cards wrap) for a right-side drawer
  const [expanded, setExpanded] = useState(true)
  const [selectedId, setSelectedId] = useState(null)
  const [trendingTemplates, setTrendingTemplates] = useState([])
  const [loadingTrending, setLoadingTrending] = useState(false)
  const [refreshNiche, setRefreshNiche] = useState("")
  const [generating, setGenerating] = useState(false)

  const staticTemplates = TEMPLATE_MAP[mode] || []

  // Fetch existing dynamic templates on mount
  useEffect(() => {
    let cancelled = false
    const fetchTrending = async () => {
      try {
        setLoadingTrending(true)
        const resp = await http.get(`/api/templates?mode=${mode}`)
        if (!cancelled && resp.data?.templates?.length) {
          setTrendingTemplates(resp.data.templates)
        }
      } catch {
        // Silent — trending templates are optional
      } finally {
        if (!cancelled) setLoadingTrending(false)
      }
    }
    fetchTrending()
    return () => { cancelled = true }
  }, [mode])

  const handleSelect = (template) => {
    setSelectedId(template.id)
    onApply(template.defaults)
  }

  const showSnackbar = useAppStore((s) => s.showSnackbar)

  const handleGenerateTrending = async () => {
    if (!refreshNiche.trim()) return
    setGenerating(true)
    try {
      const resp = await http.post(`/api/templates/refresh?mode=${mode}&niche=${encodeURIComponent(refreshNiche.trim())}`)
      if (resp.data?.templates?.length) {
        setTrendingTemplates(prev => [...resp.data.templates, ...prev.filter(t => t.niche !== refreshNiche.trim().toLowerCase())])
        showSnackbar(`Generated ${resp.data.templates.length} trending templates`, "success")
      } else {
        showSnackbar(resp.data?.message || resp.data?.error || "No templates generated — try a different niche", "warning")
      }
    } catch (err) {
      console.error("Template generation failed:", err)
      const detail = err.response?.data?.detail || err.response?.data?.error || err.message || "Unknown error"
      showSnackbar(`Template generation failed: ${detail}`, "error")
    } finally {
      setGenerating(false)
    }
  }

  const handleDeleteTemplate = async (e, template) => {
    e.stopPropagation()
    if (!template.id) return
    try {
      await http.delete(`/api/templates/${template.id}`)
      setTrendingTemplates(prev => prev.filter(t => t.id !== template.id))
      showSnackbar("Template deleted", "success")
    } catch {
      showSnackbar("Failed to delete template", "error")
    }
  }

  const allTemplates = [...staticTemplates, ...trendingTemplates]
  if (allTemplates.length === 0 && !loadingTrending) return null

  return (
    <Paper
      variant={drawer ? "elevation" : "outlined"}
      elevation={0}
      sx={{
        borderRadius: drawer ? 0 : 2.5,
        mb: drawer ? 0 : 2.5,
        overflow: "hidden",
        borderColor: "divider",
        ...(drawer && { bgcolor: "transparent" }),
      }}
    >
      {/* Header (hidden in drawer — the Drawer supplies its own) */}
      {!drawer && (
      <Box
        onClick={() => setExpanded(!expanded)}
        sx={{
          px: 2, py: 1.25,
          display: "flex", alignItems: "center", justifyContent: "space-between",
          cursor: "pointer",
          bgcolor: "action.hover",
          "&:hover": { bgcolor: "action.selected" },
          transition: "background 0.15s",
        }}
      >
        <Stack direction="row" alignItems="center" spacing={1}>
          <AutoAwesomeIcon sx={{ fontSize: 18, color: "primary.main" }} />
          <Typography variant="subtitle2" sx={{ fontWeight: 700, fontSize: "0.85rem" }}>
            Templates
          </Typography>
          <Typography variant="caption" sx={{ color: "text.secondary" }}>
            Pick a starting point
          </Typography>
          {trendingTemplates.length > 0 && (
            <Chip
              icon={<TrendingUpIcon sx={{ fontSize: 14 }} />}
              label={`${trendingTemplates.length} trending`}
              size="small"
              color="warning"
              variant="outlined"
              sx={{ fontSize: "0.65rem", height: 20, "& .MuiChip-label": { px: 0.5 } }}
            />
          )}
        </Stack>
        <IconButton size="small" sx={{ p: 0.25 }}>
          {expanded ? <ExpandLessIcon fontSize="small" /> : <ExpandMoreIcon fontSize="small" />}
        </IconButton>
      </Box>
      )}

      {/* Template cards */}
      <Collapse in={drawer || expanded}>
        <Box sx={{
          display: "flex", flexWrap: drawer ? "wrap" : "nowrap", gap: 1.5, p: 2, overflowX: drawer ? "visible" : "auto",
          "&::-webkit-scrollbar": { height: 4 },
          "&::-webkit-scrollbar-thumb": { bgcolor: "divider", borderRadius: 2 },
        }}>
          {staticTemplates.map((t) => (
            <TemplateCard
              key={t.id}
              template={t}
              isSelected={selectedId === t.id}
              onSelect={handleSelect}
            />
          ))}

          {/* Divider between static and trending */}
          {trendingTemplates.length > 0 && (
            <Box sx={{ display: "flex", alignItems: "center", px: 0.5, flexShrink: 0 }}>
              <Box sx={{ width: 1, height: "60%", bgcolor: "divider" }} />
            </Box>
          )}

          {trendingTemplates.map((t) => (
            <TemplateCard
              key={t.id}
              template={t}
              isSelected={selectedId === t.id}
              onSelect={handleSelect}
              onDelete={handleDeleteTemplate}
              isTrending
            />
          ))}

          {loadingTrending && (
            <Box sx={{ display: "flex", alignItems: "center", px: 2, flexShrink: 0 }}>
              <CircularProgress size={20} />
            </Box>
          )}
        </Box>

        {/* Generate trending templates */}
        <Box sx={{
          px: 2, pb: 1.5, pt: 0,
          display: "flex", alignItems: "center", gap: 1,
        }}>
          <TrendingUpIcon sx={{ fontSize: 16, color: "text.secondary" }} />
          <TextField
            size="small"
            placeholder="Enter niche to generate trending templates..."
            value={refreshNiche}
            onChange={(e) => setRefreshNiche(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleGenerateTrending()}
            sx={{
              flex: 1,
              "& .MuiInputBase-root": { height: 32, fontSize: "0.8rem" },
            }}
          />
          <Tooltip title="Generate trending templates from YouTube search data + your scout results">
            <span>
              <Button
                size="small"
                variant="outlined"
                onClick={handleGenerateTrending}
                disabled={generating || !refreshNiche.trim()}
                startIcon={generating ? <CircularProgress size={14} /> : <RefreshIcon sx={{ fontSize: 16 }} />}
                sx={{ fontSize: "0.75rem", textTransform: "none", whiteSpace: "nowrap", height: 32 }}
              >
                {generating ? "Generating..." : "Generate Template"}
              </Button>
            </span>
          </Tooltip>
        </Box>
      </Collapse>
    </Paper>
  )
}

/* ── Template Card ─────────────────────────────────────────────────── */

function TemplateCard({ template: t, isSelected, onSelect, onDelete, isTrending }) {
  return (
    <Paper
      variant="outlined"
      onClick={() => onSelect(t)}
      sx={{
        minWidth: 180, maxWidth: 200, p: 1.5,
        borderRadius: 2, cursor: "pointer",
        borderColor: isSelected ? "primary.main" : isTrending ? "warning.main" : "divider",
        borderWidth: isSelected ? 2 : 1,
        bgcolor: isSelected ? "action.selected" : "background.paper",
        boxShadow: isSelected ? 2 : 0,
        transition: "all 0.15s",
        "&:hover": {
          borderColor: isTrending ? "warning.light" : "primary.light",
          bgcolor: "action.hover",
          transform: "translateY(-1px)",
          boxShadow: 2,
        },
        display: "flex", flexDirection: "column", gap: 0.75,
        flexShrink: 0,
      }}
    >
      {/* Trending badge + delete row */}
      {isTrending && (
        <Stack direction="row" alignItems="center" justifyContent="space-between">
          <Chip
            icon={<TrendingUpIcon sx={{ fontSize: 10 }} />}
            label={t.niche || "trending"}
            size="small"
            color="warning"
            sx={{
              fontSize: "0.55rem", height: 16, maxWidth: 140,
              "& .MuiChip-label": { px: 0.4, overflow: "hidden", textOverflow: "ellipsis" },
              "& .MuiChip-icon": { ml: 0.3 },
            }}
          />
          {onDelete && (
            <IconButton
              size="small"
              onClick={(e) => onDelete(e, t)}
              sx={{ p: 0.25, ml: 0.5, opacity: 0.5, "&:hover": { opacity: 1 } }}
            >
              <CloseIcon sx={{ fontSize: 12 }} />
            </IconButton>
          )}
        </Stack>
      )}

      <Stack direction="row" alignItems="center" spacing={0.75}>
        <Typography sx={{ fontSize: "1.2rem", lineHeight: 1 }}>{t.icon}</Typography>
        <Typography variant="body2" sx={{ fontWeight: 700, fontSize: "0.82rem", lineHeight: 1.2 }}>
          {t.name}
        </Typography>
      </Stack>
      <Typography variant="caption" sx={{ color: "text.secondary", lineHeight: 1.3, fontSize: "0.7rem" }}>
        {t.desc}
      </Typography>
      <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ gap: 0.4 }}>
        {(t.tags || []).map(tag => (
          <Chip key={tag} label={tag} size="small" variant="outlined"
            sx={{ fontSize: "0.6rem", height: 18, "& .MuiChip-label": { px: 0.75 } }} />
        ))}
      </Stack>
    </Paper>
  )
}
