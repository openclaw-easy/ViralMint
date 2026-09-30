// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
/**
 * AssetDrawer — one detail surface for every media type.
 *
 * Replaces three drawers (VideoDetailDrawer / AssetDetailDrawer /
 * DownloadedDetailDrawer) with one frame: provenance header, preview, tabs,
 * actions. What it does NOT do is re-implement their bodies. A generated video's
 * detail (edit title + script + platform metadata, regenerate thumbnail, export
 * per aspect, export bundle) and a download's detail (transcript, insights,
 * chapters, comments, re-analyze, remix, derivatives) are large, tested
 * surfaces; rebuilding them just to change the page's taxonomy would risk losing
 * features nobody asked to lose. So this drawer EMBEDS
 * [GeneratedDetail](frontend/src/components/videos/GeneratedDetail.jsx) and
 * [DownloadedDetail](frontend/src/components/videos/DownloadedDetail.jsx), and
 * supplies what neither had: lineage, and one consistent shell.
 *
 * Job outputs and music tracks never had a detail surface at all — they get the
 * facts list, a real player and the actions here.
 */
import { useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import {
  Box, Typography, Stack, Drawer, IconButton, Button, Tabs, Tab,
  Divider, Tooltip, CircularProgress, alpha, useTheme,
} from "@mui/material"
import CloseRoundedIcon from "@mui/icons-material/CloseRounded"
import DownloadRoundedIcon from "@mui/icons-material/DownloadRounded"
import DeleteOutlineRoundedIcon from "@mui/icons-material/DeleteOutlineRounded"
import NorthEastRoundedIcon from "@mui/icons-material/NorthEastRounded"
import SubdirectoryArrowRightRoundedIcon from "@mui/icons-material/SubdirectoryArrowRightRounded"
import http from "../../api/http"
import useAppStore from "../../store/appStore"
import GeneratedDetail from "../videos/GeneratedDetail"
import DownloadedDetail from "../videos/DownloadedDetail"
import {
  ORIGINS, originColor, fmtDayLong, fmtSize, waveformBars, MEDIA,
} from "./assetModel"
import { formatClock } from "../../utils/format"
import { OriginBadge } from "./SourceTree"

/** Which `/api/*` row backs an item. */
const DETAIL_SOURCE = {
  generated: { url: (id) => `/api/videos/${id}` },
  downloaded: { url: (id) => `/api/downloaded/${id}` },
}

// Wide enough for a 9:16 preview beside its facts without the page behind
// it becoming a sliver.
const DETAIL_DRAWER_WIDTH = 720

export default function AssetDrawer({
  item, open, onClose, ancestors = [], derivatives = [], onOpen, onOpenKey,
  onDeleted, onConfirmDelete, onUpload,
}) {
  const theme = useTheme()
  const navigate = useNavigate()
  const isDark = theme.palette.mode === "dark"
  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const [tab, setTab] = useState(0)
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const oc = item ? originColor(item.origin, isDark) : theme.palette.primary.main
  const embeds = item ? DETAIL_SOURCE[item.source] : null

  // Reset to the first tab when a different item opens — landing on "Lineage"
  // because that is where you were last is disorienting.
  useEffect(() => { setTab(0) }, [item?.key])

  // The index row is a projection; the embedded bodies want their own full row,
  // so fetch it when — and only when — one of them is going to render.
  useEffect(() => {
    if (!open || !item || !embeds) { setDetail(null); return }
    let alive = true
    setDetailLoading(true)
    http.get(embeds.url(item.id))
      .then(({ data }) => { if (alive) setDetail(data) })
      .catch(() => { if (alive) setDetail(null) })
      .finally(() => { if (alive) setDetailLoading(false) })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, item?.key])

  const facts = useMemo(() => {
    if (!item) return []
    const rows = [
      ["Kind", MEDIA[item.media]?.label],
      ["Origin", ORIGINS[item.origin]?.hint],
      ["Made by", item.producer],
      ["Added", fmtDayLong(item.created_at)],
      ["Duration", formatClock(item.duration_seconds)],
      ["Aspect", item.aspect],
      ["File", item.ext ? `.${item.ext}` : null],
      ["Size", fmtSize(item.size_mb)],
      ["Status", item.status ? item.status[0].toUpperCase() + item.status.slice(1) : null],
      ["Platform", item.platform],
      ["Genre", item.genre],
      ["Niche", item.niche],
      ["Hook score", item.hook_score != null ? `${item.hook_score} / 10` : null],
      ["Virality", item.virality_score != null ? `${item.virality_score} / 10` : null],
    ]
    return rows.filter(([, v]) => v != null && v !== "")
  }, [item])

  const hasLineage = ancestors.length > 0 || derivatives.length > 0

  const performDelete = async () => {
    if (!item) return
    const endpoints = {
      generated: () => http.delete(`/api/videos/${item.id}`),
      downloaded: () => http.delete(`/api/downloaded/${item.id}`),
      job: () => http.delete(`/api/library/asset/${item.id}`),
      music: () => http.delete(`/api/music/${encodeURIComponent(item.id)}`),
    }
    try {
      await endpoints[item.source]()
      showSnackbar("Deleted", "info")
      onDeleted?.(item)
      onClose?.()
    } catch (e) {
      showSnackbar(e.response?.data?.detail || "Could not delete that item", "error")
    }
  }

  // Every delete route confirms exactly once: the page supplies the same
  // confirm the tile menu uses, and only the unwrapped fallback (no page —
  // shouldn't happen in practice) deletes straight away.
  const handleDelete = () => {
    if (!item) return
    if (onConfirmDelete) onConfirmDelete(item, performDelete)
    else performDelete()
  }

  // "Use as Inspiration" on a downloaded source — the same hand-off the old
  // page made: Smart Video auto-drafts from `?source=` (useStudioPrefill).
  // Close first, or the drawer sits over the studio it just navigated to.
  const handleUseAsInspiration = (downloadedId) => {
    onClose?.()
    navigate(`/stock?source=${encodeURIComponent(downloadedId)}`)
  }

  return (
    <Drawer anchor="right" open={open} onClose={onClose}
      data-testid="asset-drawer"
      slotProps={{ paper: {
        sx: {
          // The embedded detail bodies were built for DETAIL_DRAWER_WIDTH; at
          // the narrower width this drawer started with, DownloadedDetail's
          // action row (Analyze / Quick Video / Remix / Clipper) clipped off the
          // right edge. Facts-only items don't need the room but don't suffer
          // from it either, so one width keeps the shell consistent.
          width: { xs: "100%", sm: embeds ? DETAIL_DRAWER_WIDTH : 520 },
          maxWidth: "100vw",
          borderLeft: 1, borderColor: "divider",
        },
      } }}>
      {item && (
        <Box sx={{ height: "100%", display: "flex", flexDirection: "column" }}>
          {/* Header */}
          <Box sx={{
            px: 2, pt: 2, pb: 1.5, position: "relative", flexShrink: 0,
            background: `linear-gradient(135deg, ${alpha(oc, isDark ? 0.16 : 0.09)} 0%, transparent 70%)`,
            borderBottom: 1, borderColor: "divider",
          }}>
            <Box sx={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 3, bgcolor: oc }} />
            <Stack direction="row" alignItems="flex-start" spacing={1}>
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <OriginBadge origin={item.origin} size="md" />
                <Typography sx={{ fontSize: "1.02rem", fontWeight: 700, letterSpacing: "-0.02em", mt: 0.5 }}>
                  {item.title}
                </Typography>
                <Typography sx={{ fontSize: "0.72rem", color: "text.secondary", mt: 0.25 }}>
                  {item.producer}
                  {item.duration_seconds != null && ` · ${formatClock(item.duration_seconds)}`}
                  {item.aspect && ` · ${item.aspect}`}
                  {` · ${fmtDayLong(item.created_at)}`}
                </Typography>
              </Box>
              <IconButton size="small" onClick={onClose} aria-label="Close">
                <CloseRoundedIcon fontSize="small" />
              </IconButton>
            </Stack>
          </Box>

          {/* Preview — skipped for embedded bodies, which bring their own player */}
          {!embeds && (
            <Box sx={{ px: 2, pt: 2, flexShrink: 0 }}>
              <Box sx={{
                borderRadius: "12px", overflow: "hidden", position: "relative",
                bgcolor: "background.subtle",
                // An image gets a real stage: at a fixed 200 px a 3:4 poster
                // rendered ~149 px wide with the drawer empty below it. The
                // stage takes the image's own height (min 200, max ~420) so a
                // landscape image doesn't float in blurred bands.
                ...(item.media === "image" && item.thumb_url
                  ? { minHeight: 200, maxHeight: "min(420px, 50vh)" }
                  : { height: 200 }),
                display: "flex", alignItems: "center", justifyContent: "center",
              }}>
                {item.media === "image" && item.thumb_url ? (
                  <>
                    <Box component="img" src={item.thumb_url} alt="" aria-hidden sx={{
                      position: "absolute", inset: -12,
                      width: "calc(100% + 24px)", height: "calc(100% + 24px)",
                      objectFit: "cover", filter: "blur(20px) brightness(0.8)",
                    }} />
                    {/* The file itself, not the 400 px poster — the stage is
                        now big enough to show the difference. */}
                    <Box component="img" src={item.stream_url || item.thumb_url} alt=""
                      decoding="async"
                      onError={(e) => { if (item.thumb_url && e.currentTarget.src.indexOf(item.thumb_url) === -1) e.currentTarget.src = item.thumb_url }}
                      sx={{ position: "relative", display: "block", maxWidth: "100%", maxHeight: "min(420px, 50vh)", objectFit: "contain" }} />
                  </>
                ) : item.media === "video" ? (
                  <Box component="video" src={item.stream_url} controls preload="metadata"
                    poster={item.thumb_url || undefined}
                    sx={{ width: "100%", height: "100%", objectFit: "contain", bgcolor: "#000" }} />
                ) : item.media === "audio" ? (
                  <>
                    <Stack direction="row" alignItems="center" justifyContent="center" spacing={0.5}
                      sx={{ width: "100%", height: "100%", px: 3, pb: 5 }}>
                      {waveformBars(item.key, 46).map((h, i) => (
                        <Box key={i} sx={{
                          width: 3, borderRadius: 2, height: `${Math.round(h * 55)}%`,
                          bgcolor: oc, opacity: 0.3 + h * 0.5,
                        }} />
                      ))}
                    </Stack>
                    {/* An <audio> element is a replaced element: it has an
                        intrinsic width, so left+right alone won't stretch it and
                        `width: auto` collapses it to 0. Set the width outright. */}
                    <Box component="audio" src={item.stream_url} controls
                      sx={{ position: "absolute", bottom: 10, left: 12, width: "calc(100% - 24px)" }} />
                  </>
                ) : (
                  <Typography sx={{ fontSize: "0.78rem", color: "text.disabled", px: 3, textAlign: "center" }}>
                    {item.ext === "zip"
                      ? "Archive — save a copy to open it"
                      : "Text file — save a copy to read it"}
                  </Typography>
                )}
              </Box>
            </Box>
          )}

          {/* Tabs */}
          <Box sx={{ px: 2, mt: 1.5, flexShrink: 0 }}>
            <Tabs value={tab} onChange={(_, v) => setTab(v)}
              sx={{
                minHeight: 36,
                "& .MuiTab-root": {
                  minHeight: 36, textTransform: "none", fontSize: "0.8rem",
                  fontWeight: 600, px: 1.5,
                },
              }}>
              <Tab label="Details" />
              <Tab label={`Lineage${hasLineage ? ` (${ancestors.length + derivatives.length})` : ""}`} />
            </Tabs>
            <Divider />
          </Box>

          {/* Body */}
          <Box sx={{ flex: 1, overflow: "auto", px: 2, py: 2 }}>
            {tab === 0 && (
              embeds ? (
                detailLoading ? (
                  <Stack alignItems="center" sx={{ py: 6 }}>
                    <CircularProgress size={22} thickness={5} />
                  </Stack>
                ) : detail ? (
                  item.source === "generated" ? (
                    <GeneratedDetail
                      video={detail}
                      onDelete={handleDelete}
                      onDetailUpdate={(updated) => setDetail(updated)}
                      // Publishing is this variant's own feature, and the
                      // embedded body is where it has always lived. The record
                      // the confirm dialog needs is the one already fetched
                      // here, so it rides along rather than being re-fetched.
                      onUpload={(id, platforms) => onUpload?.(id, platforms, detail)}
                    />
                  ) : (
                    <DownloadedDetail
                      video={detail}
                      onDetailUpdate={(updated) => setDetail(updated)}
                      onUseAsInspiration={handleUseAsInspiration}
                      // A derivatives row is not an index item (no media/origin/
                      // stream_url) — hand the KEY to the page, which resolves it
                      // through /api/library/lineage before opening.
                      onSelectDerivative={(deriv) => onOpenKey?.(`gv:${deriv.id}`)}
                    />
                  )
                ) : (
                  <Typography sx={{ fontSize: "0.84rem", color: "text.secondary" }}>
                    Couldn't load the full record. Everything the library knows about it is in the
                    header above.
                  </Typography>
                )
              ) : (
                <Stack spacing={0}>
                  {facts.map(([k, v]) => (
                    <Stack key={k} direction="row" spacing={2} sx={{
                      py: 0.85, borderBottom: 1, borderColor: "divider", alignItems: "baseline",
                    }}>
                      <Typography sx={{
                        width: 108, flexShrink: 0, fontSize: "0.7rem", fontWeight: 700,
                        letterSpacing: "0.05em", textTransform: "uppercase", color: "text.disabled",
                      }}>
                        {k}
                      </Typography>
                      <Typography sx={{ fontSize: "0.82rem", color: "text.primary" }}>{v}</Typography>
                    </Stack>
                  ))}
                </Stack>
              )
            )}

            {tab === 1 && (
              <Stack spacing={1.25}>
                {!hasLineage && (
                  <Typography sx={{ fontSize: "0.82rem", color: "text.secondary" }}>
                    Nothing links to this yet. Edits you make from it will show up here.
                  </Typography>
                )}
                {ancestors.length > 0 && (
                  <>
                    <SectionLabel>Made from</SectionLabel>
                    {ancestors.map((a) => (
                      <LineageRow key={a.key} item={a}
                        icon={<NorthEastRoundedIcon sx={{ fontSize: 14 }} />} onOpen={onOpen} />
                    ))}
                  </>
                )}
                {derivatives.length > 0 && (
                  <>
                    <SectionLabel>Used to make</SectionLabel>
                    {derivatives.map((c) => (
                      <LineageRow key={c.key} item={c}
                        icon={<SubdirectoryArrowRightRoundedIcon sx={{ fontSize: 14 }} />} onOpen={onOpen} />
                    ))}
                  </>
                )}
              </Stack>
            )}
          </Box>

          {/* Actions — the embedded bodies carry their own (export, edit,
              delete), so a second set here would duplicate them. */}
          {!embeds && (
            <Stack direction="row" spacing={1} sx={{
              p: 1.5, flexShrink: 0, borderTop: 1, borderColor: "divider",
              bgcolor: "background.subtle",
            }}>
              <Button variant="contained" size="small" startIcon={<DownloadRoundedIcon />}
                sx={{ flex: 1 }} href={item.stream_url} download>
                Save a copy
              </Button>
              <Tooltip title="Delete">
                <Button variant="outlined" size="small" color="error" sx={{ minWidth: 0, px: 1.25 }}
                  aria-label="Delete" onClick={handleDelete}>
                  <DeleteOutlineRoundedIcon fontSize="small" />
                </Button>
              </Tooltip>
            </Stack>
          )}
        </Box>
      )}
    </Drawer>
  )
}

function SectionLabel({ children }) {
  return (
    <Typography sx={{
      fontSize: "0.64rem", fontWeight: 700, letterSpacing: "0.09em",
      textTransform: "uppercase", color: "text.disabled", mt: 0.5,
    }}>
      {children}
    </Typography>
  )
}

function LineageRow({ item, icon, onOpen }) {
  const theme = useTheme()
  const oc = originColor(item.origin, theme.palette.mode === "dark")
  return (
    <Stack direction="row" spacing={1} alignItems="center"
      onClick={() => onOpen?.(item)}
      sx={{
        p: 1, borderRadius: "10px", cursor: "pointer", border: 1, borderColor: "divider",
        "&:hover": { bgcolor: "action.hover", borderColor: alpha(oc, 0.4) },
      }}>
      <Box sx={{ color: oc, display: "flex" }}>{icon}</Box>
      <Box sx={{
        width: 40, height: 24, borderRadius: "4px", overflow: "hidden", flexShrink: 0,
        bgcolor: alpha(oc, 0.12),
      }}>
        {item.thumb_url && (
          <Box component="img" src={item.thumb_url} alt=""
            sx={{ width: "100%", height: "100%", objectFit: "cover" }} />
        )}
      </Box>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Typography noWrap sx={{ fontSize: "0.78rem", fontWeight: 600 }}>{item.title}</Typography>
        <Typography sx={{ fontSize: "0.66rem", color: "text.secondary" }}>
          {ORIGINS[item.origin]?.label} · {item.producer}
        </Typography>
      </Box>
    </Stack>
  )
}
