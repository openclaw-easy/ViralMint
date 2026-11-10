// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"
import UseClipButton from "./UseClipButton"

describe("UseClipButton", () => {
  it("names the clip and the scene, and uses it on click", () => {
    const onUse = vi.fn()
    render(<UseClipButton name="Pexels · Ana" sceneN={3} onUse={onUse} />)
    const btn = screen.getByRole("button", { name: "Use Pexels · Ana for scene 3" })
    expect(btn).toHaveTextContent(/^Use$/)
    fireEvent.click(btn)
    expect(onUse).toHaveBeenCalledOnce()
  })

  it("the second click of a double-click does nothing", () => {
    const onUse = vi.fn()
    render(<UseClipButton name="Pexels · Ana" sceneN={3} onUse={onUse} />)
    const btn = screen.getByRole("button", { name: /^Use / })
    fireEvent.click(btn, { detail: 1 })
    fireEvent.click(btn, { detail: 2 })
    expect(onUse).toHaveBeenCalledOnce()
  })

  it("with no scene to use it on it is disabled, and still visible", () => {
    const onUse = vi.fn()
    render(<UseClipButton name="Pexels · Ana" disabled onUse={onUse} />)
    const btn = screen.getByRole("button", { name: "Use Pexels · Ana" })
    expect(btn).toBeDisabled()
    expect(btn).toBeVisible()
  })

  it("the current clip says In use, does nothing, and keeps keyboard focus", () => {
    const onUse = vi.fn()
    const { rerender } = render(<UseClipButton name="Pexels · Ana" sceneN={3} onUse={onUse} />)
    const btn = screen.getByRole("button", { name: /^Use / })
    btn.focus()
    rerender(<UseClipButton name="Pexels · Ana" sceneN={3} inUse onUse={onUse} />)
    const same = screen.getByRole("button", { name: "In use on scene 3: Pexels · Ana" })
    expect(same).toBe(btn)
    expect(same).toHaveTextContent("In use")
    // aria-disabled, never `disabled`: a disabled button drops focus to <body>,
    // where the editor's Backspace would delete the scene.
    expect(same).toHaveAttribute("aria-disabled", "true")
    expect(same).not.toBeDisabled()
    expect(document.activeElement).toBe(same)
    fireEvent.click(same)
    expect(onUse).not.toHaveBeenCalled()
  })
})
