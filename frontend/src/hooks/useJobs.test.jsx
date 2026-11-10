// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
/**
 * useJobs — the app-wide job poll also repairs a tracked job whose terminal
 * WS event never arrived (it used to stay "running" until a reload).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, renderHook } from "@testing-library/react"

const httpMock = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock("../api/http", () => ({ default: httpMock }))

import useJobs from "./useJobs"
import useAppStore from "../store/appStore"

const row = (id, status, extra = {}) => ({
  id, status, job_type: "generate", progress_pct: 0, current_step: null,
  title: null, error_message: null, completed_at: null, started_at: null,
  input_json: null, output_json: null, ...extra,
})

describe("useJobs — reconciles jobs the UI still shows as running", () => {
  let now
  beforeEach(() => {
    vi.useFakeTimers()
    now = 1_000_000
    vi.spyOn(Date, "now").mockImplementation(() => now)
    httpMock.get.mockReset()
    useAppStore.setState({ jobs: [], activeJobs: {} })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })
  const tracked = (id) => ({ jobId: id, jobType: "generate", status: "running", percent: 0, step: "", startedAt: now - 60_000 })

  it("the server failed the job: the missed event is repaired by the first poll", async () => {
    useAppStore.setState({ activeJobs: { clip1: tracked("clip1") } })
    httpMock.get.mockResolvedValueOnce({ data: { jobs: [row("clip1", "failed", { error_message: "Video not found" })], total: 1 } })
    renderHook(() => useJobs(5000, 200))
    await act(async () => {})
    const j = useAppStore.getState().activeJobs.clip1
    expect(j.status).toBe("failed")
    expect(j.step).toBe("Video not found")
  })

  it("a complete listing without the tracked job drops it", async () => {
    useAppStore.setState({ activeJobs: { ghost: tracked("ghost") } })
    httpMock.get.mockResolvedValueOnce({ data: { jobs: [row("other", "success")], total: 1 } })
    renderHook(() => useJobs(5000, 200))
    await act(async () => {})
    expect(useAppStore.getState().activeJobs.ghost).toBeUndefined()
  })

  it("a capped window (total beyond the page) never declares a job gone", async () => {
    useAppStore.setState({ activeJobs: { maybe: tracked("maybe") } })
    httpMock.get.mockResolvedValueOnce({ data: { jobs: [row("other", "success")], total: 500 } })
    renderHook(() => useJobs(5000, 1))
    await act(async () => {})
    expect(useAppStore.getState().activeJobs.maybe.status).toBe("running")
  })

  it("a job the UI shows as running keeps the poll at the fast rate", async () => {
    useAppStore.setState({ activeJobs: { slow: tracked("slow") } })
    httpMock.get.mockResolvedValue({ data: { jobs: [row("slow", "running")], total: 1 } })
    renderHook(() => useJobs(5000, 200))
    await act(async () => {})
    expect(httpMock.get).toHaveBeenCalledTimes(1)
    // The store's jobs list has a running row too here, so set it terminal to
    // isolate the activeJobs signal.
    useAppStore.setState({ jobs: [row("slow", "success")] })
    await act(async () => { vi.advanceTimersByTime(5000) })
    expect(httpMock.get).toHaveBeenCalledTimes(2)
  })
})
