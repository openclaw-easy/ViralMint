// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { Fragment, memo, useEffect, useMemo, useRef, useState } from "react"
import {
  Accordion, AccordionDetails, AccordionSummary, Box, Button, Chip, Divider,
  IconButton, ListItemText, Menu, MenuItem, Slider, Stack, Switch,
  TextField, ToggleButton, ToggleButtonGroup, Tooltip, Typography, alpha,
} from "@mui/material"
import ExpandMoreIcon from "@mui/icons-material/ExpandMore"
import PlayArrowRoundedIcon from "@mui/icons-material/PlayArrowRounded"
import PauseRoundedIcon from "@mui/icons-material/PauseRounded"
import StopRoundedIcon from "@mui/icons-material/StopRounded"
import SwapHorizIcon from "@mui/icons-material/SwapHoriz"
import ContentCutIcon from "@mui/icons-material/ContentCut"
import CallMergeIcon from "@mui/icons-material/CallMerge"
import AddBoxOutlinedIcon from "@mui/icons-material/AddBoxOutlined"
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline"
import VisibilityOutlinedIcon from "@mui/icons-material/VisibilityOutlined"
import WarningAmberRoundedIcon from "@mui/icons-material/WarningAmberRounded"
import VideoLibraryOutlinedIcon from "@mui/icons-material/VideoLibraryOutlined"
import CloseIcon from "@mui/icons-material/Close"
import TuneRoundedIcon from "@mui/icons-material/TuneRounded"
import { useStudioRendering } from "./StudioRenderingContext"
import UseClipButton from "./UseClipButton"
import SceneAssist from "./SceneAssist"
import { onActivate } from "../../utils/a11y"
import {
  FIT_OPTIONS, HOOK_SECONDS, HOOK_TEXT_MAX, KIND_META, LINE_MAX, TRANSITIONS, VOICE_STATE,
  autoRevoices, autoTitle, isFixedVisual, isShortVisual, musicLabel,
  queryGradient, stockLabel, visualSourceLabel, voiceKindLabel,
  clipInUse,
} from "./studioModel"
import { mediaUrl, voiceUrl } from "./studioApi"
import { PROJECT_INITIAL, PROJECT_MIN } from "./editorLayout"
import useTtsVoices from "./useTtsVoices"
import useMusicLibrary from "../../hooks/useMusicLibrary"
import useVoicePreview from "../../hooks/useVoicePreview"
import useAppStore from "../../store/appStore"
import { VoiceRow } from "../create/SmartVideoPanels"
import { STYLES, StyleSwatch } from "../create/SmartVideoStyles"
import CaptionStylePicker from "../tools/CaptionStylePicker"
import { captionStyleLabel } from "../tools/captionOptions"

// The right column is the PROJECT panel: settings that apply to the
// whole video. The selected scene's panel (SceneTab, exported below) sits
// beside the player instead — see ScenePanel.jsx — and exists only while a
// scene is selected in the timeline (owner, 2026-09-16).

function Section({ title, right, children, sx }) {
  return (
    <Box sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: "divider", ...sx }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
        <Typography variant="overline" sx={{ fontWeight: 800, color: "text.secondary", lineHeight: 1 }}>{title}</Typography>
        {right}
      </Stack>
      {children}
    </Box>
  )
}

// ── Scene panel body ───────────────────────────────────────────────────────

export function SceneTab({
  scene, total, settings, findings,
  onCommitLine, onVoice, onApply, onPreview, onShowMedia, onSearchStock,
  onSplit, onMerge, onInsertAfter, onRemove, onPlayScene, isLast,
  sceneCount = 2, projectId, onFix, onNextClip, onClose, scenePlaying = false,
  onPausePlayback, onReplanScene,
}) {
  const [line, setLine] = useState(scene.line)
  useEffect(() => { setLine(scene.line) }, [scene.uid, scene.line])
  const trimmed = line.replace(/\s+/g, " ").trim()
  const dirty = trimmed !== scene.line
  const over = trimmed.length > LINE_MAX
  // Any real change is sent — an empty or over-long line comes back as an
  // error saying why, instead of being dropped without a word.
  const commit = () => { if (dirty) onCommitLine(scene.uid, trimmed) }

  const v = scene.visual || {}
  const kind = v.kind || "stock"
  const generating = v.state === "generating"
  const [replaceAnchor, setReplaceAnchor] = useState(null)
  const rendering = useStudioRendering()

  const provider = settings.voice?.provider
  const voiceState = scene.voice?.state || "none"
  const hasLine = !!scene.line.trim()
  const short = isShortVisual(scene)
  const kindColor = (t) => (KIND_META[kind] || KIND_META.stock).color(t)
  const owned = !!v.asset?.asset_id && kind === "user_clip"
  const thumb = kind === "stock" ? v.asset?.thumbnail : owned ? mediaUrl(projectId, v.asset.asset_id, { poster: true }) : null

  const takeAudio = useRef(null)
  useEffect(() => () => takeAudio.current?.pause(), [])
  const playLine = () => {
    const key = scene.voice?.take_key
    if (!key) return
    // Stop the sequence player first: its own voice + music elements keep
    // going otherwise, and "Play take" put a second take on top of them.
    onPausePlayback?.()
    takeAudio.current?.pause()
    takeAudio.current = new Audio(voiceUrl(key))
    takeAudio.current.play().catch(() => {})
  }

  const bindCandidate = (candidate) => onApply([{
    op: "bind_visual", uid: scene.uid, visual: { kind: "stock", candidate },
  }], `Scene ${scene.index + 1} now uses that clip`)
  // Back to automatic stock: the scene's best candidate, unpinned (D29).
  const backToStock = () => onApply([{ op: "clear_visual", uid: scene.uid }])
  const previewThisVisual = () => onPreview(owned
    ? { url: mediaUrl(projectId, v.asset.asset_id), image: v.asset.media === "image", label: v.asset.title || visualSourceLabel(v) }
    : kind === "stock" && v.asset?.provider
      ? { candidate: v.asset, label: stockLabel(v.asset) }
      : null)

  return (
    <Box>
      <Box sx={{ px: 2, pt: 1.5, pb: 1.25, borderBottom: 1, borderColor: "divider" }}>
        <Stack direction="row" alignItems="center" spacing={1}>
          <Typography variant="subtitle1" sx={{ fontWeight: 800 }}>Scene {scene.index + 1}</Typography>
          <Chip size="small" label={`${scene.dur.toFixed(1)}s${scene.estimated ? " est." : ""}`} sx={{ height: 20, fontSize: 11 }} />
          <Chip size="small" label={(KIND_META[kind] || {}).label}
            sx={(t) => ({ height: 20, fontSize: 11, fontWeight: 700, bgcolor: alpha(kindColor(t), 0.18) })} />
          <Box sx={{ flex: 1 }} />
          <Tooltip title={scenePlaying ? "Pause" : "Play this scene"}>
            <IconButton size="small" aria-label={scenePlaying ? "Pause this scene" : "Play this scene"} aria-pressed={scenePlaying}
              onClick={() => onPlayScene(scene.uid)}>
              {scenePlaying ? <PauseRoundedIcon /> : <PlayArrowRoundedIcon />}
            </IconButton>
          </Tooltip>
          {onClose && (
            <Tooltip title="Close (Esc)">
              <IconButton size="small" aria-label="Close scene" onClick={onClose}><CloseIcon fontSize="small" /></IconButton>
            </Tooltip>
          )}
        </Stack>
        <Typography variant="caption" sx={{ color: "text.secondary" }}>
          Starts at {scene.start.toFixed(1)}s of {total.toFixed(1)}s
        </Typography>
      </Box>

      <Section title="Line">
        <TextField
          fullWidth multiline minRows={3} maxRows={8} size="small"
          value={line} onChange={(e) => setLine(e.target.value)} onBlur={commit}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); commit() } }}
          error={over || (dirty && !trimmed)}
          placeholder="What this scene says — it's spoken and captioned"
          helperText={over
            ? `${trimmed.length}/${LINE_MAX} — split this scene first`
            : !trimmed ? (dirty ? "A scene needs a line — delete the scene instead" : "Write this scene's line — it isn't voiced or rendered until you do")
              : dirty ? "⌘↵ or click away to apply — the voice will be re-recorded" : `${trimmed.length}/${LINE_MAX}`}
          slotProps={{ input: { sx: { fontSize: "0.88rem", lineHeight: 1.5 } }, htmlInput: { "aria-label": "Scene line" } }}
        />
        <SceneAssist
          projectId={projectId} scene={scene} isFirst={scene.index === 0} disabled={generating}
          onUseLine={(text) => { setLine(text); onCommitLine(scene.uid, text) }}
          onSearch={(q) => onSearchStock?.(q)}
          onReplanScene={kind === "stock" && !v.pinned ? onReplanScene : undefined}
        />
      </Section>

      <Section
        title="Voice"
        right={<Chip size="small" color={VOICE_STATE[voiceState].tone === "default" ? "default" : VOICE_STATE[voiceState].tone}
          variant={voiceState === "ready" ? "filled" : "outlined"} label={VOICE_STATE[voiceState].label} sx={{ height: 20, fontSize: 11 }} />}
      >
        <Stack direction="row" spacing={1} alignItems="center" useFlexGap flexWrap="wrap">
          <Button size="small" variant="outlined" startIcon={<PlayArrowRoundedIcon />}
            disabled={voiceState !== "ready"} onClick={playLine}
            sx={{ textTransform: "none", fontWeight: 600 }}>
            Play voice{scene.voice?.duration ? ` · ${scene.voice.duration.toFixed(1)}s` : ""}
          </Button>
          {voiceState !== "ready" && voiceState !== "generating" && (
            <Button size="small" variant="outlined" disabled={!hasLine || rendering} onClick={() => onVoice([scene.uid])}
              sx={{ textTransform: "none", fontWeight: 700, whiteSpace: "nowrap" }}>
              {voiceState === "stale" ? "Re-voice" : "Voice this scene"}
            </Button>
          )}
        </Stack>
        <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mt: 0.75 }}>
          {settings.voice?.label || settings.voice?.voice} · {voiceKindLabel(provider)}.
          {voiceState === "stale" ? " The line or voice changed, so this take no longer matches it." : ""}
          {voiceState === "ready" ? " Edit the line or change the voice to re-record." : ""}
          {voiceState === "failed" ? ` Recording failed${scene.voice?.error ? ` — ${scene.voice.error}` : ""}.` : ""}
        </Typography>
      </Section>

      <Section
        title="Visual"
        right={
          <Stack direction="row" spacing={0.5}>
            {kind === "stock" && (v.alternates?.length || 0) >= 2 && (
              <Tooltip title="Cycle to the next clip the planner found (N)">
                <Button size="small" aria-label="Next clip (N)" disabled={generating} onClick={() => onNextClip?.(scene.uid)} sx={{ textTransform: "none", fontWeight: 700 }}>
                  Next clip (N)
                </Button>
              </Tooltip>
            )}
            <Button size="small" endIcon={<SwapHorizIcon />} disabled={generating} onClick={(e) => setReplaceAnchor(e.currentTarget)}
              sx={{ textTransform: "none", fontWeight: 700 }}>
              Replace
            </Button>
          </Stack>
        }
      >
        {kind === "stock" && !v.pinned && v.planned_for && v.planned_for !== scene.line && (
          <Box data-testid="visual-line-changed" sx={(t) => ({ mb: 1, p: 1, borderRadius: 1.5, bgcolor: alpha(t.palette.warning.main, 0.1) })}>
            <Typography variant="caption" sx={{ display: "block" }}>
              The line changed since this clip was picked for it.
            </Typography>
            {onReplanScene && (
              <Button size="small" onClick={() => onReplanScene(scene.uid)} sx={{ textTransform: "none", fontWeight: 700, px: 0.5, minWidth: 0 }}>
                Re-plan this scene
              </Button>
            )}
          </Box>
        )}
        <Stack direction="row" spacing={1.25} alignItems="center">
          <Box
            role="button" tabIndex={0} aria-label="Preview this scene's visual"
            onClick={previewThisVisual}
            onKeyDown={onActivate(previewThisVisual)}
            sx={(t) => ({
              width: 96, height: 60, borderRadius: 1, flexShrink: 0, position: "relative", overflow: "hidden",
              cursor: "pointer", display: "grid", placeItems: "center",
              background: thumb ? `center / cover no-repeat url("${thumb}")` : queryGradient(v.asset?.query || v.query),
              outline: `2px solid ${alpha(kindColor(t), 0.7)}`, outlineOffset: -2,
            })}
          >
            <VisibilityOutlinedIcon sx={{ color: "#fff", fontSize: 20, opacity: 0.9 }} />
          </Box>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="body2" sx={{ fontWeight: 700 }} noWrap>{visualSourceLabel(v)}</Typography>
            <Typography variant="caption" sx={{ color: "text.secondary" }}>
              {kind === "stock" && `${v.asset?.creator || "Stock"} · ${v.asset?.native_duration ? `${v.asset.native_duration}s clip` : ""}`}
              {kind === "user_clip" && (v.asset?.media === "image" ? "Your image" : "Your clip")}
            </Typography>
          </Box>
        </Stack>

        <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mt: 1.25 }}>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="caption" sx={{ fontWeight: 600, display: "block" }}>Keep this visual</Typography>
            <Typography variant="caption" sx={{ color: "text.secondary", display: "block", lineHeight: 1.3 }}>
              {kind === "user_clip" ? "Your clips always stay where you put them."
                : v.pinned ? "Re-plan and Shuffle won't change this scene." : "Re-plan and Shuffle may swap this scene's clip."}
            </Typography>
          </Box>
          <Switch
            size="small" slotProps={{ input: { "aria-label": "Keep this visual when re-planning" } }}
            checked={isFixedVisual(scene)} disabled={kind === "user_clip" || generating}
            onChange={(e) => onApply([{ op: "set_visual_fields", uid: scene.uid, fields: { pinned: e.target.checked } }])}
          />
        </Stack>

        {(kind === "stock" || kind === "user_clip") && (
          <Box sx={{ mt: 1.25 }}>
            <Stack direction="row" alignItems="center" justifyContent="space-between">
              <Typography variant="caption" sx={{ color: "text.secondary", fontWeight: 600 }}>When the clip is shorter than the line</Typography>
              <ToggleButtonGroup
                size="small" exclusive value={v.fit || "loop"}
                onChange={(_, val) => val && onApply([{ op: "set_visual_fields", uid: scene.uid, fields: { fit: val } }])}
                sx={{ "& .MuiToggleButton-root": { py: 0.1, px: 1, fontSize: "0.7rem", textTransform: "none" } }}
              >
                {FIT_OPTIONS.map((f) => <ToggleButton key={f.value} value={f.value} title={f.hint}>{f.label}</ToggleButton>)}
              </ToggleButtonGroup>
            </Stack>
            {short && (
              <Stack direction="row" spacing={0.5} alignItems="center" sx={{ mt: 0.5, color: "warning.main" }}>
                <WarningAmberRoundedIcon sx={{ fontSize: 15 }} />
                <Typography variant="caption">
                  This clip is {(scene.dur - v.asset.native_duration).toFixed(1)}s shorter than the line.
                </Typography>
              </Stack>
            )}
          </Box>
        )}

        {!!v.alternates?.length && kind === "stock" && (
          <Box sx={{ mt: 1.5 }}>
            <Typography variant="caption" sx={{ color: "text.secondary", fontWeight: 600, display: "block", mb: 0.75 }}>
              Other clips found for this line — click to preview
            </Typography>
            <Box sx={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 0.75 }}>
              {v.alternates.map((alt) => {
                const current = clipInUse(scene, alt)
                const label = stockLabel(alt)
                return (
                  <Box key={`${alt.provider}:${alt.provider_id}`} sx={{ minWidth: 0 }}>
                    <Box
                      role="button" tabIndex={0} aria-label={`Preview ${label}`}
                      onClick={() => onPreview({ candidate: alt, label, useAs: { kind: "stock", candidate: alt } })}
                      onKeyDown={onActivate(() => onPreview({ candidate: alt, label, useAs: { kind: "stock", candidate: alt } }))}
                      sx={{
                        height: 52, borderRadius: 1, cursor: "pointer", display: "grid", placeItems: "center",
                        background: alt.thumbnail ? `center / cover no-repeat url("${alt.thumbnail}")` : queryGradient(alt.provider_id),
                        border: 2, borderColor: current ? "primary.main" : "transparent",
                      }}
                    >
                      <PlayArrowRoundedIcon sx={{ color: "#fff", fontSize: 18, opacity: 0.9, filter: "drop-shadow(0 1px 2px rgba(0,0,0,.6))" }} />
                    </Box>
                    <Typography sx={{ fontSize: 10.5, my: 0.25 }} noWrap title={label}>{label}</Typography>
                    <UseClipButton fullWidth name={label} sceneN={scene.index + 1} inUse={current} onUse={() => bindCandidate(alt)} />
                  </Box>
                )
              })}
            </Box>
          </Box>
        )}

        <Menu anchorEl={replaceAnchor} open={!!replaceAnchor} onClose={() => setReplaceAnchor(null)}>
          <MenuItem onClick={() => { setReplaceAnchor(null); onSearchStock(v.query || v.asset?.query || "") }}>
            <ListItemText primary="Search stock footage…" secondary="Pexels · Pixabay" />
          </MenuItem>
          <MenuItem onClick={() => { setReplaceAnchor(null); onShowMedia?.() }}>
            <ListItemText primary="Your clips…" secondary="opens the Media tab for this scene" />
          </MenuItem>
          {kind !== "stock" && <Divider />}
          {kind !== "stock" && (
            <MenuItem onClick={() => { setReplaceAnchor(null); backToStock() }}>
              <ListItemText primary="Back to stock footage" secondary="your clip stays in the Media tab" />
            </MenuItem>
          )}
        </Menu>
      </Section>

      <Section title="Arrange">
        <Box sx={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 0.75 }}>
          <Button size="small" variant="outlined" startIcon={<ContentCutIcon />} onClick={() => onSplit(scene.uid)} sx={{ textTransform: "none" }}>Split (S)</Button>
          <Button size="small" variant="outlined" startIcon={<CallMergeIcon />} disabled={isLast} onClick={() => onMerge(scene.uid)} sx={{ textTransform: "none" }}>Merge next (M)</Button>
          <Button size="small" variant="outlined" startIcon={<AddBoxOutlinedIcon />} onClick={() => onInsertAfter(scene.uid)} sx={{ textTransform: "none" }}>Insert after</Button>
          <Button size="small" variant="outlined" color="error" startIcon={<DeleteOutlineIcon />} disabled={sceneCount <= 1} onClick={() => onRemove(scene.uid)} sx={{ textTransform: "none" }}>Delete (Del)</Button>
        </Box>
      </Section>

      {findings.length > 0 && (
        <Section title="Notes">
          <Stack spacing={0.75}>
            {findings.map((f, i) => <FindingRow key={i} finding={f} onFix={onFix} />)}
          </Stack>
        </Section>
      )}
    </Box>
  )
}

// A critic note with its fix on the same line (plain edits only).
function FindingRow({ finding: f, onFix }) {
  return (
    <Stack direction="row" alignItems="flex-start" spacing={1}>
      <Typography variant="caption" sx={{ flex: 1, color: f.severity === "high" ? "warning.main" : "text.secondary" }}>
        • {f.message}
      </Typography>
      {f.fix && onFix && (
        <Button size="small" variant="outlined" onClick={() => onFix(f)}
          sx={{ textTransform: "none", fontWeight: 700, py: 0, px: 1, fontSize: 11, whiteSpace: "nowrap", flexShrink: 0 }}>
          {f.fix.label}
        </Button>
      )}
    </Stack>
  )
}

// ── Project tab ────────────────────────────────────────────────────────────

function Group({ title, summary, defaultExpanded = false, children }) {
  return (
    <Accordion
      disableGutters elevation={0} square defaultExpanded={defaultExpanded}
      sx={{ bgcolor: "transparent", borderBottom: 1, borderColor: "divider", "&:before": { display: "none" } }}
    >
      <AccordionSummary expandIcon={<ExpandMoreIcon fontSize="small" />}
        sx={{ minHeight: 44, px: 2, "& .MuiAccordionSummary-content": { my: 0.75, alignItems: "center", gap: 1, minWidth: 0 } }}>
        <Typography variant="body2" sx={{ fontWeight: 700 }}>{title}</Typography>
        <Typography variant="caption" sx={{ color: "text.secondary", minWidth: 0 }} noWrap>{summary}</Typography>
      </AccordionSummary>
      <AccordionDetails sx={{ px: 2, pt: 0, pb: 2 }}>{children}</AccordionDetails>
    </Accordion>
  )
}

function VoiceGroup({ settings, onApply, onVoiceAll, scenes }) {
  const { voices, error } = useTtsVoices()
  const showSnackbar = useAppStore((st) => st.showSnackbar)
  const [filter, setFilter] = useState("")
  const preview = useVoicePreview({ onError: (m) => showSnackbar(m, "error") })
  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase()
    return !q ? voices : voices.filter((v) => `${v.label} ${v.tagline} ${v.id} ${v.country}`.toLowerCase().includes(q))
  }, [voices, filter])
  const current = settings.voice || {}
  const unvoiced = scenes.filter((s) => s.voice?.state !== "ready" && s.voice?.state !== "generating" && s.line.trim())
  const rendering = useStudioRendering()

  return (
    <Group title="Voice" summary={`${current.label || current.voice} · ${voiceKindLabel(current.provider)}`} defaultExpanded>
      {unvoiced.length > 0 && (
        <Stack direction="row" alignItems="center" spacing={1} sx={(t) => ({ mb: 1.25, p: 1, borderRadius: 1, bgcolor: alpha(t.palette.info.main, 0.08) })}>
          <Typography variant="caption" sx={{ flex: 1 }}>{unvoiced.length} scene{unvoiced.length === 1 ? "" : "s"} not voiced yet</Typography>
          <Button size="small" variant="outlined" disabled={rendering} onClick={() => onVoiceAll()}
            sx={{ textTransform: "none", fontWeight: 700, whiteSpace: "nowrap" }}>
            Voice all
          </Button>
        </Stack>
      )}
      <TextField
        size="small" fullWidth value={filter} onChange={(e) => setFilter(e.target.value)}
        placeholder="Filter — name, language or country"
        slotProps={{ htmlInput: { "aria-label": "Filter voices" } }}
        sx={{ mb: 0.75, "& .MuiOutlinedInput-root": { fontSize: "0.8rem", height: 32 } }}
      />
      {error && <Typography variant="caption" color="error">Couldn't load voices.</Typography>}
      <Box sx={{ maxHeight: 250, overflowY: "auto", display: "flex", flexDirection: "column", gap: 0.25 }}>
        {shown.map((voice, i) => (
          <Fragment key={`${voice.provider}:${voice.id}`}>
            {voice.provider !== shown[i - 1]?.provider && (
              <Typography variant="caption" sx={{ color: "text.secondary", fontWeight: 700, px: 0.5, pt: i ? 0.75 : 0 }}>
                {voice.provider === "openai_tts" ? "OpenAI TTS · your API key" : "Edge TTS"}
              </Typography>
            )}
            <VoiceRow
              voice={voice}
              selected={current.voice === voice.id}
              isPlayingPreview={preview.playing === voice.id || preview.loading === voice.id}
              onPreview={(v) => preview.play(v.provider, v.id)}
              onClick={() => {
                if (current.voice === voice.id && current.provider === voice.provider) return
                onApply([{ op: "set_setting", path: "voice", value: { provider: voice.provider, voice: voice.id, label: voice.label } }],
                  null, { revoice: autoRevoices(voice.provider) })
              }}
            />
          </Fragment>
        ))}
      </Box>
      <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mt: 1 }}>
        Changing the voice re-records every scene. Edge voices do it automatically; an OpenAI voice records when you press Voice all (it uses your OpenAI API key). Takes already recorded are kept, so switching back is instant.
      </Typography>
    </Group>
  )
}

function MusicGroup({ settings, onApply, onPausePlayback }) {
  const { tracks, loading } = useMusicLibrary()
  const music = settings.music || {}
  const [playing, setPlaying] = useState(null)
  const audioRef = useRef(null)
  const stop = () => { audioRef.current?.pause(); audioRef.current = null; setPlaying(null) }
  useEffect(() => () => stop(), [])
  const toggle = (track) => {
    if (playing === track.filename) { stop(); return }
    onPausePlayback?.()      // same collision as the scene's "Play take"
    stop()
    const a = new Audio(track.stream_url)
    a.volume = 0.6
    audioRef.current = a
    setPlaying(track.filename)
    a.onended = stop
    a.play().catch(stop)
  }
  const setMusic = (value) => onApply([{ op: "set_setting", path: "music", value }])

  return (
    <Group
      title="Music"
      summary={musicLabel(music)}
    >
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
        <Typography variant="caption" sx={{ fontWeight: 600 }}>Background music</Typography>
        <Switch size="small" checked={!!music.enabled} onChange={(e) => setMusic({ enabled: e.target.checked })}
          inputProps={{ "aria-label": "Background music" }} />
      </Stack>
      <Box sx={{ opacity: music.enabled ? 1 : 0.45, pointerEvents: music.enabled ? "auto" : "none" }}>
        <Box sx={{ maxHeight: 180, overflowY: "auto", border: 1, borderColor: "divider", borderRadius: 1 }}>
          {!loading && tracks.length === 0 && (
            <Typography variant="caption" sx={{ display: "block", p: 1.25, color: "text.secondary" }}>
              Your music library is empty — add tracks to it first.
            </Typography>
          )}
          {tracks.map((tr) => {
            const sel = music.track === tr.filename
            return (
              <Stack key={tr.filename} direction="row" alignItems="center" spacing={0.5}
                sx={(t) => ({ px: 0.5, py: 0.25, bgcolor: sel ? alpha(t.palette.primary.main, 0.1) : "transparent", cursor: "pointer", "&:hover": { bgcolor: alpha(t.palette.text.primary, 0.05) } })}
                onClick={() => setMusic({ source: "library", track: tr.filename, track_name: tr.display_name, genre: tr.genre })}
              >
                <IconButton size="small" onClick={(e) => { e.stopPropagation(); toggle(tr) }} aria-label={`Preview ${tr.display_name}`}>
                  {playing === tr.filename ? <StopRoundedIcon fontSize="small" /> : <PlayArrowRoundedIcon fontSize="small" />}
                </IconButton>
                <Typography variant="body2" sx={{ flex: 1, minWidth: 0, fontSize: "0.8rem", fontWeight: sel ? 700 : 500 }} noWrap>{tr.display_name}</Typography>
                <Typography variant="caption" sx={{ color: "text.disabled", pr: 1 }}>{tr.genre}</Typography>
              </Stack>
            )
          })}
        </Box>
        <Box sx={{ mt: 1.5 }}>
          <LiveSlider label={(v) => `Volume under the voice · ${v} dB`} min={-30} max={0} step={1}
            value={music.volume_db ?? -14} onCommit={(v) => setMusic({ volume_db: v })} />
          <LiveSlider label={(v) => `Start the track at · ${v}s`} min={0} max={90} step={1}
            value={music.offset_s || 0} onCommit={(v) => setMusic({ offset_s: v })} />
        </Box>
      </Box>
    </Group>
  )
}

// A slider whose label follows the drag; the op is sent once, on release.
function LiveSlider({ label, value, min, max, step, onCommit }) {
  const [v, setV] = useState(value)
  useEffect(() => { setV(value) }, [value])
  return (
    <>
      <Typography variant="caption" sx={{ color: "text.secondary", fontWeight: 600 }}>{label(v)}</Typography>
      <Slider size="small" min={min} max={max} step={step} value={v}
        onChange={(_, x) => setV(x)} onChangeCommitted={(_, x) => { if (x !== value) onCommit(x) }} />
    </>
  )
}

// Your own clips live in the media bin's Media tab (add, place, remove — one
// home, owner 2026-09-16). The Project panel only says how many and links there.
function ClipsSummary({ mine, scenes, onShowMedia }) {
  const inUse = new Set(scenes.filter((sc) => sc.visual?.kind === "user_clip").map((sc) => sc.visual.asset?.asset_id))
  const used = mine.filter((it) => inUse.has(it.asset_id)).length
  return (
    <Stack direction="row" alignItems="center" spacing={1} sx={{ px: 2, minHeight: 44, borderBottom: 1, borderColor: "divider" }}>
      <Typography variant="body2" sx={{ fontWeight: 700 }}>Your clips</Typography>
      <Typography variant="caption" sx={{ color: "text.secondary", flex: 1, minWidth: 0 }} noWrap>
        {mine.length ? `${mine.length} clip${mine.length === 1 ? "" : "s"} · ${used} in use` : "none yet"}
      </Typography>
      <Button size="small" startIcon={<VideoLibraryOutlinedIcon />} onClick={onShowMedia} sx={{ textTransform: "none", fontWeight: 700, whiteSpace: "nowrap" }}>
        {mine.length ? "Manage in Media" : "Add in Media"}
      </Button>
    </Stack>
  )
}

function ProjectTab({
  project, timeline, onApply, onVoiceAll, mine, onShowMedia,
  onReplan, onShuffle, onPausePlayback,
}) {
  const s = project.doc.settings
  const style = STYLES.find((x) => x.id === s.visual_style) || STYLES[0]
  return (
    <Box>

      <Group title="Look" summary={`${style.label} · ${project.doc.aspect_ratio}`} defaultExpanded>
        <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(72px, 1fr))", gap: 0.5, justifyItems: "center" }}>
          {STYLES.map((st) => (
            <StyleSwatch key={st.id} style={st} mini selected={s.visual_style === st.id}
              onClick={() => onApply([{ op: "set_setting", path: "visual_style", value: st.id }])} />
          ))}
        </Box>
        <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mt: 1 }}>
          Sets the colour grade of every scene.
        </Typography>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mt: 1.25 }} useFlexGap flexWrap="wrap">
          <Button size="small" variant="outlined" onClick={onShuffle} sx={{ textTransform: "none", fontWeight: 700, flexShrink: 0 }}>
            Shuffle footage
          </Button>
          <Button size="small" variant="outlined" onClick={onReplan} sx={{ textTransform: "none", fontWeight: 700, flexShrink: 0 }}>
            Re-plan visuals
          </Button>
          <Typography variant="caption" sx={{ color: "text.secondary" }}>
            Shuffle moves every scene to its next clip; Re-plan searches again. Scenes you chose to keep never move.
          </Typography>
        </Stack>
      </Group>

      <VoiceGroup settings={s} onApply={onApply} onVoiceAll={onVoiceAll} scenes={timeline.scenes} />

      <Group title="Captions" summary={s.captions?.enabled ? captionStyleLabel(s.captions.style) : "off"}>
        <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
          <Typography variant="caption" sx={{ fontWeight: 600 }}>Burn captions into the video</Typography>
          <Switch size="small" checked={!!s.captions?.enabled} inputProps={{ "aria-label": "Burn captions into the video" }}
            onChange={(e) => onApply([{ op: "set_setting", path: "captions.enabled", value: e.target.checked }])} />
        </Stack>
        <Box sx={{ opacity: s.captions?.enabled ? 1 : 0.45, pointerEvents: s.captions?.enabled ? "auto" : "none" }}>
          <CaptionStylePicker variant="compact" value={s.captions?.style}
            onChange={(v) => onApply([{ op: "set_setting", path: "captions.style", value: v }])} />
          <HookTitleControl captions={s.captions} firstLine={timeline.scenes.find((sc) => sc.line)?.line} onApply={onApply} />
        </Box>
      </Group>

      <MusicGroup settings={s} onApply={onApply} onPausePlayback={onPausePlayback} />

      {/* The everyday choices above; what a first video rarely needs below. */}
      <Typography variant="overline" sx={{ display: "block", px: 0.25, pt: 1.5, pb: 0.25, color: "text.secondary", fontWeight: 800, letterSpacing: 0.6 }}>
        More options
      </Typography>
      <ClipsSummary mine={mine} scenes={timeline.scenes} onShowMedia={onShowMedia} />

      <Group title="Transitions" summary={(TRANSITIONS.find((t) => t.value === s.transition) || TRANSITIONS[0]).label}>
        <Stack direction="row" gap={0.5} flexWrap="wrap">
          {TRANSITIONS.map((t) => (
            <Chip key={t.value} label={t.label} size="small"
              variant={s.transition === t.value ? "filled" : "outlined"} color={s.transition === t.value ? "primary" : "default"}
              onClick={() => onApply([{ op: "set_setting", path: "transition", value: t.value }])} />
          ))}
        </Stack>
      </Group>

      <Group title="Pacing" summary={`${(s.tail_gap_s ?? 0.15).toFixed(2)}s between lines`}>
        <LiveSlider label={(v) => `Pause after each line · ${Number(v).toFixed(2)}s`} min={0} max={0.6} step={0.05}
          value={s.tail_gap_s ?? 0.15} onCommit={(v) => onApply([{ op: "set_setting", path: "tail_gap_s", value: v }])} />
      </Group>

      <FinishingGroup post={s.post} onApply={onApply} />
    </Box>
  )
}

// Finishing steps applied at render, the same ones every Smart Video render
// uses. Each follows Settings unless this project chooses.
const FINISHING = [
  { key: "auto_zoom", label: "Auto-zoom on key words" },
  { key: "sfx", label: "Sound effects" },
]
const TRISTATE = [{ value: "settings", label: "Default" }, { value: "on", label: "On" }, { value: "off", label: "Off" }]

export function FinishingGroup({ post, onApply }) {
  const p = post || {}
  const toState = (v) => (v === true ? "on" : v === false ? "off" : "settings")
  const fromState = (v) => (v === "on" ? true : v === "off" ? false : null)
  const chosen = FINISHING.filter((f) => p[f.key] != null).length + (p.caption_emoji != null ? 1 : 0)
  return (
    <Group title="Finishing" summary={chosen ? `${chosen} set for this video` : "from Settings"}>
      <Stack spacing={1}>
        {FINISHING.map((f) => (
          <Stack key={f.key} direction="row" alignItems="center" spacing={1}>
            <Typography variant="caption" sx={{ flex: 1, minWidth: 0 }} noWrap>{f.label}</Typography>
            <ToggleButtonGroup size="small" exclusive value={toState(p[f.key])} aria-label={f.label}
              onChange={(_, v) => v && onApply([{ op: "set_setting", path: "post", value: { [f.key]: fromState(v) } }])}
              sx={{ "& .MuiToggleButton-root": { textTransform: "none", fontSize: "0.7rem", py: 0.25, px: 1 } }}>
              {TRISTATE.map((t) => <ToggleButton key={t.value} value={t.value}>{t.label}</ToggleButton>)}
            </ToggleButtonGroup>
          </Stack>
        ))}
        <Stack direction="row" alignItems="center" useFlexGap flexWrap="wrap" sx={{ columnGap: 1, rowGap: 0.5 }}>
          <Typography variant="caption" sx={{ flex: "1 0 auto" }} noWrap>Caption emoji</Typography>
          <ToggleButtonGroup size="small" exclusive value={p.caption_emoji ?? "settings"} aria-label="Caption emoji"
            onChange={(_, v) => v && onApply([{ op: "set_setting", path: "post", value: { caption_emoji: v === "settings" ? null : v } }])}
            sx={{ "& .MuiToggleButton-root": { textTransform: "none", fontSize: "0.7rem", py: 0.25, px: 1 } }}>
            {["settings", "none", "moderate", "heavy"].map((v) => (
              <ToggleButton key={v} value={v}>{v === "settings" ? "Settings" : v[0].toUpperCase() + v.slice(1)}</ToggleButton>
            ))}
          </ToggleButtonGroup>
        </Stack>
        <Typography variant="caption" sx={{ color: "text.secondary" }}>
          Applied when you render. "Settings" follows your app Settings.
        </Typography>
      </Stack>
    </Group>
  )
}

// ── Inspector ──────────────────────────────────────────────────────────────

// 360 by default, 340–1000 (the panel was too broad next to the player), drag
// / arrows / double-click reset via the shared hook. The rendered width comes
// from the editor's row budget (editorLayout.js).
export const INSPECTOR_WIDTH = { storageKey: "vm_studio_inspector_w", initial: PROJECT_INITIAL, min: PROJECT_MIN, max: 1000, label: "the project panel" }

function Inspector({
  project, timeline, findings = [],
  onApply, onVoiceAll, onShowMedia,
  mine = [],
  onReplan, onFix, onShuffle, drawer = null, width, resize, onPausePlayback,
  folded = false, drawerW = 420, open = false, onOpenChange,
}) {
  const { dragging, handleProps } = resize
  // `folded` (editorLayout.js, a row too narrow for three columns): the column
  // is a slim rail, and the panel opens as a drawer over the row's right edge
  // — shown while `open`, or while a selected scene is in it (`drawer`).
  if (folded) {
    const shown = open || !!drawer
    return (
      <Box sx={{
        flex: `0 0 ${width}px`, minWidth: 0, position: "relative", minHeight: 0,
        borderLeft: 1, borderColor: "divider", bgcolor: "background.paper",
      }}>
        <Tooltip title={shown ? "Hide the project panel" : "Project settings"} placement="left">
          <Box
            role="button" tabIndex={0} aria-label="Project panel" aria-expanded={shown}
            onClick={() => onOpenChange?.(!shown)} onKeyDown={onActivate(() => onOpenChange?.(!shown))}
            sx={{
              height: "100%", display: "flex", flexDirection: "column", alignItems: "center", gap: 1, pt: 1.25,
              cursor: "pointer", color: shown ? "primary.main" : "text.secondary",
              "&:hover": { bgcolor: "action.hover" },
              "&:focus-visible": { outline: "2px solid", outlineColor: "primary.main", outlineOffset: -2 },
            }}
          >
            <TuneRoundedIcon fontSize="small" />
            <Typography sx={{ writingMode: "vertical-rl", transform: "rotate(180deg)", fontSize: "0.75rem", fontWeight: 800, letterSpacing: "0.03em", userSelect: "none" }}>
              Project
            </Typography>
            {findings.length > 0 && (
              <Box aria-label={`${findings.length} notes`} sx={(t) => ({
                minWidth: 18, height: 18, px: 0.5, borderRadius: 9, fontSize: 10.5, fontWeight: 800,
                display: "grid", placeItems: "center", color: t.palette.warning.contrastText, bgcolor: "warning.main",
              })}>{findings.length}</Box>
            )}
          </Box>
        </Tooltip>
        {shown && (
          <Box
            data-testid="project-drawer"
            sx={(t) => ({
              position: "absolute", top: 0, bottom: 0, right: "100%", width: drawerW, zIndex: 6,
              display: "flex", flexDirection: "column", minHeight: 0,
              bgcolor: "background.paper", borderLeft: 1, borderColor: "divider",
              boxShadow: t.palette.mode === "dark" ? "-16px 0 40px rgba(0,0,0,0.55)" : "-16px 0 40px rgba(30,30,60,0.16)",
            })}
          >
            {drawer}
            <Stack direction="row" alignItems="center" sx={{ pl: 2, pr: 0.75, minHeight: 40, flexShrink: 0, borderBottom: 1, borderColor: "divider" }}>
              <Typography component="h2" variant="body2" sx={{ fontWeight: 700, flex: 1 }}>Project</Typography>
              <IconButton size="small" aria-label="Close project panel" onClick={() => onOpenChange?.(false)}>
                <CloseIcon fontSize="small" />
              </IconButton>
            </Stack>
            <ProjectBody
              findings={findings} onFix={onFix} project={project} timeline={timeline}
              onApply={onApply} onVoiceAll={onVoiceAll}
              mine={mine} onShowMedia={onShowMedia}
              onReplan={onReplan} onShuffle={onShuffle} onPausePlayback={onPausePlayback}
            />
          </Box>
        )}
      </Box>
    )
  }
  // `width` is what the editor's row budget allows (editorLayout.js): the
  // saved width when there's room, less when the player and scene panel need
  // it — the saved preference is never rewritten.
  return (
    <Box sx={{
      flex: `0 0 ${width}px`, minWidth: 0,
      position: "relative", display: "flex", flexDirection: "column",
      borderLeft: 1, borderColor: "divider", bgcolor: "background.paper", minHeight: 0,
    }}>
      {drawer}
      <Box
        {...handleProps}
        // Arrow keys resize here; don't let the editor's window handler also step scenes.
        onKeyDown={(e) => { handleProps.onKeyDown(e); if (e.key.startsWith("Arrow")) e.stopPropagation() }}
        sx={(t) => ({
          position: "absolute", left: -3, top: 0, bottom: 0, width: 6, zIndex: 2, cursor: "col-resize", touchAction: "none",
          bgcolor: dragging ? alpha(t.palette.primary.main, 0.5) : "transparent",
          transition: "background-color .12s",
          "&:hover, &:focus-visible": { bgcolor: alpha(t.palette.primary.main, 0.35), outline: "none" },
        })}
      />
      <Typography
        component="h2" variant="body2"
        sx={{ px: 2, minHeight: 40, display: "flex", alignItems: "center", flexShrink: 0, fontWeight: 700, borderBottom: 1, borderColor: "divider" }}
      >
        Project
      </Typography>
      <ProjectBody
        findings={findings} onFix={onFix} project={project} timeline={timeline}
        onApply={onApply} onVoiceAll={onVoiceAll}
        mine={mine} onShowMedia={onShowMedia}
        onReplan={onReplan} onShuffle={onShuffle} onPausePlayback={onPausePlayback}
      />
    </Box>
  )
}

// The Project panel's scrolling body — the same in the column and the drawer.
function ProjectBody({
  findings, onFix, project, timeline, onApply, onVoiceAll,
  mine, onShowMedia, onReplan, onShuffle, onPausePlayback,
}) {
  return (
      <Box sx={{ flex: 1, minHeight: 0, overflowY: "auto" }} role="region" aria-label="Project settings">
        {findings.length > 0 && (
          <Box sx={(t) => ({ m: 1.5, p: 1.25, borderRadius: 1.5, bgcolor: alpha(t.palette.warning.main, 0.08), border: 1, borderColor: alpha(t.palette.warning.main, 0.25) })}>
            <Typography variant="caption" sx={{ fontWeight: 800, display: "block", mb: 0.5 }}>Notes on this video</Typography>
            <Stack spacing={0.5}>
              {findings.slice(0, 2).map((f, i) => <FindingRow key={i} finding={f} onFix={onFix} />)}
            </Stack>
            {findings.length > 2 && (
              <Typography variant="caption" sx={{ display: "block", color: "text.disabled", mt: 0.25 }}>
                + {findings.length - 2} more — select a scene to see its notes
              </Typography>
            )}
          </Box>
        )}
        <ProjectTab
          project={project} timeline={timeline}
          onApply={onApply} onVoiceAll={onVoiceAll}
          mine={mine} onShowMedia={onShowMedia}
          onReplan={onReplan} onShuffle={onShuffle} onPausePlayback={onPausePlayback}
        />
      </Box>
  )
}

// The opening title burned over the first seconds (studio_doc.hook_title).
// Blank text = the first line; typing is committed on blur, not per key.
function HookTitleControl({ captions, firstLine, onApply }) {
  const [draft, setDraft] = useState(captions?.hook_text || "")
  useEffect(() => { setDraft(captions?.hook_text || "") }, [captions?.hook_text])
  const on = !!captions?.hook
  const commit = () => {
    const next = draft.trim() || null
    if (next !== (captions?.hook_text || null)) onApply([{ op: "set_setting", path: "captions.hook_text", value: next }])
  }
  return (
    <Box sx={{ mt: 1.25 }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between">
        <Box>
          <Typography variant="caption" sx={{ fontWeight: 600, display: "block" }}>Opening title</Typography>
          <Typography variant="caption" sx={{ color: "text.secondary", display: "block", lineHeight: 1.3 }}>
            Big text over the first {HOOK_SECONDS} seconds — the scroll-stopper
          </Typography>
        </Box>
        <Switch size="small" checked={on} inputProps={{ "aria-label": "Opening title" }}
          onChange={(e) => onApply([{ op: "set_setting", path: "captions.hook", value: e.target.checked }])} />
      </Stack>
      {on && (
        <TextField
          size="small" fullWidth sx={{ mt: 0.75 }} value={draft}
          placeholder={firstLine ? `From your first line: “${autoTitle(firstLine)}”` : "Your opening title"}
          inputProps={{ maxLength: HOOK_TEXT_MAX, "aria-label": "Opening title text" }}
          onChange={(e) => setDraft(e.target.value)} onBlur={commit}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit() } }}
        />
      )}
    </Box>
  )
}

export default memo(Inspector)
