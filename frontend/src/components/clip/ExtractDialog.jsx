// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { useState, useEffect, useRef } from "react"
import {
  Box, Typography, Stack, Chip, IconButton, Button,
  TextField, Tooltip, MenuItem, FormControlLabel, Checkbox, Switch,
  Dialog, DialogTitle, DialogContent, DialogActions, Collapse,
} from "@mui/material"
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesomeOutlined"
import ContentCutIcon from "@mui/icons-material/ContentCut"
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined"
import TuneIcon from "@mui/icons-material/TuneOutlined"
import ExpandMoreIcon from "@mui/icons-material/ExpandMore"
import SubtitlesOutlinedIcon from "@mui/icons-material/SubtitlesOutlined"
import GraphicEqOutlinedIcon from "@mui/icons-material/GraphicEqOutlined"
import CropPortraitOutlinedIcon from "@mui/icons-material/CropPortraitOutlined"
import RecordVoiceOverOutlinedIcon from "@mui/icons-material/RecordVoiceOverOutlined"
import { formatTime } from "./clipFormat"
import { CAPTION_STYLES } from "../tools/captionOptions"
import { clipSettingsPayload } from "./useClipSettings"
import { autoCutEstimate } from "./autoClipCount"

/* ── Auto-cut ──────────────────────────────────────────────────
   The express lane: trust the AI, cut now, don't review.

   This dialog used to be the ONLY way to cut anything, and carried a
   "My ranges" tab where you typed timestamps at a video you couldn't see.
   The cutting bench (components/clip/bench/) replaced that outright, so
   the tab is gone and this surface has exactly one job — the one the bench
   deliberately can't do:

     * any number of clips (the bench caps at _MANUAL_MAX_RANGES, because
       every proposal it shows has to survive the round trip back through
       manual mode to be cut)
     * cut immediately, with no proposal step

   Everything that applies per-clip — captions, emoji, silence, vertical,
   transcription — is NOT owned here. It comes from `useClipSettings`, held
   once by ClipStudio and shared with the bench, because two copies of the
   same four controls is how a caption style chosen in one place quietly
   failed to apply in the other.

   To hand-pick ranges, or to see the AI's picks before they become files,
   use the bench — "Ask AI" in its Ranges rail is the reviewable half of
   this same search.
*/

/* ── Extract Clips Dialog ──────────────────────────────────── */

const WHISPER_QUALITIES = [
  { value: "fast", label: "Fast (base)", desc: "~30s per 5min video" },
  { value: "balanced", label: "Balanced (small)", desc: "~90s per 5min video" },
  { value: "accurate", label: "Accurate (medium)", desc: "~3min per 5min video" },
  { value: "best", label: "Best (large-v3)", desc: "~8min per 5min video" },
]

// Style vocab comes from the shared captionOptions list (10 styles, guarded
// by tests/test_caption_styles_parity.py) — this dialog kept its own literal
// copy once and drifted to 7, hiding the urban/warm/mono pack on /clips.
// "none" (captions off) is the Captions row's SWITCH state, not a chip:
// clip extraction can skip burn-in entirely and that's the default
// (caption_service.captions_disabled handles "none" upstream).
const CAPTION_STYLE_OPTIONS = CAPTION_STYLES.map((s) => ({ v: s.value, label: s.label }))
const EMOJI_STYLE_OPTIONS = [
  { v: "none", label: "Off" },
  { v: "minimal", label: "Minimal" },
  { v: "moderate", label: "Moderate" },
  { v: "heavy", label: "Heavy" },
]

/* ── Settings-group rows ──────────────────────────────────────
   The four shared concerns (captions / pacing / aspect / transcription)
   render as one outlined group of compact rows — icon, label, current
   state on the right, switch or chevron. The row IS the summary; the
   controls only exist while a row is open. This is what keeps the
   dialog scroll-free by default: the tallest section (11 style chips +
   emoji) collapses to a 44px row until captions are switched on. */

function SettingRow({ icon, label, sublabel, summary, control, onRowClick, expanded, children, divider }) {
  return (
    <Box sx={{ borderTop: divider ? "1px solid" : "none", borderColor: "divider" }}>
      <Stack
        direction="row" alignItems="center" spacing={1.25}
        onClick={onRowClick}
        sx={{
          px: 1.5, py: 1, minHeight: 44,
          cursor: onRowClick ? "pointer" : "default",
          userSelect: "none",
          "&:hover": onRowClick ? { bgcolor: "action.hover" } : undefined,
          transition: "background-color .12s ease",
        }}
      >
        <Box sx={{ display: "flex", color: "text.secondary", "& svg": { fontSize: 19 } }}>{icon}</Box>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="body2" sx={{ fontWeight: 600, lineHeight: 1.25 }}>{label}</Typography>
          {sublabel && (
            <Typography variant="caption" sx={{ color: "text.secondary", display: "block", lineHeight: 1.3 }}>
              {sublabel}
            </Typography>
          )}
        </Box>
        {summary && (
          <Typography variant="caption" sx={{ color: "text.secondary", flexShrink: 0, fontWeight: 500 }}>
            {summary}
          </Typography>
        )}
        {control}
      </Stack>
      {children != null && (
        <Collapse in={expanded} timeout={180}>
          <Box sx={{ px: 1.5, pb: 1.5, pt: 0.25 }}>{children}</Box>
        </Collapse>
      )}
    </Box>
  )
}

// Visual-only switch: the whole row is the click target (single handler,
// no double-toggle), the switch just renders the state.
function RowSwitch({ checked }) {
  return <Switch checked={checked} size="small" sx={{ pointerEvents: "none", flexShrink: 0 }} />
}

export default function ExtractDialog({ open, onClose, video, onExtract, settings, onSettings }) {
  const hasSegments = !!video?.has_transcript_segments
  // AI targeting only. Everything per-clip lives in `settings`, owned by
  // ClipStudio and shared with the bench.
  const [ai, setAi] = useState({
    min_duration: null, max_duration: null, max_clips: null,
    user_query: "", target_platform: "", genre: "",
  })
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [transcriptOpen, setTranscriptOpen] = useState(false)
  // Remember the last picked style so toggling captions off and on again
  // restores the user's choice instead of resetting to viral.
  const lastStyle = useRef("viral")

  // Reset the per-source intent when the video changes — a stale "find
  // Lionel Messi goals" on a cooking podcast is just confusing. The shared
  // per-clip settings deliberately DO persist: those are habits.
  useEffect(() => {
    setAi({ min_duration: null, max_duration: null, max_clips: null,
            user_query: "", target_platform: "", genre: "" })
    setAdvancedOpen(false)
    setTranscriptOpen(false)
  }, [video?.id])

  if (!video) return null

  const set = (patch) => onSettings(patch)
  const transcribeEnabled = !hasSegments || settings.force_retranscribe
  const captionsOn = settings.caption_style !== "none"
  const durationError = ai.min_duration && ai.max_duration && ai.max_duration - ai.min_duration < 1
  const canSubmit = !durationError

  // Say what the button will do before it does it. This is the surface that
  // can produce the most: a blank "Clips (max)" means ~1 clip per 30s, so a
  // 22-minute source silently means 43 renders — 43 caption burns, and a
  // Library page to weed afterwards. The button said "Extract now" and named
  // no figure at all.
  //
  // "up to", and meant literally: max_clips is the ceiling the runner is
  // given, and it scales down when the video has less quality material. The
  // real count can be lower than this and never higher, which is the only
  // direction this may be wrong.
  const estimate = autoCutEstimate({
    durationSeconds: video.duration_seconds,
    requested: ai.max_clips,
    // A "Min (s)" of 60 means at most duration/60 clips fit, whatever the
    // count field says — quote what can actually be produced.
    minDurationSeconds: ai.min_duration,
  })

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <AutoAwesomeIcon color="secondary" /> Auto-cut clips
        <Tooltip title="Reads the transcript, picks the most viral moments and cuts them straight away — no review step. To see the picks on a timeline and adjust them before cutting, use Ask AI on the bench." arrow>
          <IconButton size="small" sx={{ ml: "auto", color: "text.secondary" }}>
            <InfoOutlinedIcon sx={{ fontSize: 18 }} />
          </IconButton>
        </Tooltip>
      </DialogTitle>
      <DialogContent>
        <Typography variant="body2" sx={{ color: "text.secondary", mb: 0.5 }}>
          From: <strong>{video.title || "Untitled"}</strong> ({formatTime(video.duration_seconds)})
        </Typography>
        <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mb: 2 }}>
          Cuts as soon as you press the button — no review step.
        </Typography>

        <Stack spacing={2.5}>
          {/* ── Layout contract (2026-08-01 reorganization) ──
              1. Mode-specific body FIRST — what to cut (ranges | AI targeting).
              2. "Style & polish" ONCE, shared — captions / emoji / silence /
                 vertical apply identically in both modes. They used to render
                 per-tab (buried under Advanced in AI mode, inline in manual),
                 so the same control sat at a different place and prominence
                 depending on the tab.
              3. "Transcription" LAST — plumbing with correct defaults; the
                 least-touched section shouldn't be the first thing shown. */}

          {/* Clip length & count — the two knobs that shape what the AI hunts
              for. Count is the MAX; the backend auto-scales down when the
              content doesn't support that many quality clips. */}
          <Box>
            <Typography variant="caption" sx={{ fontWeight: 600, mb: 0.5, display: "block" }}>
              Clip length & count (leave empty for auto)
            </Typography>
            <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap" useFlexGap>
              <TextField label="Min (s)" type="number" size="small" sx={{ width: 90 }}
                value={ai.min_duration || ""}
                error={!!durationError}
                slotProps={{ htmlInput: { min: 10, max: 120 } }}
                onChange={e => setAi(p => ({ ...p, min_duration: parseInt(e.target.value) || null }))} />
              <Typography variant="body2" sx={{ color: "text.secondary" }}>to</Typography>
              <TextField label="Max (s)" type="number" size="small" sx={{ width: 90 }}
                value={ai.max_duration || ""}
                error={!!durationError}
                slotProps={{ htmlInput: { min: 15, max: 180 } }}
                onChange={e => setAi(p => ({ ...p, max_duration: parseInt(e.target.value) || null }))} />
              <Box sx={{ flex: 1 }} />
              <TextField label="Clips (max)" type="number" size="small" sx={{ width: 110 }}
                value={ai.max_clips || ""}
                slotProps={{ htmlInput: { min: 1, max: 99 } }}
                onChange={e => setAi(p => ({ ...p, max_clips: parseInt(e.target.value) || null }))} />
            </Stack>
            <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.5, display: "block" }}>
              {ai.max_clips
                ? `Up to ${estimate.clips} clip${estimate.clips === 1 ? "" : "s"}, 15–60s each — fewer if the video has less quality material.`
                : video.duration_seconds
                  ? `Blank means ~1 clip per 30s of content: up to ${estimate.clips} for this ${Math.round(video.duration_seconds / 60)}-minute video. Fewer if it has less quality material.`
                  /* Duration unknown (an import ffprobe could not read): the
                     backend falls back to a flat 5, so say 5 rather than
                     inventing a "0-minute video". */
                  : `Blank means ~1 clip per 30s of content — up to ${estimate.clips} here, since this video's length is unknown.`}
            </Typography>
            {durationError && (
              <Typography variant="caption" sx={{ color: "error.main", mt: 0.5, display: "block" }}>
                Max must be at least 1 second greater than Min
              </Typography>
            )}
          </Box>

          {/* Advanced-options expander — collapses the 3 AI-targeting knobs so
              the dialog opens compact. All defaults are sensible; the user only
              has to touch these for niche cases (genre bias, custom query,
              platform bias). */}
          <Box>
            <Button
              variant="text"
              startIcon={<TuneIcon sx={{ fontSize: 18 }} />}
              endIcon={<ExpandMoreIcon sx={{ fontSize: 18, transform: advancedOpen ? "rotate(180deg)" : "rotate(0deg)", transition: "transform .15s ease" }} />}
              onClick={() => setAdvancedOpen(v => !v)}
              sx={{
                textTransform: "none", fontWeight: 600, color: "text.secondary",
                px: 0.5, py: 0.5,
                "&:hover": { bgcolor: "action.hover", color: "primary.main" },
              }}
            >
              Advanced options
              <Typography variant="caption" sx={{ ml: 0.75, color: "text.disabled", fontWeight: 500 }}>
                find specific moments · platform · genre
              </Typography>
            </Button>
          </Box>

          <Collapse in={advancedOpen} timeout={200}>
            <Stack spacing={2.5}>

              {/* User query — natural-language clip filter (Opus's "ClipAnything"
                  equivalent). When non-empty, the AI ranks segments by match to
                  this query first, then virality. Empty = pure virality default. */}
              <Box>
                <Typography variant="caption" sx={{ fontWeight: 600, mb: 0.5, display: "block" }}>
                  Find specific moments <Chip label="optional" size="small" variant="outlined" sx={{ ml: 0.5, height: 18, fontSize: "0.6rem" }} />
                </Typography>
                <TextField
                  size="small" fullWidth
                  placeholder='e.g. "every joke that landed", "all Q&A moments", "moments where he raises his voice"'
                  value={ai.user_query}
                  onChange={e => setAi(p => ({ ...p, user_query: e.target.value }))}
                  slotProps={{ htmlInput: { maxLength: 500 } }}
                />
                <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.5, display: "block" }}>
                  Leave empty to find the most viral clips automatically.
                </Typography>
              </Box>

              {/* Target platform — biases the AI hook-type ranker. Each
                  platform's algorithm rewards different hook patterns:
                  TikTok prefers shocking_claim / emotional_peak / contrarian;
                  LinkedIn prefers actionable_tip / number_promise / contrarian;
                  YouTube Shorts sits in between. Empty (default) = vanilla
                  virality ranking, no bias. */}
              <Box>
                <Typography variant="caption" sx={{ fontWeight: 600, mb: 0.5, display: "block" }}>
                  Target platform <Chip label="optional" size="small" variant="outlined" sx={{ ml: 0.5, height: 18, fontSize: "0.6rem" }} />
                </Typography>
                <TextField select size="small" fullWidth
                  value={ai.target_platform}
                  onChange={e => setAi(p => ({ ...p, target_platform: e.target.value }))}
                >
                  <MenuItem value="">Any (general viral ranking)</MenuItem>
                  <MenuItem value="tiktok">TikTok — shock / contrarian / emotional</MenuItem>
                  <MenuItem value="youtube_shorts">YouTube Shorts — curiosity / numbers / story</MenuItem>
                  <MenuItem value="reels">Instagram Reels — emotional / lifestyle</MenuItem>
                  <MenuItem value="linkedin">LinkedIn — actionable / data-backed</MenuItem>
                  <MenuItem value="twitter">Twitter / X — hot takes / debates</MenuItem>
                </TextField>
                <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.5, display: "block" }}>
                  Biases which hook types the AI prioritizes. Clip length is unchanged.
                </Typography>
              </Box>

              {/* Genre — biases the AI's clip-selection heuristics. Different
                  content types reward different picks: a podcast clip needs a
                  standalone insight, a tutorial needs a complete tip, a gaming
                  clip wants a hype moment. Pairs cleanly with target_platform —
                  you can pick both. */}
              <Box>
                <Typography variant="caption" sx={{ fontWeight: 600, mb: 0.5, display: "block" }}>
                  Content genre <Chip label="optional" size="small" variant="outlined" sx={{ ml: 0.5, height: 18, fontSize: "0.6rem" }} />
                </Typography>
                <TextField select size="small" fullWidth
                  value={ai.genre}
                  onChange={e => setAi(p => ({ ...p, genre: e.target.value }))}
                >
                  <MenuItem value="">Auto-detect (no genre bias)</MenuItem>
                  <MenuItem value="podcast">Podcast — guest's quotable moments</MenuItem>
                  <MenuItem value="interview">Interview — best Q&amp;A answers</MenuItem>
                  <MenuItem value="qa">Q&amp;A / AMA — question + answer pairs</MenuItem>
                  <MenuItem value="vlog">Vlog — reactions + storytelling beats</MenuItem>
                  <MenuItem value="tutorial">Tutorial / how-to — standalone tips</MenuItem>
                  <MenuItem value="gaming">Gaming — big plays + reactions</MenuItem>
                  <MenuItem value="reaction">Reaction — emotional peaks</MenuItem>
                  <MenuItem value="lecture">Lecture / educational — concept explainers</MenuItem>
                </TextField>
                <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.5, display: "block" }}>
                  Tells the AI what shape a "good clip" has for this content type.
                </Typography>
              </Box>

            </Stack>
          </Collapse>

          {/* ── Shared settings group — applies to every clip ──────
              One outlined group, four compact rows. Rendered ONCE for
              both modes: these run per-clip AFTER the cut, identically
              whether the range came from the AI or the user (silence
              removal included — it trims INSIDE each already-cut clip,
              so a hand-picked range never shifts). */}
          <Box>
            <Typography variant="caption" sx={{ fontWeight: 700, color: "text.secondary", letterSpacing: 0.3, display: "block", mb: 0.75 }}>
              Every clip
            </Typography>
            <Box sx={{ border: "1px solid", borderColor: "divider", borderRadius: 2, overflow: "hidden" }}>

              <SettingRow
                icon={<SubtitlesOutlinedIcon />}
                label="Captions"
                sublabel={!captionsOn ? "Off also hides the AI hook overlay" : null}
                summary={captionsOn
                  ? (CAPTION_STYLE_OPTIONS.find(o => o.v === settings.caption_style)?.label || settings.caption_style)
                  : "Off"}
                control={<RowSwitch checked={captionsOn} />}
                onRowClick={() => set({ caption_style: captionsOn ? "none" : lastStyle.current })}
                expanded={captionsOn}
                divider={false}
              >
                <Stack spacing={1.25}>
                  <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                    {CAPTION_STYLE_OPTIONS.map(s => (
                      <Chip key={s.v} label={s.label} size="small"
                        variant={settings.caption_style === s.v ? "filled" : "outlined"}
                        color={settings.caption_style === s.v ? "primary" : "default"}
                        onClick={(e) => {
                          e.stopPropagation()
                          lastStyle.current = s.v
                          set({ caption_style: s.v })
                        }}
                        sx={{ cursor: "pointer" }} />
                    ))}
                  </Stack>
                  <Box onClick={e => e.stopPropagation()}>
                    <Typography variant="caption" sx={{ fontWeight: 600, mb: 0.5, display: "block" }}>
                      AutoEmoji
                    </Typography>
                    <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                      {EMOJI_STYLE_OPTIONS.map(({ v, label }) => (
                        <Chip key={v} label={label} size="small"
                          variant={settings.emoji_style === v ? "filled" : "outlined"}
                          color={settings.emoji_style === v ? "primary" : "default"}
                          onClick={() => set({ emoji_style: v })}
                          sx={{ cursor: "pointer" }} />
                      ))}
                    </Stack>
                    <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.5, display: "block" }}>
                      Adds 🔥 💰 ❤️ after matching words in the captions.
                    </Typography>
                  </Box>
                </Stack>
              </SettingRow>

              <SettingRow
                icon={<GraphicEqOutlinedIcon />}
                label="Remove silence & fillers"
                sublabel={'Cuts "um"s and dead air inside each clip'}
                control={<RowSwitch checked={settings.remove_silence} />}
                onRowClick={() => set({ remove_silence: !settings.remove_silence })}
                divider
              />

              <SettingRow
                icon={<CropPortraitOutlinedIcon />}
                label="Force vertical (9:16)"
                sublabel="Blur-fill landscape sources into portrait"
                control={<RowSwitch checked={settings.force_vertical} />}
                onRowClick={() => set({ force_vertical: !settings.force_vertical })}
                divider
              />

              <SettingRow
                icon={<RecordVoiceOverOutlinedIcon />}
                label="Transcription"
                summary={hasSegments && !settings.force_retranscribe
                  ? "Cached · Whisper skipped"
                  : WHISPER_QUALITIES.find(q => q.value === settings.whisper_quality)?.label || settings.whisper_quality}
                control={
                  <ExpandMoreIcon sx={{
                    fontSize: 20, color: "text.secondary", flexShrink: 0,
                    transform: transcriptOpen ? "rotate(180deg)" : "rotate(0deg)",
                    transition: "transform .15s ease",
                  }} />
                }
                onRowClick={() => setTranscriptOpen(v => !v)}
                expanded={transcriptOpen}
                divider
              >
                <Box onClick={e => e.stopPropagation()}>
                  <FormControlLabel
                    control={
                      <Checkbox
                        checked={transcribeEnabled}
                        onChange={e => set({ force_retranscribe: e.target.checked })}
                        disabled={!hasSegments}
                        size="small"
                      />
                    }
                    label={
                      <Typography variant="caption" sx={{ fontWeight: 600 }}>
                        Run Whisper {hasSegments
                          ? <Chip label="cached transcript available" size="small" color="success" variant="outlined" sx={{ ml: 0.5, height: 18, fontSize: "0.65rem" }} />
                          : <Chip label="required" size="small" color="warning" variant="outlined" sx={{ ml: 0.5, height: 18, fontSize: "0.65rem" }} />
                        }
                      </Typography>
                    }
                    sx={{ mb: 0.5 }}
                  />
                  {!hasSegments && (
                    <Typography variant="caption" sx={{ color: "text.secondary", display: "block", ml: 4, mb: 1 }}>
                      No word-level transcript found — Whisper transcribes the audio first
                      {!captionsOn ? " (still used for silence detection and per-clip titles)" : ""}
                    </Typography>
                  )}
                  {hasSegments && settings.force_retranscribe && (
                    <Typography variant="caption" sx={{ color: "warning.main", display: "block", ml: 4, mb: 1 }}>
                      Re-transcribes with the selected model (replaces the cached transcript)
                    </Typography>
                  )}
                  <TextField select size="small" fullWidth
                    value={settings.whisper_quality}
                    onChange={e => set({ whisper_quality: e.target.value })}
                    disabled={!transcribeEnabled}
                  >
                    {WHISPER_QUALITIES.map(q => (
                      <MenuItem key={q.value} value={q.value}>
                        <Stack direction="row" justifyContent="space-between" sx={{ width: "100%" }}>
                          <Typography variant="body2">{q.label}</Typography>
                          <Typography variant="caption" sx={{ color: "text.secondary", ml: 2 }}>{q.desc}</Typography>
                        </Stack>
                      </MenuItem>
                    ))}
                  </TextField>
                </Box>
              </SettingRow>

            </Box>
          </Box>
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" startIcon={<ContentCutIcon />}
          disabled={!canSubmit}
          onClick={() => {
            onExtract(video.id, {
              // Whisper has to run when there is no cached transcript, and
              // the payload has to say so or the chosen model is dropped —
              // derived once in clipSettingsPayload, shared with the bench.
              ...clipSettingsPayload(settings, { hasTranscript: hasSegments }),
              ...ai,
              mode: "ai",
            })
            onClose()
          }}>
          {`Cut now · ${estimate.label}`}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
