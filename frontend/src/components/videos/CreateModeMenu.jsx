import { useState } from "react"
import { Typography, Menu, MenuItem, ListItemText, ListItemIcon } from "@mui/material"
import AutoFixHighIcon from "@mui/icons-material/AutoFixHigh"
import PhotoLibraryIcon from "@mui/icons-material/PhotoLibrary"
import http from "../../api/http"
import useAppStore from "../../store/appStore"

export default function CreateModeMenu({ anchorEl, onClose, sourceId, navigate }) {
  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const [quickLoading, setQuickLoading] = useState(false)

  const handleQuickGenerate = async () => {
    onClose()
    setQuickLoading(true)
    try {
      await http.post(`/api/downloaded/${sourceId}/generate`, {
        aspect_ratio: "9:16",
        tts_provider: "edge_tts",
        caption_enabled: true,
        caption_style: "viral",
        music_enabled: true,
        music_genre: "lofi",
      })
      showSnackbar("Quick video generation started! Check the Generated tab.", "success")
    } catch (e) {
      showSnackbar(e.response?.data?.detail || `Quick generate failed: ${e.message}`, "error")
    } finally {
      setQuickLoading(false)
    }
  }

  return (
    <Menu anchorEl={anchorEl} open={Boolean(anchorEl)} onClose={onClose}>
      <MenuItem onClick={handleQuickGenerate} disabled={quickLoading}
        sx={{ borderBottom: 1, borderColor: "divider", mb: 0.5 }}>
        <ListItemIcon><AutoFixHighIcon fontSize="small" color="primary" /></ListItemIcon>
        <ListItemText
          primary={quickLoading ? "Starting..." : "Quick Stock Video"}
          secondary="One-click: stock footage + free voice + viral captions"
          primaryTypographyProps={{ fontWeight: 700, color: "primary.main" }}
          secondaryTypographyProps={{ fontSize: "0.7rem" }}
        />
      </MenuItem>
      <Typography variant="caption" sx={{ px: 2, py: 0.5, color: "text.disabled", display: "block" }}>
        Or customize in editor:
      </Typography>
      <MenuItem onClick={() => { onClose(); navigate(`/stock?source=${sourceId}`) }}>
        <ListItemIcon><PhotoLibraryIcon fontSize="small" /></ListItemIcon>
        <ListItemText>Stock Video</ListItemText>
      </MenuItem>
    </Menu>
  )
}
