// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors

/** The control a key press in the editor belongs to, or null when it is the editor's own.
 *  Real buttons AND focusable tiles (role="button" divs: thumbnails, alternates, stock tiles):
 *  Backspace on a focused tile is not "delete the scene", and Space there activates the tile.
 *  The one exception is the SELECTED scene's own block in the timeline. A click on a block selects
 *  the scene and leaves the focus on it, so until the next click every Space and Delete used to
 *  land on a "button" and do nothing. That block is the scene, not a control. */
export function controlOf(el) {
  const control = el?.tagName === "BUTTON" ? el : el?.closest?.('[role="button"]') || null
  if (control?.matches?.('[data-scene][aria-pressed="true"]')) return null
  return control
}
