// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
/**
 * Library — every file the user owns, on two orthogonal axes.
 *
 * Two orthogonal axes instead of the old five tabs: the media tabs say what a
 * file IS, the From chips say where it CAME FROM. See
 * [assetModel.js](frontend/src/components/librarynext/assetModel.js) for the
 * reasoning and [library_taxonomy.py](backend/services/library_taxonomy.py) for
 * the map both sides share.
 *
 * All data is live: `/api/library/items` (one faceted union over generated
 * videos, downloads, tool outputs and music tracks), `/api/library/families`
 * for the by-source view, `/api/library/lineage` for the drawer, and
 * `/api/jobs` for work in flight.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useNavigate, useSearchParams } from "react-router-dom"
import {
  Box, Typography, Stack, TextField, Button, Chip, IconButton,
  Tooltip, Menu, MenuItem, CircularProgress, alpha, useTheme,
  Dialog, DialogTitle, DialogContent, DialogActions,
} from "@mui/material"
import SearchIcon from "@mui/icons-material/Search"
import CloseRoundedIcon from "@mui/icons-material/CloseRounded"
import FileUploadRoundedIcon from "@mui/icons-material/FileUploadRounded"
import PhotoLibraryRoundedIcon from "@mui/icons-material/PhotoLibraryRounded"
import InventoryRoundedIcon from "@mui/icons-material/Inventory2Rounded"
import AutoAwesomeRoundedIcon from "@mui/icons-material/AutoAwesomeRounded"
import DeleteOutlineRoundedIcon from "@mui/icons-material/DeleteOutlineRounded"
import DownloadRoundedIcon from "@mui/icons-material/DownloadRounded"
import LightbulbRoundedIcon from "@mui/icons-material/LightbulbRounded"
import BoltRoundedIcon from "@mui/icons-material/BoltRounded"
import ErrorOutlineRoundedIcon from "@mui/icons-material/ErrorOutlineRounded"
import http from "../api/http"
import PageHero from "../components/PageHero"
import useDocumentTitle from "../hooks/useDocumentTitle"
import useAppStore from "../store/appStore"
import useLibraryIndex, { PAGE_SIZES, DEFAULT_PAGE_SIZE } from "../hooks/useLibraryIndex"
import useConfirm from "../hooks/useConfirm"
import LibraryPager from "../components/librarynext/LibraryPager"
import FacetBar from "../components/librarynext/FacetBar"
import AssetTile from "../components/librarynext/AssetTile"
import SourceTree from "../components/librarynext/SourceTree"
import AssetDrawer from "../components/librarynext/AssetDrawer"
import PendingTile from "../components/librarynext/PendingTile"
import { MEDIA, ORIGINS, originColor, originTextColor, activityFromJob } from "../components/librarynext/assetModel"

// NOTE: there is no "Use in…" hand-off here. Every tool page takes an UPLOAD —
// no endpoint in this variant accepts a library reference — so a button that
// sent you to a tool which then demanded the file again would be a promise the
// app can't keep. Adding `library_ref` inputs to the tool endpoints is what
// would earn that button.
// What /api/downloaded/import accepts — video or audio, since an imported
// podcast is as valid a source as an imported clip.
const IMPORT_ACCEPT = "video/*,audio/*,.mp4,.mov,.avi,.mkv,.webm,.mp3,.wav,.m4a,.aac,.flac"

// Reads as a sentence in the empty state: "no audio files that were …".
const ORIGIN_PHRASE = {
  created: "made in ViralMint",
  edited: "made from something you already own",
  imported: "brought in from outside",
}

// The old page's five tabs, translated into the new axes. Twenty-one links
// across pages and chat cards point at `?tab=…`, so they keep working: the
// param is consumed once on arrival and replaced with the filters it means.
//   scout → its own page   ·   jobs → the Activity panel
const LEGACY_TAB_FILTERS = {
  downloaded: { origin: "imported" },
  generated: { origin: "created" },
  audio: { media: "audio" },
  music: { media: "audio" },
}

/** Every key a family tree renders, so the page can dim what the current
 *  filters exclude without the tree needing to know about filters. */
function treeKeys(families, loners) {
  const keys = new Set()
  const walk = (nodes) => nodes.forEach((n) => { keys.add(n.item.key); walk(n.children) })
  families.forEach((f) => { keys.add(f.root.key); walk(f.children) })
  loners.forEach((l) => keys.add(l.key))
  return keys
}

export default function Library() {
  useDocumentTitle("Library")
  const theme = useTheme()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const showSnackbar = useAppStore((s) => s.showSnackbar)

  // Filters live in the URL so a view is linkable: chat cards, the old tab
  // links and a bookmark all land on the same shape without the page inventing
  // a second source of truth.
  const media = params.get("media") || "all"
  const origins = (params.get("origin") || "").split(",").filter(Boolean)
  const producer = params.get("tool") || "all"
  const sort = params.get("sort") || "newest"
  const view = params.get("view") === "tree" ? "tree" : "grid"
  const q = params.get("q") || ""
  const page = Math.max(1, parseInt(params.get("page") || "1", 10) || 1)
  const pageSize = PAGE_SIZES.includes(Number(params.get("per")))
    ? Number(params.get("per")) : DEFAULT_PAGE_SIZE
  const [dense, setDense] = useState(false)

  /** Write one filter, dropping it from the URL when it returns to default. */
  const setParam = useCallback((key, value, dflt) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      if (!value || value === dflt) next.delete(key)
      else next.set(key, value)
      return next
    }, { replace: true })
  }, [setParams])

  /** Any filter change resets to page 1 — page 7 of an old result set is a
   *  guaranteed empty grid, and "nothing matches" would read as a bug. */
  const setFilterParam = useCallback((key, value, dflt) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      if (!value || value === dflt) next.delete(key)
      else next.set(key, value)
      next.delete("page")
      return next
    }, { replace: true })
  }, [setParams])

  const setMedia = (v) => setFilterParam("media", v, "all")
  const setProducer = (v) => setFilterParam("tool", v, "all")
  const setSort = (v) => setFilterParam("sort", v, "newest")
  const setView = (v) => setFilterParam("view", v, "grid")
  const setQ = (v) => setFilterParam("q", v, "")
  const setPage = (v) => setParam("page", v > 1 ? String(v) : "", "")
  const setPageSize = (v) => setFilterParam("per", v === DEFAULT_PAGE_SIZE ? "" : String(v), "")
  // key → ITEM, not a Set of keys: selection survives page and filter changes,
  // so bulk actions must be able to resolve every selected item — resolving
  // against the current page silently dropped anything selected on another one.
  const [selected, setSelected] = useState(() => new Map())
  const [drawerItem, setDrawerItem] = useState(null)
  const [drawerLineage, setDrawerLineage] = useState({ ancestors: [], children: [] })
  const [showWhy, setShowWhy] = useState(true)
  const [moreMenu, setMoreMenu] = useState(null)
  const openActivity = useAppStore((s) => s.openActivity)
  const [importing, setImporting] = useState(false)
  const { confirm: openConfirm, ConfirmDialog } = useConfirm()

  const {
    items, total, libraryTotal, facets, loading, pageCount,
    families, loners, familyTotal, matchedKeys, familiesLoading,
    removeLocal, refresh,
  } = useLibraryIndex({ media, origins, producer, q, sort, view, page, pageSize })

  // Jobs are polled once in Layout (which also owns the Activity panel); this
  // page reads the same store rather than starting a second poll.
  const jobs = useAppStore((s) => s.jobs)
  const activity = useMemo(() => jobs.map(activityFromJob), [jobs])
  const runningCount = activity.filter((a) => a.state === "running").length

  // Work in flight sits in the grid it will land in. Only jobs the current
  // facets would admit are shown, so a filter never hides what you are waiting
  // for and never smuggles in something you filtered out. Capped at 8: past
  // that the grid is a progress dashboard, and Activity is the better surface.
  const pendingTiles = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return activity.filter((a) => {
      if (a.state !== "running") return false
      // A running scout / analyze / metadata pass never becomes a library
      // tile — promising a landing spot for it here would be a lie.
      if (!a.libraryBound) return false
      if (media !== "all" && a.media !== media) return false
      if (origins.length && !origins.includes(a.origin)) return false
      if (producer !== "all") return false
      if (needle && !a.label.toLowerCase().includes(needle)) return false
      return true
    }).slice(0, 8)
  }, [activity, media, origins, producer, q])

  // Failures are a batch, not a tile each: one strip, one trip to Activity.
  // Older than a day they stop being news and live in Activity only.
  const recentFailures = useMemo(() => {
    const dayAgo = Date.now() - 24 * 3600e3
    return activity.filter((a) => a.state === "failed" && new Date(a.at).getTime() >= dayAgo)
  }, [activity])

  const mediaCounts = useMemo(() => ({ all: facets.all, ...facets.media }), [facets])

  const dimmedIds = useMemo(() => {
    if (!matchedKeys) return new Set()
    const all = treeKeys(families, loners)
    return new Set([...all].filter((k) => !matchedKeys.has(k)))
  }, [families, loners, matchedKeys])

  const toggleOrigin = (k) => {
    const next = origins.includes(k) ? origins.filter((x) => x !== k) : [...origins, k]
    setFilterParam("origin", next.join(","), "")
  }

  const toggleSelect = (item) =>
    setSelected((prev) => {
      const next = new Map(prev)
      next.has(item.key) ? next.delete(item.key) : next.set(item.key, item)
      return next
    })

  const clearFilters = () => setParams(new URLSearchParams(), { replace: true })

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape" && selected.size) setSelected(new Map()) }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [selected.size])

  const scrollRef = useRef(null)
  useEffect(() => { scrollRef.current?.scrollTo({ top: 0 }) }, [page, pageSize])

  // A shrinking result set can strand `?page=` past the end (a delete emptied
  // the last page, or a hand-edited URL) — the pager would then highlight a
  // page the fetch disagrees with. Clamp instead of showing an empty grid.
  useEffect(() => {
    if (!loading && total > 0 && page > pageCount) setPage(Math.max(1, pageCount))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, total, page, pageCount])

  // When a running job finishes, its output belongs in this grid — but nothing
  // else refetches the index, so the pending tile would just vanish. One
  // coalesced refresh per batch of completions.
  const prevRunningRef = useRef(new Set())
  useEffect(() => {
    const running = new Set(activity.filter((a) => a.state === "running").map((a) => a.id))
    const finished = [...prevRunningRef.current].some((id) => !running.has(id))
    prevRunningRef.current = running
    if (finished) refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activity])

  // Legacy `?tab=` links, translated once on arrival.
  const legacyTab = params.get("tab")
  useEffect(() => {
    if (!legacyTab) return
    if (legacyTab === "scout") { navigate("/scout", { replace: true }); return }
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete("tab")
      for (const [k, v] of Object.entries(LEGACY_TAB_FILTERS[legacyTab] || {})) next.set(k, v)
      return next
    }, { replace: true })
    if (legacyTab === "jobs") openActivity()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [legacyTab])

  // Lineage is fetched per item rather than held for the whole library, because
  // the grid deliberately does not load the family tree.
  const openItem = useCallback(async (item) => {
    setDrawerItem(item)
    setDrawerLineage({ ancestors: [], children: [] })
    try {
      const { data } = await http.get("/api/library/lineage", { params: { key: item.key } })
      setDrawerLineage({ ancestors: data.ancestors || [], children: data.children || [] })
    } catch {
      /* An item can be indexed and then deleted from under us — the drawer
         still shows everything it already has. */
    }
  }, [])

  const openByKey = useCallback(async (key) => {
    const local = items.find((i) => i.key === key)
    if (local) { openItem(local); return }
    try {
      const { data } = await http.get("/api/library/lineage", { params: { key } })
      setDrawerItem(data.item)
      setDrawerLineage({ ancestors: data.ancestors || [], children: data.children || [] })
    } catch {
      showSnackbar("That item is no longer in your library", "info")
    }
  }, [items, openItem, showSnackbar])

  // `?open=<key>` opens one item directly. Activity lives in the sidebar now, so
  // "open what it made" has to be able to cross a route boundary to get here.
  const openParam = params.get("open")
  useEffect(() => {
    if (!openParam) return
    openByKey(openParam)
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete("open")
      return next
    }, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openParam])

  const handleImport = async (event) => {
    const file = event.target.files?.[0]
    if (!file) return
    event.target.value = ""          // so re-picking the same file fires again
    setImporting(true)
    try {
      const form = new FormData()
      form.append("file", file)
      form.append("title", file.name.replace(/\.[^.]+$/, ""))
      const { data } = await http.post("/api/downloaded/import", form, {
        headers: { "Content-Type": "multipart/form-data" },
      })
      showSnackbar(data.message || "Imported", "success")
      refresh()
    } catch (e) {
      showSnackbar(e.response?.data?.detail || "Import failed", "error")
    } finally {
      setImporting(false)
    }
  }

  /** Endpoint per store — there is no single delete route across all four. */
  const DELETE_BY_SOURCE = {
    generated: (it) => http.delete(`/api/videos/${it.id}`),
    downloaded: (it) => http.delete(`/api/downloaded/${it.id}`),
    job: (it) => http.delete(`/api/library/asset/${it.id}`),
    // No delete route for the music directory — see deleteSelected.
    music: null,
  }

  const deleteOne = async (item) => {
    const del = DELETE_BY_SOURCE[item.source]
    if (!del) {
      showSnackbar(
        "Music tracks live in storage/music/ — remove the file there", "info",
      )
      return
    }
    try {
      await del(item)
      removeLocal(item.key)
      refresh()  // facet counts and pageCount come from the server
      showSnackbar("Deleted", "success")
    } catch (e) {
      showSnackbar(e.response?.data?.detail || "Could not delete that item", "error")
    }
  }

  const confirmDeleteOne = (item) => openConfirm(
    "Delete this item?",
    `“${item.title}” is removed from disk. This cannot be undone.`,
    () => deleteOne(item),
  )

  /** Delete everything selected, grouped by the store each item lives in.
   *
   *  There is no single endpoint for this: job assets have a bulk route, and
   *  everything else is one call per item. Music tracks are files you dropped
   *  into storage/music/ yourself and have no delete route at all, so the bar
   *  says so rather than pretending. Partial failure is reported honestly
   *  rather than as a blanket success.
   */
  const deleteSelected = async () => {
    // The full selection, not the current page — the Map holds every item.
    const chosen = [...selected.values()]
    const byStore = {
      generated: chosen.filter((i) => i.source === "generated"),
      job: chosen.filter((i) => i.source === "job"),
      downloaded: chosen.filter((i) => i.source === "downloaded"),
      music: chosen.filter((i) => i.source === "music"),
    }
    let ok = 0
    const failures = []
    const succeeded = []
    // Per-group failure handling: one failing route must not skip the other
    // stores' deletions, and only what actually deleted leaves the grid.
    for (const it of byStore.generated) {
      try { await http.delete(`/api/videos/${it.id}`); ok++; succeeded.push(it) } catch { failures.push(it.title) }
    }
    if (byStore.job.length) {
      try {
        await http.post("/api/library/bulk-delete", { job_ids: byStore.job.map((i) => i.id) })
        ok += byStore.job.length
        succeeded.push(...byStore.job)
      } catch { failures.push(...byStore.job.map((i) => i.title)) }
    }
    for (const it of byStore.downloaded) {
      try { await http.delete(`/api/downloaded/${it.id}`); ok++; succeeded.push(it) } catch { failures.push(it.title) }
    }
    // Music tracks are yours on disk and there is no endpoint that removes
    // them. Counting them as failures is the honest read: the user asked and
    // it did not happen.
    failures.push(...byStore.music.map((i) => `${i.title} (music tracks are removed from storage/music/)`))
    succeeded.forEach((i) => removeLocal(i.key))
    setSelected(new Map())
    refresh()
    showSnackbar(
      failures.length
        ? `Deleted ${ok}; ${failures.length} could not be deleted`
        : `Deleted ${ok} item${ok === 1 ? "" : "s"}`,
      failures.length ? "warning" : "success",
    )
  }

  /** Publishing — this variant ships an uploader, and the Library is where a
   *  finished video is reviewed before it goes out. The confirm dialog shows
   *  the per-platform title and description the pipeline drafted, because
   *  "Upload" with no preview is a one-click irreversible publish.
   */
  const [uploadPreview, setUploadPreview] = useState(
    { open: false, videoId: null, platforms: [], video: null })

  const openUploadPreview = (id, platforms, video) =>
    setUploadPreview({ open: true, videoId: id, platforms, video })

  const doUpload = async () => {
    const { videoId, platforms } = uploadPreview
    setUploadPreview((prev) => ({ ...prev, open: false }))
    try {
      await http.post(`/api/videos/${videoId}/upload`, { platforms })
      showSnackbar(`Upload started for ${platforms.join(", ")}`, "success")
      refresh()
    } catch (e) {
      showSnackbar(e.response?.data?.detail || e.message, "error")
    }
  }

  const cancelJob = async (jobId) => {
    try {
      const { data } = await http.delete(`/api/jobs/${jobId}`)
      // See Layout.jsx: a running job's cancel is best-effort, and the user
      // needs to know that before they retry and end up with two of them.
      showSnackbar(
        data?.best_effort
          ? "Cancelling — the step already running will finish on its own"
          : "Job cancelled",
        "info",
      )
    } catch (e) {
      showSnackbar(e.response?.data?.detail || "Could not cancel that job", "error")
    }
  }

  const filtersActive =
    media !== "all" || origins.length > 0 || producer !== "all" || Boolean(q.trim())

  return (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <PageHero
        icon={<PhotoLibraryRoundedIcon sx={{ fontSize: 22 }} />}
        title="Library"
        subtitle={
          loading && !libraryTotal
            ? "Reading your library…"
            // Library-wide count only. The per-origin numbers live on the From
            // chips, where they are filter-scoped on purpose — printing them
            // here next to a library-wide total made the header contradict
            // itself as soon as you picked a media tab.
            : `${libraryTotal} items`
        }
        accentColor="#0097a7"
        actions={
          <Stack direction="row" spacing={1} alignItems="center">
            <TextField
              placeholder="Search library…"
              size="small"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              slotProps={{
                input: {
                  startAdornment: <SearchIcon sx={{ mr: 0.5, fontSize: 18, color: "text.secondary" }} />,
                },
              }}
              sx={{ width: { xs: 140, sm: 250 } }}
            />
            {/* A real file input behind the label — the first cut carried the
                button over without one, so clicking it did nothing. */}
            <Button size="small" variant="outlined" component="label"
              disabled={importing} startIcon={<FileUploadRoundedIcon />}>
              {importing ? "Importing…" : "Import"}
              <input type="file" hidden accept={IMPORT_ACCEPT} onChange={handleImport} />
            </Button>
            <Button size="small" variant="outlined" onClick={openActivity}
              startIcon={<BoltRoundedIcon sx={{ fontSize: 17 }} />}>
              Activity
              {(runningCount > 0 || recentFailures.length > 0) && (
                <Box component="span" sx={{
                  ml: 0.7, px: 0.6, borderRadius: "999px", fontSize: "0.68rem", fontWeight: 700,
                  bgcolor: runningCount > 0 ? "primary.main" : "error.main",
                  color: "primary.contrastText",
                }}>
                  {runningCount > 0 ? runningCount : recentFailures.length}
                </Box>
              )}
            </Button>
          </Stack>
        }
      />

      {/* Facets — a fixed flex sibling above the scroller. No backdrop-filter:
          nothing ever scrolls beneath this bar, so the blur had zero visual
          effect while still forcing the compositor to keep a backdrop
          texture (part of the 2026-08-18 scroll-jank fix). */}
      <Box sx={{
        flexShrink: 0, borderBottom: 1, borderColor: "divider",
        bgcolor: alpha(theme.palette.background.paper, 0.72),
      }}>
        <FacetBar
          media={media} onMediaChange={setMedia}
          origins={origins} onToggleOrigin={toggleOrigin}
          producer={producer} onProducerChange={setProducer} producerOptions={facets.producers}
          sort={sort} onSortChange={setSort}
          view={view} onViewChange={setView}
          dense={dense} onDenseChange={setDense}
          mediaCounts={mediaCounts} originCounts={facets.origin}
        />
      </Box>

      {/* Content */}
      <Box ref={scrollRef} sx={{
        flex: 1, overflow: "auto", px: { xs: 2, md: 3 },
        pt: 2.5, pb: selected.size ? 9 : 2.5,
      }}>
        {showWhy && (
          <Stack direction="row" spacing={1.25} alignItems="flex-start" sx={{
            mb: 2.5, px: 1.5, py: 1.1, borderRadius: "12px",
            border: 1, borderColor: alpha("#0097a7", 0.3),
            bgcolor: alpha("#0097a7", theme.palette.mode === "dark" ? 0.1 : 0.06),
          }}>
            <LightbulbRoundedIcon sx={{ fontSize: 17, color: "#0097a7", mt: "1px" }} />
            <Typography sx={{ flex: 1, fontSize: "0.78rem", color: "text.secondary", lineHeight: 1.65 }}>
              <strong>Two questions, two controls.</strong> The tabs say what a file <em>is</em>; the
              From chips say where it <em>came from</em>. A downloaded mp3 is Audio + Sources, a
              voice-over is Audio + Created, an enhanced podcast is Audio + Edited — all three live
              in Audio, and none of them needs a tab of its own. Switch to
              <strong> By source</strong> to see each download with everything you made from it.
              <br />
              Scout results are deliberately not here — a lead is not a file you own, so it has its
              own page. Jobs split in two: work in flight appears as a tile in the grid where it will
              land, and the full log lives in <strong>Activity</strong> (top right), reachable from
              every page instead of only this one.
            </Typography>
            <IconButton size="small" onClick={() => setShowWhy(false)} aria-label="Dismiss">
              <CloseRoundedIcon sx={{ fontSize: 15 }} />
            </IconButton>
          </Stack>
        )}

        {/* What the active origin is actually made of. "Imported 165" read as
            "I imported 165 files" — it was 156 downloaded from links, 8 uploaded
            tracks and one file from disk. The chips answer the axis; this answers
            "made of what", where the question actually gets asked. */}
        {origins.length === 1 && facets.producers.length > 1 && (
          <Typography sx={{ mb: 2, fontSize: "0.76rem", color: "text.secondary" }}>
            <Box component="span" sx={{ color: originTextColor(origins[0], theme.palette.mode === "dark"), fontWeight: 700 }}>
              {ORIGINS[origins[0]]?.label}
            </Box>
            {" is "}
            {facets.producers.slice(0, 5).map((p, i) => (
              <Box component="span" key={p.key}>
                {i > 0 ? " · " : ""}
                <strong>{p.count}</strong> {p.label.toLowerCase()}
              </Box>
            ))}
            {facets.producers.length > 5 && ` · +${facets.producers.length - 5} more tools`}
          </Typography>
        )}

        {recentFailures.length > 0 && (
          <Stack direction="row" spacing={1.25} alignItems="center" sx={{
            mb: 2.5, px: 1.5, py: 1, borderRadius: "12px",
            border: 1, borderColor: alpha(theme.palette.error.main, 0.4),
            bgcolor: alpha(theme.palette.error.main, theme.palette.mode === "dark" ? 0.1 : 0.05),
          }}>
            <ErrorOutlineRoundedIcon sx={{ fontSize: 18, color: "error.main" }} />
            <Typography sx={{ flex: 1, fontSize: "0.8rem", color: "text.secondary" }}>
              <strong>
                {recentFailures.length} {recentFailures.length === 1 ? "run" : "runs"} didn't finish
              </strong>{" "}
              in the last day — nothing was added to your library.
            </Typography>
            <Button size="small" variant="outlined" color="error"
              onClick={openActivity}>
              Review
            </Button>
          </Stack>
        )}

        {loading ? (
          <Stack alignItems="center" sx={{ py: 10 }}>
            <CircularProgress size={26} thickness={5} />
          </Stack>
        ) : view === "tree" ? (
          familiesLoading && families.length === 0 ? (
            <Stack alignItems="center" sx={{ py: 10 }}>
              <CircularProgress size={26} thickness={5} />
            </Stack>
          ) : (
            <>
              <SourceTree families={families} loners={loners}
                dimmedIds={dimmedIds} onOpen={openItem} />
              <LibraryPager
                page={page} pageCount={pageCount} pageSize={pageSize}
                onPage={setPage} onPageSize={setPageSize}
                shown={families.length} total={familyTotal} libraryTotal={libraryTotal}
                unit="families"
                filtersActive={filtersActive} onClearFilters={clearFilters}
              />
            </>
          )
        ) : total === 0 && pendingTiles.length === 0 ? (
          <Stack alignItems="center" spacing={1.5} sx={{ py: 10 }}>
            <InventoryRoundedIcon sx={{ fontSize: 40, color: "text.disabled" }} />
            <Typography sx={{ fontSize: "0.95rem", fontWeight: 600 }}>
              {filtersActive ? "Nothing matches those filters" : "Your library is empty"}
            </Typography>
            <Typography sx={{
              fontSize: "0.82rem", color: "text.secondary", textAlign: "center", maxWidth: 380,
            }}>
              {filtersActive
                ? (media !== "all" && origins.length === 1
                  ? `You have no ${MEDIA[media]?.plural} that were ${ORIGIN_PHRASE[origins[0]]}.`
                  : "Widen the media tab or the From chips to see more.")
                : "Renders, downloads and anything you edit collect here."}
            </Typography>
            {filtersActive
              ? <Button size="small" variant="outlined" onClick={clearFilters}>Clear filters</Button>
              : <Button size="small" variant="contained" onClick={() => navigate("/stock")}>Make a video</Button>}
          </Stack>
        ) : (
          <>
            <Box sx={{
              display: "grid", gap: dense ? 1.25 : 1.75,
              gridTemplateColumns: `repeat(auto-fill, minmax(${dense ? 168 : 202}px, 1fr))`,
            }}>
              {pendingTiles.map((job) => (
                <PendingTile key={job.id} job={job} dense={dense}
                  onCancel={() => cancelJob(job.id)} />
              ))}
              {items.map((item) => (
                <AssetTile
                  key={item.key}
                  item={item}
                  dense={dense}
                  selected={selected.has(item.key)}
                  anySelected={selected.size > 0}
                  onOpen={openItem}
                  onToggleSelect={toggleSelect}
                  onMore={(it, e) => setMoreMenu({
                    anchor: e.currentTarget.closest('[data-testid="asset-tile"]') || e.currentTarget,
                    item: it,
                  })}
                />
              ))}
            </Box>

            <LibraryPager
              page={page} pageCount={pageCount} pageSize={pageSize}
              onPage={setPage} onPageSize={setPageSize}
              shown={items.length} total={total} libraryTotal={libraryTotal}
              filtersActive={filtersActive} onClearFilters={clearFilters}
            />
          </>
        )}
      </Box>

      {/* Bulk bar */}
      {selected.size > 0 && (
        <Box sx={{
          position: "absolute", bottom: 20, left: "50%",
          zIndex: 20, px: 1.5, py: 1, borderRadius: "999px",
          display: "flex", alignItems: "center", gap: 1.25,
          bgcolor: "primary.main",
          color: "primary.contrastText",
          border: 1, borderColor: alpha("#fff", 0.25),
          boxShadow: (t) => `${t.customShadows?.xl}, ${t.customShadows?.glowStrong}`,
          // Slides up once, so the mode change is felt as well as seen.
          animation: "vm-bulkbar-in .22s cubic-bezier(0.34, 1.56, 0.64, 1)",
          "@keyframes vm-bulkbar-in": {
            from: { transform: "translate(-50%, 14px)", opacity: 0 },
            to: { transform: "translate(-50%, 0)", opacity: 1 },
          },
          transform: "translateX(-50%)",
          "@media (prefers-reduced-motion: reduce)": { animation: "none" },
          // The buttons live on a coloured ground now, so they inherit it rather
          // than staying brand-on-brand (invisible).
          "& .MuiButton-root": {
            color: "primary.contrastText",
            fontWeight: 700,
            "&:hover": { bgcolor: alpha("#fff", 0.16) },
          },
          "& .MuiIconButton-root": { color: "primary.contrastText" },
        }}>
          <Chip size="small" label={`${selected.size} selected`}
            sx={{
              fontWeight: 800, height: 24,
              bgcolor: alpha("#fff", 0.22), color: "primary.contrastText",
            }} />
          <Button size="small" startIcon={<DeleteOutlineRoundedIcon sx={{ fontSize: 16 }} />}
            onClick={() => openConfirm(
              `Delete ${selected.size} item${selected.size === 1 ? "" : "s"}?`,
              "The files are removed from disk. This cannot be undone.",
              deleteSelected,
              { confirmLabel: `Delete ${selected.size}` },
            )}>
            Delete
          </Button>
          <IconButton size="small" onClick={() => setSelected(new Map())} aria-label="Clear selection">
            <CloseRoundedIcon sx={{ fontSize: 16 }} />
          </IconButton>
        </Box>
      )}

      {/* The tile's ⋯ — the same actions the drawer offers, without opening it.
          It used to be a toast telling you to open the item, which is not an
          action. */}
      <Menu open={Boolean(moreMenu)} anchorEl={moreMenu?.anchor} onClose={() => setMoreMenu(null)}
        slotProps={{ paper: { sx: { minWidth: 190 } } }}>
        <MenuItem sx={{ fontSize: "0.84rem" }}
          onClick={() => { const it = moreMenu.item; setMoreMenu(null); openItem(it) }}>
          Open details
        </MenuItem>
        <MenuItem sx={{ fontSize: "0.84rem" }}
          component="a" href={moreMenu?.item?.stream_url} download
          onClick={() => setMoreMenu(null)}>
          Save a copy
        </MenuItem>
        <MenuItem sx={{ fontSize: "0.84rem", color: "error.main" }}
          onClick={() => { const it = moreMenu.item; setMoreMenu(null); confirmDeleteOne(it) }}>
          Delete
        </MenuItem>
      </Menu>

      <ConfirmDialog />

      {/* Publish confirm. An upload is irreversible and public, so the exact
          title and description that will go out are shown first — the same
          preview the previous page carried, kept rather than simplified. */}
      <Dialog open={uploadPreview.open} maxWidth="sm" fullWidth
        onClose={() => setUploadPreview((prev) => ({ ...prev, open: false }))}>
        <DialogTitle>
          Confirm upload to{" "}
          {uploadPreview.platforms.map((pl) => pl.charAt(0).toUpperCase() + pl.slice(1)).join(", ")}
        </DialogTitle>
        <DialogContent>
          {uploadPreview.video && (
            <Stack spacing={1.5} sx={{ pt: 1 }}>
              {uploadPreview.platforms.includes("youtube") && (
                <>
                  <Typography variant="caption" color="text.secondary">YouTube title</Typography>
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>
                    {uploadPreview.video.youtube_title || uploadPreview.video.title || "(untitled)"}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">YouTube description</Typography>
                  <Typography variant="body2" sx={{
                    whiteSpace: "pre-wrap", maxHeight: 120, overflow: "auto", color: "text.secondary",
                  }}>
                    {uploadPreview.video.youtube_description || "(no description)"}
                  </Typography>
                </>
              )}
              {uploadPreview.platforms.includes("tiktok") && (
                <>
                  <Typography variant="caption" color="text.secondary">TikTok title</Typography>
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>
                    {uploadPreview.video.tiktok_title || uploadPreview.video.title || "(untitled)"}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">TikTok description</Typography>
                  <Typography variant="body2" sx={{
                    whiteSpace: "pre-wrap", maxHeight: 120, overflow: "auto", color: "text.secondary",
                  }}>
                    {uploadPreview.video.tiktok_description || "(no description)"}
                  </Typography>
                </>
              )}
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setUploadPreview((prev) => ({ ...prev, open: false }))}>
            Cancel
          </Button>
          <Button variant="contained" onClick={doUpload}>Upload</Button>
        </DialogActions>
      </Dialog>

      <AssetDrawer
        item={drawerItem}
        open={Boolean(drawerItem)}
        onClose={() => setDrawerItem(null)}
        ancestors={drawerLineage.ancestors}
        derivatives={drawerLineage.children}
        onOpen={openItem}
        onOpenKey={openByKey}
        onDeleted={(deleted) => { removeLocal(deleted.key); refresh() }}
        onUpload={openUploadPreview}
        // Same confirm every other delete route gets — the drawer must not be
        // the one place a single click destroys a file.
        onConfirmDelete={(item, perform) => openConfirm(
          "Delete this item?",
          `“${item.title}” is removed from disk. This cannot be undone.`,
          perform,
        )}
      />
    </Box>
  )
}
