// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
/**
 * A constraint_warning toast carries the backend's severity: an "info" notice
 * used to show a warning icon because the toast only knew error and warning.
 */
import { beforeEach, describe, expect, it, vi } from "vitest"
import { act, renderHook } from "@testing-library/react"

const handlers = vi.hoisted(() => ({}))
vi.mock("../api/websocket", () => ({
  ws: {
    on: (type, fn) => { handlers[type] = fn; return () => { delete handlers[type] } },
    connect: () => {}, disconnect: () => {}, updateAuthToken: () => {}, send: () => {},
  },
}))
const httpMock = vi.hoisted(() => ({ get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn() }))
vi.mock("../api/http", () => ({ default: httpMock }))

import useWebSocket from "./useWebSocket"
import useAppStore from "../store/appStore"

let showSnackbar
beforeEach(() => {
  showSnackbar = vi.fn()
  useAppStore.setState({ showSnackbar })
})

describe("constraint_warning toasts carry the backend's severity", () => {
  it("info stays info, error stays error, anything else is a warning", () => {
    renderHook(() => useWebSocket())
    act(() => handlers.constraint_warning({ message: "no audio to begin with", severity: "info", constraint: "mute_no_audio" }))
    expect(showSnackbar).toHaveBeenLastCalledWith("no audio to begin with", "info")
    act(() => handlers.constraint_warning({ message: "boom", severity: "error", constraint: "x" }))
    expect(showSnackbar).toHaveBeenLastCalledWith("boom", "error")
    act(() => handlers.constraint_warning({ message: "careful", severity: "warning", constraint: "x" }))
    expect(showSnackbar).toHaveBeenLastCalledWith("careful", "warning")
    act(() => handlers.constraint_warning({ message: "old", constraint: "x" }))
    expect(showSnackbar).toHaveBeenLastCalledWith("old", "warning")
  })
})
