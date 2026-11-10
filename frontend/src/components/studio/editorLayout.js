// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
// The editor row's width budget (owner, 2026-09-16): MediaBin | centre (player
// [+ Scene panel]) | Project. The columns used to size themselves, so a wider
// media column (or a saved Project width) overflowed the row and pushed the
// Project column off-screen. One pure function now decides every width from the
// measured row, in this order of priority:
//
//   1. the player keeps its minimum (and the Scene panel its own, when open),
//   2. the PROJECT column gives way first, then the media column,
//   3. in a tight row both may drop to TIGHT_MIN before anything overflows,
//   4. the Scene panel opens beside the player only when the picture keeps its
//      size there (sceneBesideAt); otherwise it opens in the Project column
//      (never over the player), so selecting a scene moves nothing,
//   5. a row too narrow for bin + player + Project at their tightest (a
//      1024-1280px window with the app sidebar open) folds the Project column
//      into a slim rail; it opens as a drawer over the row's right edge, and
//      a selected scene opens in that drawer (audit 2026-09-25 — the column
//      used to be clipped off the right edge instead).
//
//   6. a row too narrow for the media column at all (a phone) folds that too:
//      a rail each side, the player between them, the bin a drawer over the
//      row's left edge. A desktop window never gets here.
//
// Saved (dragged) widths are preferences: they are clamped here, never
// rewritten, so they come back when the room does.

export const SCENE_PANEL_MIN = 300
export const SCENE_PANEL_WIDTH = 360
export const BIN_MIN = 340
export const BIN_INITIAL = 380
export const PROJECT_MIN = 340
export const PROJECT_INITIAL = 360
export const PROJECT_MIN_WITH_SCENE = 300
export const TIGHT_MIN = 300
// The folded Project column: a rail with a "Project" button.
export const PROJECT_RAIL_W = 44
// The media bin's floor once the Project column is folded away.
export const BIN_FLOOR = 240
// The Project drawer's width when it opens over the row.
export const PROJECT_DRAWER_W = 360
// Under this the media column folds as well: BIN_FLOOR + a rail left the 9:16 player under 340.
export const PHONE_ROW_W = 600

// 16:9 is width-limited in any centre column, so it needs the most.
export const playerMinWidth = (aspect) => (aspect === "9:16" ? 340 : aspect === "16:9" ? 560 : 480)

// The player width the Scene panel leaves untouched: a tall frame is limited by
// the row's height, so it shrinks only below these; 16:9 shrinks at once.
const sceneBesideAt = (aspect) => (aspect === "9:16" ? 340 : aspect === "16:9" ? 800 : 520)

const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi)

export function editorColumns({ rowW, binSaved, projectSaved, aspect, sceneSelected }) {
  const playerMin = playerMinWidth(aspect)
  if (!rowW || rowW <= 0) {
    // Not measured yet (first paint): the saved preferences as they are.
    return { bin: binSaved, project: projectSaved, binMin: BIN_MIN,
      projectMin: sceneSelected ? PROJECT_MIN_WITH_SCENE : PROJECT_MIN, centerMin: playerMin, sceneAsDrawer: false,
      projectAsDrawer: false }
  }
  if (rowW < PHONE_ROW_W) {
    return {
      bin: PROJECT_RAIL_W, project: PROJECT_RAIL_W, binMin: PROJECT_RAIL_W, projectMin: PROJECT_RAIL_W,
      centerMin: Math.max(0, rowW - 2 * PROJECT_RAIL_W),
      sceneAsDrawer: !!sceneSelected, projectAsDrawer: true, binAsDrawer: true,
      drawerW: Math.min(PROJECT_DRAWER_W, Math.max(280, rowW - 120)),
    }
  }
  if (rowW < TIGHT_MIN + playerMin + TIGHT_MIN) {
    // Folded: bin | player | rail. The bin gives way down to BIN_FLOOR; below
    // that the player takes what's left rather than overflowing the row.
    const bin = clamp(binSaved, Math.min(BIN_FLOOR, TIGHT_MIN), Math.max(BIN_FLOOR, rowW - playerMin - PROJECT_RAIL_W))
    const centerMin = Math.max(0, Math.min(playerMin, rowW - bin - PROJECT_RAIL_W))
    return {
      bin, project: PROJECT_RAIL_W, binMin: BIN_FLOOR, projectMin: PROJECT_RAIL_W, centerMin,
      sceneAsDrawer: !!sceneSelected, projectAsDrawer: true,
      drawerW: Math.min(PROJECT_DRAWER_W, Math.max(280, rowW - 120)),
    }
  }
  const sceneAsDrawer = !!sceneSelected && rowW < BIN_MIN + PROJECT_MIN_WITH_SCENE + sceneBesideAt(aspect) + SCENE_PANEL_MIN
  const centerMin = sceneSelected && !sceneAsDrawer ? sceneBesideAt(aspect) + SCENE_PANEL_MIN : playerMin
  let projectMin = sceneSelected && !sceneAsDrawer ? PROJECT_MIN_WITH_SCENE : PROJECT_MIN
  let binMin = BIN_MIN
  if (binMin + projectMin + centerMin > rowW) projectMin = TIGHT_MIN
  if (binMin + projectMin + centerMin > rowW) binMin = TIGHT_MIN
  const bin = clamp(binSaved, binMin, Math.max(binMin, rowW - centerMin - projectMin))
  const project = clamp(projectSaved, projectMin, Math.max(projectMin, rowW - centerMin - bin))
  return { bin, project, binMin, projectMin, centerMin, sceneAsDrawer, projectAsDrawer: false }
}
