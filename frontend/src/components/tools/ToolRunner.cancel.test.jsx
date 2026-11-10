// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
// A running tool page says how to stop it (Cancel used to live only in Activity, out of view while a
// long script ran). One door for every ToolRunner page: same DELETE, same honest wording as Activity.
import { describe, it, expect, vi, beforeEach } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import ToolRunner from "./ToolRunner"
import useAppStore from "../../store/appStore"
import http from "../../api/http"

vi.mock("../../api/http", () => ({ default: { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), delete: vi.fn() } }))

const JOB = "job-1"
const page = () => render(
  <MemoryRouter><ToolRunner title="Voice-over" endpoint="/api/tools/voiceover" /></MemoryRouter>,
)

describe("Cancel on a running tool page", () => {
  beforeEach(() => {
    http.delete.mockReset()
    useAppStore.setState({ activeJobs: {}, snackbar: { open: false, message: "", severity: "info", action: null } })
    act(() => { useAppStore.getState().startJob(JOB, "tool:voiceover", "Generating voice...") })
  })

  it("is there while it runs, and says the running step finishes on its own when the server says best_effort", async () => {
    http.delete.mockResolvedValue({ data: { cancelled: true, best_effort: true } })
    page()
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }))
    await waitFor(() => expect(http.delete).toHaveBeenCalledWith(`/api/jobs/${JOB}`))
    await waitFor(() => expect(useAppStore.getState().activeJobs[JOB]).toBeUndefined())
    expect(useAppStore.getState().snackbar.message).toMatch(/already running will finish on its own/)
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull()
  })

  it("a job that never started just says cancelled", async () => {
    http.delete.mockResolvedValue({ data: { cancelled: true, best_effort: false } })
    page()
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }))
    await waitFor(() => expect(useAppStore.getState().snackbar.message).toMatch(/Job cancelled/))
  })

  it("a refused cancel keeps the job on screen and gives the server's reason", async () => {
    http.delete.mockRejectedValue({ response: { data: { detail: "Not now" } } })
    page()
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }))
    await waitFor(() => expect(useAppStore.getState().snackbar.message).toMatch(/Not now/))
    expect(useAppStore.getState().activeJobs[JOB]).toBeDefined()
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled()
  })
})
