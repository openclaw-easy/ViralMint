// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { memo, useEffect, useMemo, useRef, useState } from "react"
import {
  Box, Button, Chip, CircularProgress, IconButton, InputAdornment, Stack, Tab, Tabs,
  TextField, Tooltip, Typography, alpha,
} from "@mui/material"
import SearchIcon from "@mui/icons-material/SearchOutlined"
import PlayArrowRoundedIcon from "@mui/icons-material/PlayArrowRounded"
import FileUploadOutlinedIcon from "@mui/icons-material/FileUploadOutlined"
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline"
import UseClipButton from "./UseClipButton"
import { onActivate } from "../../utils/a11y"
import { BIN_INITIAL, BIN_MIN } from "./editorLayout"
import { clipInUse, isFixedVisual, queryGradient, stockLabel } from "./studioModel"

// The media bin: find something to put in the selected scene — stock
// footage, or your own uploaded clips. Tiles are thumbnails, never <video> —
// a clip plays in the player when you click it.
//
// One home per job (owner, 2026-09-16): the Media tab is where your own clips
// are added, placed and removed. The Project panel and the Scene panel link
// here instead of repeating them.

// A clip card's frame: the clip in use is ringed (a shadow, so the card doesn't
// shift by a pixel when it changes state).
const clipCardSx = (inUse, idleBorder = "divider") => ({
  borderRadius: 1.5, overflow: "hidden", border: 1, bgcolor: "background.paper",
  borderColor: inUse ? "primary.main" : idleBorder,
  boxShadow: (t) => (inUse ? `0 0 0 1px ${t.palette.primary.main}` : "none"),
})

function StockTile({ candidate, canUse, sceneN, inUse, onPreview, onUse }) {
  const label = stockLabel(candidate)
  return (
    <Box data-testid="stock-tile" sx={clipCardSx(inUse)}>
      <Box
        role="button" tabIndex={0} aria-label={`Preview ${label}`}
        onClick={() => onPreview({ candidate, label, useAs: { kind: "stock", candidate } })}
        onKeyDown={onActivate(() => onPreview({ candidate, label, useAs: { kind: "stock", candidate } }))}
        sx={{
          position: "relative", aspectRatio: "16 / 10", cursor: "pointer", display: "grid", placeItems: "center",
          background: candidate.thumbnail ? `center / cover no-repeat url("${candidate.thumbnail}")` : queryGradient(candidate.provider_id),
        }}
      >
        <PlayArrowRoundedIcon sx={{ color: "#fff", opacity: 0.9, filter: "drop-shadow(0 1px 2px rgba(0,0,0,.6))" }} />
        {!!candidate.native_duration && (
          <Typography sx={{
            position: "absolute", right: 4, bottom: 3, fontSize: 10, color: "#fff", px: 0.5, borderRadius: 0.5,
            bgcolor: "rgba(0,0,0,0.55)",
          }}>
            {Math.round(candidate.native_duration)}s
          </Typography>
        )}
      </Box>
      <Stack direction="row" alignItems="center" spacing={0.5} sx={{ pl: 1, pr: 0.625, py: 0.625 }}>
        <Typography variant="caption" noWrap sx={{ flex: 1, minWidth: 0, fontWeight: 600, fontSize: 11 }} title={label}>{label}</Typography>
        <UseClipButton name={label} sceneN={sceneN} inUse={inUse} disabled={!canUse} onUse={() => onUse(candidate)} />
      </Stack>
    </Box>
  )
}

// The left column: 380px by default (room for the player), drag its right
// edge to resize — the width is remembered; tile
// grids add columns as it widens. The editor owns the hook and passes the
// width its row budget allows (editorLayout.js).
export const MEDIA_BIN_WIDTH = { storageKey: "vm_studio_bin_w", initial: BIN_INITIAL, min: BIN_MIN, max: 900, label: "the media panel", anchor: "left" }

function MediaBin({
  selected, scenes = [], onPreview, onUseStock, onUseMine, onUpload, onSearch, mine,
  onMatchClips, onRemoveClip, request, width, resize,
}) {
  const [tab, setTab] = useState("stock")
  const [input, setInput] = useState("")
  const [search, setSearch] = useState({ query: "", loading: false, error: null, results: [] })
  const canUse = !!selected

  const searchSeq = useRef(0)
  const runSearch = async (raw) => {
    const query = String(raw || "").trim()
    if (query.length < 2) return
    const seq = ++searchSeq.current            // a slower, older answer must not overwrite a newer one
    setSearch({ query, loading: true, error: null, results: [] })
    try {
      const results = await onSearch(query)
      if (seq === searchSeq.current) setSearch({ query, loading: false, error: null, results })
    } catch (e) {
      if (seq === searchSeq.current) setSearch({ query, loading: false, error: e.message || "Stock search didn't answer — try again.", results: [] })
    }
  }

  useEffect(() => {
    if (!request) return
    if (request.tab) setTab(request.tab)
    if (request.query != null) { setInput(request.query); runSearch(request.query) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [request])

  const sceneQuery = selected?.visual?.query || ""
  // The Stock tab searches for the selected scene by itself (it sat empty
  // until you clicked the query chip — 2026-10-13 audit). Only while the box
  // holds nothing the user typed: a search of their own is never replaced.
  const autoQuery = useRef("")
  useEffect(() => {
    if (tab !== "stock" || !sceneQuery || sceneQuery === autoQuery.current) return
    if (input.trim() && input !== autoQuery.current) return
    autoQuery.current = sceneQuery
    setInput(sceneQuery)
    runSearch(sceneQuery)
  }, [selected?.uid, sceneQuery, tab])  // eslint-disable-line react-hooks/exhaustive-deps
  const usedOn = useMemo(() => {
    const m = {}
    for (const sc of scenes) {
      const id = sc.visual?.kind === "user_clip" && sc.visual.asset?.asset_id
      if (id) (m[id] ||= []).push(sc.index + 1)
    }
    return m
  }, [scenes])
  const unused = mine.filter((it) => !usedOn[it.asset_id])
  const openScenes = scenes.filter((sc) => sc.line && !isFixedVisual(sc)).length
  const { dragging, handleProps } = resize

  return (
    <Box sx={{ flex: `0 0 ${width}px`, minWidth: 0, position: "relative", display: "flex", flexDirection: "column", borderRight: 1, borderColor: "divider", bgcolor: "background.paper", minHeight: 0 }}>
      <Box
        {...handleProps}
        // Arrow keys resize here; don't let the editor's window handler also step scenes.
        onKeyDown={(e) => { handleProps.onKeyDown(e); if (e.key.startsWith("Arrow")) e.stopPropagation() }}
        sx={(t) => ({
          position: "absolute", right: -3, top: 0, bottom: 0, width: 6, zIndex: 2, cursor: "col-resize", touchAction: "none",
          bgcolor: dragging ? alpha(t.palette.primary.main, 0.5) : "transparent",
          transition: "background-color .12s",
          "&:hover, &:focus-visible": { bgcolor: alpha(t.palette.primary.main, 0.35), outline: "none" },
        })}
      />
      <Tabs value={tab} onChange={(_, v) => setTab(v)} variant="fullWidth"
        sx={{ minHeight: 40, borderBottom: 1, borderColor: "divider", "& .MuiTab-root": { minHeight: 40, minWidth: 0, textTransform: "none", fontWeight: 700, fontSize: "0.8rem" } }}>
        <Tab value="stock" label="Stock" />
        <Tab value="mine" label="Media" />
      </Tabs>
      {!selected && (
        <Typography variant="caption" data-testid="bin-no-scene"
          sx={(t) => ({ px: 1.5, py: 0.75, fontWeight: 600, bgcolor: alpha(t.palette.primary.main, 0.08), borderBottom: 1, borderColor: "divider", display: "block" })}>
          Select a scene in the timeline to use a visual for it.
        </Typography>
      )}

      <Box sx={{ flex: 1, minHeight: 0, overflowY: "auto", p: 1.25 }}>
        {tab === "stock" && (
          <>
            <TextField
              fullWidth size="small" value={input} placeholder="Search footage — press Enter"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") runSearch(input) }}
              slotProps={{
                input: { startAdornment: <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment> },
                htmlInput: { "aria-label": "Search stock footage", enterKeyHint: "search" },
              }}
              sx={{ "& .MuiOutlinedInput-root": { fontSize: "0.82rem" } }}
            />
            {sceneQuery && (
              <Stack direction="row" alignItems="center" spacing={0.5} sx={{ mt: 0.75 }}>
                <Typography variant="caption" sx={{ color: "text.secondary" }}>Scene {selected.index + 1}:</Typography>
                <Chip size="small" label={sceneQuery} onClick={() => { setInput(sceneQuery); runSearch(sceneQuery) }}
                  sx={{ height: 20, fontSize: 11, maxWidth: 190 }} />
              </Stack>
            )}
            {search.loading && (
              <Stack alignItems="center" sx={{ mt: 3 }}><CircularProgress size={18} /></Stack>
            )}
            {search.error && (
              <Typography variant="caption" sx={{ color: "warning.main", display: "block", mt: 2, textAlign: "center" }}>{search.error}</Typography>
            )}
            {!search.loading && !search.error && !search.query && (
              <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mt: 2, textAlign: "center" }}>
                Search stock footage, click a result to watch it, then Use it for the selected scene.
              </Typography>
            )}
            {!search.loading && !search.error && search.query && !search.results.length && (
              <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mt: 2, textAlign: "center" }}>
                No footage found for “{search.query}” — try fewer or simpler words.
              </Typography>
            )}
            <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 0.75, mt: 1.25 }}>
              {search.results.map((c) => (
                <StockTile key={`${c.provider}:${c.provider_id}`} candidate={c} canUse={canUse} sceneN={selected ? selected.index + 1 : null}
                  inUse={clipInUse(selected, c)} onPreview={onPreview} onUse={onUseStock} />
              ))}
            </Box>
          </>
        )}

        {tab === "mine" && (
          <>
            <Button fullWidth size="small" variant="outlined" startIcon={<FileUploadOutlinedIcon />} onClick={onUpload}
              sx={{ textTransform: "none", fontWeight: 700 }}>
              Upload clips or images
            </Button>
            {!mine.length && (
              <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mt: 2, textAlign: "center" }}>
                Your own clips make the biggest difference to how a video looks. Upload some to use them in scenes.
              </Typography>
            )}
            {unused.length > 0 && (
              <Stack direction="row" alignItems="center" spacing={1} data-testid="place-unused"
                sx={(t) => ({ mt: 1.25, p: 1, borderRadius: 1, bgcolor: alpha(t.palette.success.main, 0.08) })}>
                <Typography variant="caption" sx={{ flex: 1 }}>
                  {unused.length} clip{unused.length === 1 ? "" : "s"} not used yet
                </Typography>
                <Tooltip title={openScenes ? "Puts each clip on the open scene whose line matches its name best" : "Every scene already has a visual you chose — turn off \"Keep this visual\" on one first"}>
                  <span>
                    <Button size="small" variant="contained" disabled={!openScenes} onClick={() => onMatchClips?.(unused)}
                      sx={{ textTransform: "none", fontWeight: 700, whiteSpace: "nowrap" }}>
                      Place {unused.length} on scenes
                    </Button>
                  </span>
                </Tooltip>
              </Stack>
            )}
            <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 0.75, mt: 1.25 }}>
              {mine.map((item) => {
                const at = usedOn[item.asset_id]
                const inUse = clipInUse(selected, item)
                return (
                  <Box key={item.asset_id} data-testid="mine-tile"
                    sx={(t) => clipCardSx(inUse, at ? alpha(t.palette.success.main, 0.6) : "divider")}>
                    <Box
                      role="button" tabIndex={0} aria-label={`Preview ${item.title}`}
                      onClick={() => onPreview({ url: item.url, image: item.media === "image", label: item.title, useAs: { kind: "user_clip", asset: item } })}
                      onKeyDown={onActivate(() => onPreview({ url: item.url, image: item.media === "image", label: item.title, useAs: { kind: "user_clip", asset: item } }))}
                      sx={{
                        position: "relative", aspectRatio: "16 / 10", cursor: "pointer", display: "grid", placeItems: "center",
                        background: item.poster_url ? `center / cover no-repeat url("${item.poster_url}")` : queryGradient(item.title),
                      }}
                    >
                      {item.media !== "image" && <PlayArrowRoundedIcon sx={{ color: "#fff", opacity: 0.85 }} />}
                      <Typography sx={(t) => ({
                        position: "absolute", left: 4, top: 4, fontSize: 10, fontWeight: 700, px: 0.5, borderRadius: 0.5,
                        color: "#fff", bgcolor: at ? alpha(t.palette.success.main, 0.9) : "rgba(0,0,0,0.55)",
                      })}>
                        {at ? `Scene ${at.join(", ")}` : "Not used"}
                      </Typography>
                    </Box>
                    <Stack direction="row" alignItems="center" spacing={0.25} sx={{ pl: 1, pr: 0.625, py: 0.625 }}>
                      <Typography variant="caption" noWrap sx={{ flex: 1, minWidth: 0, fontWeight: 600, fontSize: 11 }} title={item.title}>{item.title}</Typography>
                      <Tooltip title={`Remove from this project${at ? " — its scenes go back to stock" : ""}`}>
                        <IconButton size="small" aria-label={`Remove ${item.title}`} onClick={() => onRemoveClip?.(item)} sx={{ p: 0.4 }}>
                          <DeleteOutlineIcon sx={{ fontSize: 16 }} />
                        </IconButton>
                      </Tooltip>
                      <UseClipButton name={item.title} sceneN={selected ? selected.index + 1 : null} inUse={inUse} disabled={!canUse} onUse={() => onUseMine(item)} />
                    </Stack>
                  </Box>
                )
              })}
            </Box>
          </>
        )}
      </Box>
    </Box>
  )
}

export default memo(MediaBin)
