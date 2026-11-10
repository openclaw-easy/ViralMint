// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
/**
 * reconcileActiveJobs — the poll-driven safety net. A missed job_failed /
 * job_complete left a tracked job "running" until a page reload; the Activity
 * poll now reconciles it against the server's rows.
 */
import { beforeEach, describe, expect, it } from "vitest"
import useAppStore from "./appStore"

describe("reconcileActiveJobs (poll safety net)", () => {
  const old = Date.now() - 60_000
  const track = (id, extra = {}) =>
    useAppStore.setState((s) => ({
      activeJobs: { ...s.activeJobs, [id]: { jobId: id, jobType: "generate", status: "running", percent: 0, step: "", startedAt: old, ...extra } },
    }))
  beforeEach(() => useAppStore.setState({ activeJobs: {} }))

  it("a job the server failed turns failed, with the server's error", () => {
    track("f1")
    useAppStore.getState().reconcileActiveJobs([{ id: "f1", status: "failed", error_message: "Source video not found" }], new Set())
    const j = useAppStore.getState().activeJobs.f1
    expect(j.status).toBe("failed")
    expect(j.step).toBe("Source video not found")
  })

  it("a job the server finished turns success", () => {
    track("s1")
    useAppStore.getState().reconcileActiveJobs([{ id: "s1", status: "success" }], new Set())
    expect(useAppStore.getState().activeJobs.s1.status).toBe("success")
  })

  it("a cancelled job is dropped", () => {
    track("c1")
    useAppStore.getState().reconcileActiveJobs([{ id: "c1", status: "cancelled" }], new Set())
    expect(useAppStore.getState().activeJobs.c1).toBeUndefined()
  })

  it("a job with no row and not in the authoritative live set is dropped (the deleted-row case)", () => {
    track("gone")
    useAppStore.getState().reconcileActiveJobs([], new Set())
    expect(useAppStore.getState().activeJobs.gone).toBeUndefined()
  })

  it("without an authoritative live set, a missing row is LEFT alone (a capped window never guesses)", () => {
    track("maybe")
    useAppStore.getState().reconcileActiveJobs([], null)
    expect(useAppStore.getState().activeJobs.maybe.status).toBe("running")
  })

  it("live jobs are untouched", () => {
    track("live")
    useAppStore.getState().reconcileActiveJobs([{ id: "live", status: "running" }], new Set(["live"]))
    expect(useAppStore.getState().activeJobs.live.status).toBe("running")
  })

  it("a just-started job is protected by the 15 s grace", () => {
    track("fresh", { startedAt: Date.now() })
    useAppStore.getState().reconcileActiveJobs([], new Set())
    expect(useAppStore.getState().activeJobs.fresh.status).toBe("running")
  })

  it("already-finished entries are not rewritten", () => {
    track("done", { status: "success", percent: 100 })
    useAppStore.getState().reconcileActiveJobs([{ id: "done", status: "failed", error_message: "x" }], new Set())
    expect(useAppStore.getState().activeJobs.done.status).toBe("success")
  })
})
