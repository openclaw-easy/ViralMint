// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { describe, it, expect, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

vi.mock("../../api/http", () => ({ default: { post: vi.fn(), get: vi.fn(() => Promise.resolve({ data: {} })) } }))
import MediaBin from "./MediaBin"

const resize = { width: 320, dragging: false, reset: () => {}, handleProps: { onKeyDown: () => {} } }
const props = { onPreview: vi.fn(), onUseStock: vi.fn(), onUseMine: vi.fn(), onUpload: vi.fn(), onSearch: vi.fn(async () => []),
  mine: [], onMatchClips: vi.fn(), onRemoveClip: vi.fn(), request: null, width: 320, resize }

describe("MediaBin — Use on a clip card", () => {
  const candidates = [
    { provider: "pexels", provider_id: "1", creator: "A very long creator name that will not fit on the card" },
    { provider: "pexels", provider_id: "2", creator: "Ana" },
  ]
  const boundScene = { uid: "a", index: 0, line: "One.", visual: { kind: "stock", state: "bound", query: "cat", asset: { provider: "pexels", provider_id: "1" } } }

  it("one primary Use per card; the scene's current clip says In use", async () => {
    const onUseStock = vi.fn()
    render(<MediaBin {...props} selected={boundScene} scenes={[boundScene]} onUseStock={onUseStock} onSearch={async () => candidates} />)
    const use = await screen.findByRole("button", { name: /^Use .*Ana for scene 1$/ })
    expect(use.className).toMatch(/MuiButton-contained/)
    const inUse = screen.getByRole("button", { name: /^In use on scene 1: / })
    expect(inUse).toHaveAttribute("aria-disabled", "true")
    fireEvent.click(inUse)
    expect(onUseStock).not.toHaveBeenCalled()
    fireEvent.click(use)
    expect(onUseStock).toHaveBeenCalledWith(candidates[1])
  })

  it("your own clip in the Media tab: the same button, the same state", () => {
    const scene = { uid: "a", index: 0, line: "One.", visual: { kind: "user_clip", asset: { asset_id: "m1" } } }
    const mine = [{ asset_id: "m1", title: "One", url: "/1.mp4" }, { asset_id: "m2", title: "Two", url: "/2.mp4" }]
    const onUseMine = vi.fn()
    render(<MediaBin {...props} selected={scene} scenes={[scene]} mine={mine} onUseMine={onUseMine} />)
    fireEvent.click(screen.getByRole("tab", { name: "Media" }))
    expect(screen.getByRole("button", { name: "In use on scene 1: One" })).toHaveAttribute("aria-disabled", "true")
    fireEvent.click(screen.getByRole("button", { name: "Use Two for scene 1" }))
    expect(onUseMine).toHaveBeenCalledWith(mine[1])
  })
})
