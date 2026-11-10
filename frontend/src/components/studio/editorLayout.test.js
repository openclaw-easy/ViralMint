// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { describe, expect, it } from "vitest"
import { BIN_FLOOR, BIN_INITIAL, PROJECT_INITIAL, PROJECT_MIN, PROJECT_RAIL_W, TIGHT_MIN, editorColumns, playerMinWidth } from "./editorLayout"

const fits = (c, rowW) => c.bin + c.project + c.centerMin <= rowW

describe("a phone", () => {
  for (const rowW of [320, 390, 430, 599]) {
    for (const aspect of ["9:16", "16:9", "1:1"]) {
      it(`row ${rowW} ${aspect}: a rail each side and the player between them`, () => {
        const c = editorColumns({ rowW, binSaved: 380, projectSaved: 360, aspect, sceneSelected: false })
        expect(c.binAsDrawer).toBe(true)
        expect(c.projectAsDrawer).toBe(true)
        expect(c.bin + c.project + c.centerMin).toBe(rowW)
        expect(c.centerMin).toBeGreaterThan(rowW - 100)
      })
    }
  }
  it("a window at the line keeps its media column, as before", () => {
    const c = editorColumns({ rowW: 600, binSaved: 380, projectSaved: 360, aspect: "9:16", sceneSelected: false })
    expect(c.binAsDrawer).toBeFalsy()
    expect(c.bin).toBeGreaterThanOrEqual(240)
  })
})

describe("editorColumns — the row never overflows", () => {
  // The row widths measured in the browser at 1600 / 1440 / 1280 px windows.
  for (const rowW of [1360, 1200, 1040]) {
    for (const sceneSelected of [false, true]) {
      for (const [binSaved, projectSaved] of [[432, 600], [832, 600], [432, 1000], [900, 1000]]) {
        it(`row ${rowW}, scene ${sceneSelected}, saved bin ${binSaved} / project ${projectSaved}`, () => {
          const c = editorColumns({ rowW, binSaved, projectSaved, aspect: "9:16", sceneSelected })
          expect(fits(c, rowW)).toBe(true)
          expect(c.bin).toBeGreaterThanOrEqual(c.binMin)
          expect(c.project).toBeGreaterThanOrEqual(c.projectMin)
        })
      }
    }
  }

  it("keeps the saved widths when there is room", () => {
    expect(editorColumns({ rowW: 2200, binSaved: 432, projectSaved: 600, aspect: "9:16", sceneSelected: true }))
      .toMatchObject({ bin: 432, project: 600, sceneAsDrawer: false })
  })

  it("the Project column gives way before the media column", () => {
    const c = editorColumns({ rowW: 1500, binSaved: 432, projectSaved: 600, aspect: "9:16", sceneSelected: true })
    expect(c.bin).toBe(432)                                              // kept whole
    expect(c.project).toBe(1500 - 432 - (playerMinWidth("9:16") + 300))  // took the whole squeeze
  })

  it("a very narrow row opens the scene in the Project column, never over the player", () => {
    const c = editorColumns({ rowW: 1200, binSaved: 432, projectSaved: 600, aspect: "16:9", sceneSelected: true })
    expect(c.sceneAsDrawer).toBe(true)
    expect(c.centerMin).toBe(playerMinWidth("16:9"))
    expect(fits(c, 1200)).toBe(true)
  })

  it("before the row is measured, uses the saved widths", () => {
    expect(editorColumns({ rowW: 0, binSaved: 500, projectSaved: 700, aspect: "9:16", sceneSelected: false }))
      .toMatchObject({ bin: 500, project: 700, sceneAsDrawer: false })
  })

  it("keeps all three columns while bin + player + Project fit at their tightest", () => {
    const rowW = TIGHT_MIN + playerMinWidth("9:16") + TIGHT_MIN
    const c = editorColumns({ rowW, binSaved: 432, projectSaved: 600, aspect: "9:16", sceneSelected: false })
    expect(c.projectAsDrawer).toBe(false)
    expect(fits(c, rowW)).toBe(true)
  })
})

describe("editorColumns — a narrow row folds the Project column into a drawer", () => {
  // 1024 and 1280 px windows minus the 240px app sidebar.
  for (const [rowW, aspect] of [[784, "9:16"], [784, "16:9"], [784, "1:1"], [1040, "16:9"], [900, "9:16"]]) {
    for (const sceneSelected of [false, true]) {
      it(`row ${rowW} ${aspect}, scene ${sceneSelected}: rail + drawer, nothing clipped`, () => {
        const c = editorColumns({ rowW, binSaved: 432, projectSaved: 600, aspect, sceneSelected })
        expect(c.projectAsDrawer).toBe(true)
        expect(c.project).toBe(PROJECT_RAIL_W)
        expect(c.bin).toBeGreaterThanOrEqual(BIN_FLOOR)
        expect(c.bin + c.project + c.centerMin).toBeLessThanOrEqual(rowW)
        // A selected scene always opens in the drawer — there's no room beside the player.
        expect(c.sceneAsDrawer).toBe(sceneSelected)
        expect(c.drawerW).toBeLessThanOrEqual(rowW - 100)
      })
    }
  }

  it("the player keeps its minimum whenever the row allows it", () => {
    const c = editorColumns({ rowW: 784, binSaved: 900, projectSaved: 600, aspect: "9:16", sceneSelected: false })
    expect(c.centerMin).toBe(playerMinWidth("9:16"))
    expect(c.bin).toBe(784 - playerMinWidth("9:16") - PROJECT_RAIL_W)
  })
})

describe("editorColumns — the player gets the room", () => {
  const at = (rowW, aspect, sceneSelected = false) => editorColumns({
    rowW, binSaved: BIN_INITIAL, projectSaved: PROJECT_INITIAL, aspect, sceneSelected,
  })
  const centre = (c, rowW) => rowW - c.bin - c.project

  it("the side columns are narrow by default, and the centre takes the rest", () => {
    expect(PROJECT_INITIAL).toBeLessThanOrEqual(360)
    expect(BIN_INITIAL).toBeLessThanOrEqual(380)
    expect(centre(at(1440, "16:9"), 1440)).toBeGreaterThanOrEqual(700)
    expect(centre(at(1680, "16:9"), 1680)).toBeGreaterThanOrEqual(940)
  })

  it("16:9 keeps a useful width wherever all three columns show", () => {
    for (const rowW of [1160, 1200, 1300]) expect(centre(at(rowW, "16:9"), rowW)).toBeGreaterThanOrEqual(560)
  })

  it("selecting a scene doesn't move the player when it opens in the Project column", () => {
    for (const [rowW, aspect] of [[1040, "9:16"], [1200, "9:16"], [1200, "16:9"], [1440, "16:9"], [1680, "16:9"], [1200, "1:1"], [1440, "1:1"]]) {
      const idle = at(rowW, aspect)
      const open = at(rowW, aspect, true)
      expect(open.sceneAsDrawer).toBe(true)
      expect([open.bin, open.project]).toEqual([idle.bin, idle.project])
    }
  })

  it("a scene opens beside the player only when the picture keeps its size", () => {
    expect(at(1440, "9:16", true).sceneAsDrawer).toBe(false)
    expect(at(1680, "1:1", true).sceneAsDrawer).toBe(false)
    expect(at(2320, "16:9", true).sceneAsDrawer).toBe(false)
  })

  it("the Project column has one floor while idle", () => {
    expect(at(1300, "9:16").projectMin).toBe(PROJECT_MIN)
  })
})
