// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { AxiosError, CanceledError } from "axios"
import http from "./http"

let adapter
beforeEach(() => {
  adapter = vi.fn()
  http.defaults.adapter = adapter
})
afterEach(() => vi.restoreAllMocks())

describe("a cancel is not an error", () => {
  it("logs nothing for a cancel and still logs a real failure", async () => {
    const log = vi.spyOn(console, "error").mockImplementation(() => {})
    adapter.mockImplementationOnce((config) => Promise.reject(new CanceledError("canceled", undefined, config)))
    await expect(http.get("/x")).rejects.toBeTruthy()
    expect(log).not.toHaveBeenCalled()
    adapter.mockImplementationOnce((config) => Promise.reject(new AxiosError("Network Error", "ERR_NETWORK", config)))
    await expect(http.get("/y")).rejects.toBeTruthy()
    expect(log).toHaveBeenCalledTimes(1)
  })
})
