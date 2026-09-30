// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { Box, Typography } from "@mui/material"
import { onActivate } from "../../../utils/a11y"
import CloseIcon from "@mui/icons-material/Close"
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesomeOutlined"

/* ── One pending cut, drawn on the timeline ────────────────────
   Three hit zones, and their widths are the whole ergonomics of the
   component: 10px grab strips on each edge for resize, the remainder for
   move. Below ~28px total the two edges would swallow the body, so a very
   short range drops its move zone rather than becoming un-resizable —
   you can still slide it from the range list.

   The block reports gestures as (kind, clientX) and owns no time math:
   px↔seconds lives in Timeline, which is the only thing that knows how
   wide the track is.
*/

const EDGE_PX = 10

export default function RangeBlock({
  index, range, left, width, selected, duration,
  onGrab,        // (kind: "move"|"start"|"end", event) => void
  onSelect,      // () => void
  onRemove,      // () => void
}) {
  const len = range.end - range.start
  const tiny = width < 44
  const showBody = width >= 2 * EDGE_PX + 8

  return (
    <Box
      onPointerDown={(e) => {
        // Selecting on pointerdown (not click) means the block is already
        // the active one by the time the drag moves a pixel, so the IN/OUT
        // frames track the block you actually grabbed.
        e.stopPropagation()
        onSelect()
      }}
      sx={(t) => ({
        position: "absolute",
        top: 0, bottom: 0,
        left, width,
        borderRadius: 1.5,
        overflow: "hidden",
        // Selected reads as a solid tinted pane with a bright rim; the
        // others sit back so a busy timeline still has one obvious subject.
        bgcolor: selected
          ? "rgba(99,179,237,0.30)"
          : t.palette.mode === "dark" ? "rgba(255,255,255,0.14)" : "rgba(25,45,90,0.16)",
        border: "2px solid",
        borderColor: selected ? "primary.main" : "transparent",
        boxShadow: selected ? "0 0 0 1px rgba(0,0,0,0.35), 0 4px 14px rgba(0,0,0,0.35)" : "none",
        transition: "background-color .12s ease, border-color .12s ease",
        "&:hover": { borderColor: selected ? "primary.main" : "primary.light" },
      })}
    >
      {/* Move zone */}
      {showBody && (
        <Box
          onPointerDown={(e) => { e.stopPropagation(); onSelect(); onGrab("move", e) }}
          sx={{
            position: "absolute", top: 0, bottom: 0,
            left: EDGE_PX, right: EDGE_PX,
            cursor: "grab",
            "&:active": { cursor: "grabbing" },
          }}
        />
      )}

      {/* Edge handles. The inner pill is the visible affordance; the
          parent is the (wider) hit area, so a 2px-looking line is still
          a 10px target. */}
      {["start", "end"].map((edge) => (
        <Box
          key={edge}
          onPointerDown={(e) => { e.stopPropagation(); onSelect(); onGrab(edge, e) }}
          sx={{
            position: "absolute", top: 0, bottom: 0,
            [edge === "start" ? "left" : "right"]: 0,
            width: EDGE_PX,
            cursor: "ew-resize",
            display: "flex", alignItems: "center",
            justifyContent: edge === "start" ? "flex-start" : "flex-end",
            px: "2px",
            touchAction: "none",
          }}
        >
          <Box sx={{
            width: 4, height: "62%", borderRadius: 2,
            bgcolor: selected ? "primary.main" : "rgba(255,255,255,0.75)",
            boxShadow: "0 0 0 1px rgba(0,0,0,0.45)",
          }} />
        </Box>
      ))}

      {/* Label — index always; the ✨ marks a block the AI proposed (kept
          after adoption, so "my cut" and "its cut" stay distinguishable);
          duration only when there's room without overlapping the handles. */}
      <Box sx={{
        position: "absolute", top: 3, left: EDGE_PX + 3, right: EDGE_PX + 3,
        display: "flex", alignItems: "center", gap: 0.5,
        pointerEvents: "none", overflow: "hidden",
      }}>
        <Box sx={{
          flexShrink: 0,
          minWidth: 15, height: 15, px: "3px",
          borderRadius: "4px",
          bgcolor: selected ? "primary.main" : "rgba(0,0,0,0.55)",
          color: "#fff",
          fontSize: "0.6rem", fontWeight: 800, lineHeight: "15px",
          textAlign: "center",
        }}>
          {index + 1}
        </Box>
        {range.meta && (
          <AutoAwesomeIcon sx={{
            flexShrink: 0, fontSize: 11, color: "#ffd479",
            filter: "drop-shadow(0 1px 2px rgba(0,0,0,0.9))",
          }} />
        )}
        {!tiny && (
          <Typography sx={{
            fontSize: "0.62rem", fontWeight: 700, color: "#fff",
            textShadow: "0 1px 3px rgba(0,0,0,0.9)",
            whiteSpace: "nowrap", overflow: "hidden", textOverflow: "clip",
          }}>
            {len < 60 ? `${len.toFixed(1)}s` : `${Math.floor(len / 60)}m${String(Math.round(len % 60)).padStart(2, "0")}`}
          </Typography>
        )}
      </Box>

      {/* Remove — only on the selected block, so a dense timeline isn't a
          field of ✕ buttons waiting for a misclick. */}
      {selected && width >= 56 && (
        <Box
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => { e.stopPropagation(); onRemove() }}
          // Reachable and pressable: it was a role="button" with no tabIndex
          // and no key handler, so a keyboard user could neither land on it
          // nor fire it. (⌫ on the selected range is the faster path; this is
          // the one that matches what the mouse sees.)
          role="button" tabIndex={0}
          onKeyDown={onActivate((e) => { e.stopPropagation(); onRemove() })}
          aria-label={`Remove range ${index + 1}`}
          sx={{
            position: "absolute", bottom: 3, right: EDGE_PX + 2,
            width: 16, height: 16, borderRadius: "50%",
            bgcolor: "rgba(0,0,0,0.6)", color: "#fff",
            display: "flex", alignItems: "center", justifyContent: "center",
            cursor: "pointer",
            "&:hover": { bgcolor: "error.main" },
          }}
        >
          <CloseIcon sx={{ fontSize: 11 }} />
        </Box>
      )}
    </Box>
  )
}
