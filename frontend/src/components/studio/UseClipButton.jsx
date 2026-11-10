// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { Button, Tooltip } from "@mui/material"
import CheckRoundedIcon from "@mui/icons-material/CheckRounded"

// The one "Use this clip" button on every clip card (stock results, your own
// clips, a scene's other clips). `inUse`: the scene already shows this clip.
//
// In use is aria-disabled, never `disabled`: the button you just pressed turns
// into this state, and a disabled button drops keyboard focus to <body>, where
// the editor's Backspace deletes the selected scene.
export default function UseClipButton({ name, sceneN = null, inUse = false, disabled = false, onUse, fullWidth = false }) {
  const scene = sceneN ? ` for scene ${sceneN}` : ""
  const label = inUse ? `In use${sceneN ? ` on scene ${sceneN}` : ""}: ${name}` : `Use ${name}${scene}`
  const tip = inUse ? "The scene is using this clip"
    : disabled ? "Select a scene in the timeline (one that isn't generating) first"
      : sceneN ? `Use for scene ${sceneN}` : "Use for the selected scene"
  return (
    <Tooltip title={tip}>
      <span style={{ display: fullWidth ? "flex" : "inline-flex", flexShrink: 0 }}>
        <Button
          size="small" fullWidth={fullWidth} disableElevation
          variant={inUse ? "outlined" : "contained"}
          aria-label={label}
          aria-disabled={inUse || undefined}
          disabled={disabled && !inUse}
          disableRipple={inUse}
          startIcon={inUse ? <CheckRoundedIcon sx={{ fontSize: "15px !important" }} /> : null}
          // detail is the browser's click count: 2 is the second click of a double-click.
          onClick={inUse ? undefined : (e) => { if (e.detail < 2) onUse() }}
          sx={{
            minWidth: 0, minHeight: 28, px: 1.25, flexShrink: 0, textTransform: "none", fontWeight: 800, fontSize: 12, whiteSpace: "nowrap",
            "& .MuiButton-startIcon": { mr: 0.4, ml: -0.25 },
            ...(inUse ? { cursor: "default", "&:hover": { bgcolor: "transparent" } } : null),
            "&.Mui-focusVisible": { outline: "2px solid", outlineColor: "primary.light", outlineOffset: 2 },
          }}
        >
          {inUse ? "In use" : "Use"}
        </Button>
      </span>
    </Tooltip>
  )
}
