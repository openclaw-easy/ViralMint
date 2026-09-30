import { useState } from "react"
import { Box, Typography, Button, ButtonGroup, IconButton, Stack, Drawer, Dialog, DialogTitle, DialogContent, DialogActions } from "@mui/material"
import GridViewIcon from "@mui/icons-material/GridView"
import TableRowsIcon from "@mui/icons-material/TableRows"
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline"
import CloseIcon from "@mui/icons-material/Close"
import VisibilityIcon from "@mui/icons-material/VisibilityOutlined"
import NewspaperIcon from "@mui/icons-material/Newspaper"
import LaunchIcon from "@mui/icons-material/Launch"
import { Chip, Divider } from "@mui/material"
import http from "../../api/http"
import useAppStore from "../../store/appStore"
import CardGrid from "./CardGrid"
import DataTable from "./DataTable"
import VideoEmbed from "./VideoEmbed"

export default function ScoutResults({ results, onSelect, onRefresh }) {
  const [view, setView] = useState("cards")
  const [selectedResult, setSelectedResult] = useState(null)
  const [selectedIds, setSelectedIds] = useState(new Set())
  const [deleting, setDeleting] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const showSnackbar = useAppStore((s) => s.showSnackbar)

  const handleSelect = (r) => {
    setSelectedResult(r)
    onSelect?.(r)
  }

  const toggleSelection = (id, e) => {
    e.stopPropagation()
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectAll = () => {
    if (selectedIds.size === results.length) setSelectedIds(new Set())
    else setSelectedIds(new Set(results.map((r) => r.id)))
  }

  const handleDownloadOne = async (id) => {
    // Check if this is a news article — use save endpoint instead of download
    const result = results?.find(r => r.id === id)
    if (result?.platform === "news") {
      try {
        await http.post("/api/news/save", { article_ids: [id] })
        showSnackbar("Saving article to Library...", "success")
      } catch (err) {
        showSnackbar(err.response?.data?.detail || err.message, "error")
      }
      return
    }
    try {
      await http.post("/api/scout/download", { scout_result_ids: [id] })
      showSnackbar("Downloading & analyzing video...", "success")
    } catch (err) {
      showSnackbar(err.response?.data?.detail || err.message, "error")
    }
  }

  const handleDeleteOne = async (id) => {
    try {
      await http.delete(`/api/scout/results/${id}`)
      showSnackbar("Deleted", "success")
      selectedIds.delete(id)
      setSelectedIds(new Set(selectedIds))
      if (selectedResult?.id === id) setSelectedResult(null)
      onRefresh?.()
    } catch (err) {
      showSnackbar(err.response?.data?.detail || err.message, "error")
    }
  }

  const handleBatchDelete = async () => {
    if (selectedIds.size === 0) return
    setDeleting(true)
    try {
      const ids = [...selectedIds]
      await Promise.all(ids.map((id) => http.delete(`/api/scout/results/${id}`)))
      showSnackbar(`Deleted ${ids.length} result${ids.length > 1 ? "s" : ""}`, "success")
      setSelectedIds(new Set())
      if (selectedResult && ids.includes(selectedResult.id)) setSelectedResult(null)
      onRefresh?.()
    } catch (err) {
      showSnackbar(err.response?.data?.detail || err.message, "error")
    } finally {
      setDeleting(false)
    }
  }

  return (
    <Box>
      {/* Toolbar */}
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" sx={{ mb: 2 }}>
        <ButtonGroup size="small" variant="outlined">
          <Button onClick={() => setView("cards")} variant={view === "cards" ? "contained" : "outlined"} startIcon={<GridViewIcon />}>Cards</Button>
          <Button onClick={() => setView("table")} variant={view === "table" ? "contained" : "outlined"} startIcon={<TableRowsIcon />}>Table</Button>
        </ButtonGroup>

        <Box sx={{ flex: 1 }} />

        <Typography variant="body2" sx={{ color: "text.secondary" }}>
          {results?.length || 0} results
        </Typography>

        {results?.length > 0 && (
          <Button size="small" variant="outlined" color="inherit" onClick={selectAll}>
            {selectedIds.size === results.length ? "Deselect All" : "Select All"}
          </Button>
        )}

        {selectedIds.size > 0 && (
          <Button
            size="small"
            variant="outlined"
            color="error"
            startIcon={<DeleteOutlineIcon />}
            onClick={() => setConfirmOpen(true)}
            disabled={deleting}
          >
            {deleting ? "Deleting..." : `Delete (${selectedIds.size})`}
          </Button>
        )}
      </Stack>

      {view === "cards" && <CardGrid results={results} onSelect={handleSelect} onDownload={handleDownloadOne} onDelete={handleDeleteOne} selectedIds={selectedIds} onToggle={toggleSelection} />}
      {view === "table" && <DataTable results={results} onSelect={handleSelect} onDownload={handleDownloadOne} onDelete={handleDeleteOne} selectedIds={selectedIds} onToggle={toggleSelection} />}

      {/* Delete confirmation dialog */}
      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle>Delete {selectedIds.size} result{selectedIds.size > 1 ? "s" : ""}?</DialogTitle>
        <DialogContent><Typography>This will permanently remove the selected scout results.</Typography></DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmOpen(false)}>Cancel</Button>
          <Button color="error" variant="contained" disabled={deleting} onClick={() => { setConfirmOpen(false); handleBatchDelete() }}>
            {deleting ? "Deleting..." : "Delete"}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Detail panel — a right-hand drawer. It used to render BELOW the whole
          results grid, so clicking a card in a long list opened something
          thousands of pixels down the page and the click appeared to do
          nothing. */}
      <Drawer
        anchor="right"
        open={!!selectedResult}
        onClose={() => setSelectedResult(null)}
        slotProps={{ paper: { sx: { width: { xs: "100%", sm: 560 }, maxWidth: "100%", p: 2 } } }}
      >
      {selectedResult && (
        <Box data-testid="scout-detail">
          <Stack direction="row" justifyContent="space-between" alignItems="start">
            <Box sx={{ flex: 1, minWidth: 0 }}>
              <Typography variant="h6" sx={{ wordBreak: "break-word" }}>{selectedResult.title}</Typography>
              {selectedResult.platform === "news" ? (
                <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 0.5 }}>
                  <Chip icon={<NewspaperIcon />} label="NEWS" size="small" variant="outlined" color="warning" />
                  <Typography variant="body2" sx={{ color: "text.secondary" }}>
                    {selectedResult.author} · Score: {selectedResult.virality_score}
                    {selectedResult.upload_date && ` · ${new Date(selectedResult.upload_date).toLocaleDateString()}`}
                  </Typography>
                </Stack>
              ) : (
                <Typography variant="body2" sx={{ color: "text.secondary" }}>
                  {selectedResult.author} &middot; {selectedResult.platform} &middot; <VisibilityIcon sx={{ fontSize: 14, verticalAlign: "middle", mr: 0.3 }} />{selectedResult.views?.toLocaleString()} &middot; Score: {selectedResult.virality_score}
                </Typography>
              )}
            </Box>
            <IconButton size="small" aria-label="Close details" onClick={() => setSelectedResult(null)}>
              <CloseIcon />
            </IconButton>
          </Stack>

          {selectedResult.platform === "news" ? (
            <Box sx={{ mt: 1.5 }}>
              {(() => {
                let desc = null
                try { desc = typeof selectedResult.description === "string" ? JSON.parse(selectedResult.description) : selectedResult.description } catch { /* ignore */ }
                return desc ? (
                  <Stack spacing={1.5}>
                    {desc.why_trending && (
                      <Typography variant="body2" sx={{ fontStyle: "italic", color: "warning.main" }}>
                        {desc.why_trending}
                      </Typography>
                    )}
                    {desc.hook && (
                      <Box>
                        <Typography variant="caption" sx={{ fontWeight: 700, color: "text.secondary" }}>Video Hook</Typography>
                        <Typography variant="body2">{desc.hook}</Typography>
                      </Box>
                    )}
                    {desc.suggested_angle && (
                      <Box>
                        <Typography variant="caption" sx={{ fontWeight: 700, color: "text.secondary" }}>Suggested Angle</Typography>
                        <Typography variant="body2">{desc.suggested_angle}</Typography>
                      </Box>
                    )}
                    {desc.talking_points?.length > 0 && (
                      <Box>
                        <Typography variant="caption" sx={{ fontWeight: 700, color: "text.secondary" }}>Talking Points</Typography>
                        {desc.talking_points.map((pt, i) => (
                          <Typography key={i} variant="body2" sx={{ pl: 1 }}>• {pt}</Typography>
                        ))}
                      </Box>
                    )}
                    {desc.full_text_preview && (
                      <>
                        <Divider />
                        <Typography variant="body2" sx={{ color: "text.secondary", fontSize: "0.82rem" }}>
                          {desc.full_text_preview}
                        </Typography>
                      </>
                    )}
                    {selectedResult.video_url && (
                      <Button size="small" variant="outlined" startIcon={<LaunchIcon />}
                        onClick={() => window.open(selectedResult.video_url, "_blank", "noopener")}
                        sx={{ alignSelf: "flex-start", textTransform: "none" }}>
                        Read full article
                      </Button>
                    )}
                  </Stack>
                ) : null
              })()}
            </Box>
          ) : (selectedResult.video_id || selectedResult.video_url) ? (
            <Box sx={{ mt: 1.5 }}>
              <VideoEmbed platform={selectedResult.platform} videoId={selectedResult.video_id}
                videoUrl={selectedResult.video_url} thumbnailUrl={selectedResult.thumbnail_url} />
            </Box>
          ) : null}
        </Box>
      )}
      </Drawer>
    </Box>
  )
}
