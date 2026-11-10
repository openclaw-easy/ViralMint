// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { describe, it, expect, vi } from "vitest"
import { fireEvent, render, screen, within } from "@testing-library/react"
import TopBar from "./TopBar"

const baseProps = (over = {}) => ({
  project: { title: "My video", doc: { aspect_ratio: "9:16" } },
  timeline: { total: 12, scenes: [{}, {}] },
  saving: false, saveError: null, canUndo: true, canRedo: false,
  onUndo: vi.fn(), onRedo: vi.fn(), onRename: vi.fn(), onAspect: vi.fn(),
  onRender: vi.fn(), rendering: null, onBack: vi.fn(), onScript: vi.fn(),
  ...over,
})

describe("TopBar — shortcuts + controls", () => {
  it("surfaces every Studio shortcut in the popover", async () => {
    render(<TopBar {...baseProps()} />)
    fireEvent.click(screen.getByRole("button", { name: "Keyboard shortcuts" }))
    const pop = await screen.findByTestId("shortcuts-popover")
    for (const k of ["Space", "← →", "S", "M", "N", "⌫", "Esc", "⌘Z / ⇧⌘Z"]) {
      expect(within(pop).getByText(k)).toBeInTheDocument()
    }
    expect(within(pop).getByText("Close the preview, then the scene panel")).toBeInTheDocument()
  })

  it("render button starts the render, and shows progress while rendering", () => {
    const p = baseProps()
    const { rerender } = render(<TopBar {...p} />)
    fireEvent.click(screen.getByRole("button", { name: "Render" }))
    expect(p.onRender).toHaveBeenCalled()
    rerender(<TopBar {...p} rendering={{ pct: 42 }} />)
    expect(screen.getByRole("button", { name: /Rendering 42%/ })).toBeDisabled()
    expect(screen.getAllByRole("progressbar").some((el) => el.getAttribute("aria-valuenow") === "42")).toBe(true)
  })

  it("renaming: a blank title reverts, a new one commits on Enter/blur", () => {
    const p = baseProps()
    render(<TopBar {...p} />)
    const input = screen.getByRole("textbox", { name: "Project title" })
    fireEvent.change(input, { target: { value: "   " } })
    fireEvent.blur(input)
    expect(input).toHaveValue("My video")
    expect(p.onRename).not.toHaveBeenCalled()
    fireEvent.change(input, { target: { value: "My video" } })
    fireEvent.blur(input)
    expect(p.onRename).not.toHaveBeenCalled()
    fireEvent.change(input, { target: { value: " New name " } })
    fireEvent.blur(input)
    expect(p.onRename).toHaveBeenCalledWith("New name")
  })

  it("save states, undo/redo, aspect and back", () => {
    const p = baseProps()
    const { rerender } = render(<TopBar {...p} />)
    expect(screen.getByText("Saved")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Redo" })).toBeDisabled()
    fireEvent.click(screen.getByRole("button", { name: "Undo" }))
    expect(p.onUndo).toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: "16:9" }))
    expect(p.onAspect).toHaveBeenCalledWith("16:9")
    fireEvent.click(screen.getByRole("button", { name: "Script" }))
    expect(p.onScript).toHaveBeenCalled()
    rerender(<TopBar {...p} saving />)
    expect(screen.getByText("Saving…")).toBeInTheDocument()
    rerender(<TopBar {...p} saveError="boom" />)
    expect(screen.getByText("Not saved")).toBeInTheDocument()
  })
})

describe("TopBar — narrow bars give way before Render does", () => {
  const withWidth = (w, fn) => {
    const orig = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(cb) { this.cb = cb }
      observe() { this.cb([{ contentRect: { width: w } }]) }
      disconnect() {}
    }
    try { return fn() } finally { globalThis.ResizeObserver = orig }
  }

  it("wide: duration and the 'Script' text", () => {
    withWidth(1400, () => render(<TopBar {...baseProps()} />))
    expect(screen.getByText(/0:12 · 2 scenes/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Script" })).toHaveTextContent("Script")
  })

  it("a phone: the bar takes two rows, so Render is on screen", () => {
    withWidth(390, () => render(<TopBar {...baseProps()} />))
    expect(screen.getByTestId("topbar-row")).toHaveStyle({ flexWrap: "wrap" })
    expect(screen.getByRole("button", { name: /Render/ })).toBeInTheDocument()
  })

  it("from 760 up the bar stays one row", () => {
    withWidth(760, () => render(<TopBar {...baseProps()} />))
    expect(screen.getByTestId("topbar-row")).toHaveStyle({ flexWrap: "nowrap" })
  })

  it("on a phone Last render is an icon that still opens it, so Render stays on the second row", () => {
    const onOpenLast = vi.fn()
    withWidth(390, () => render(<TopBar {...baseProps()} onOpenLast={onOpenLast} />))
    const last = screen.getByRole("button", { name: "Last render" })
    expect(last).not.toHaveTextContent("Last render")
    fireEvent.click(last)
    expect(onOpenLast).toHaveBeenCalledOnce()
  })

  it("from 480 up Last render stays in words", () => {
    withWidth(620, () => render(<TopBar {...baseProps()} onOpenLast={vi.fn()} />))
    expect(screen.getByRole("button", { name: "Last render" })).toHaveTextContent("Last render")
  })

  it("narrow (a 1024px window): duration hidden, Script an icon — Render intact", () => {
    withWidth(760, () => render(<TopBar {...baseProps()} />))
    expect(screen.queryByText(/0:12 · 2 scenes/)).toBeNull()
    expect(screen.getByRole("button", { name: "Script" })).not.toHaveTextContent("Script")
    const renderBtn = screen.getByRole("button", { name: "Render" })
    expect(renderBtn).toBeInTheDocument()
    expect(getComputedStyle(renderBtn).flexShrink).toBe("0")
    expect(getComputedStyle(renderBtn).whiteSpace).toBe("nowrap")
    // "Saved" stays readable by screen readers.
    expect(screen.getByText("Saved")).toBeInTheDocument()
  })
})
