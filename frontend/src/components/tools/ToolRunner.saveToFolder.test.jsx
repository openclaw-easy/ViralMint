// "Can't save clips directly to any folder in the pc" (user report, 2026-11-12): a finished tool
// result only ever went to the browser's download folder. Save to folder… opens the same native
// picker as Clipper's "Save clips" and has the backend copy the result there.
import { describe, it, expect, vi, beforeEach } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import ToolRunner from "./ToolRunner"
import useAppStore from "../../store/appStore"
import http from "../../api/http"

vi.mock("../../api/http", () => ({ default: { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), delete: vi.fn() } }))

const JOB = "job-9"
const page = (props = {}) => render(
  <MemoryRouter><ToolRunner title="Audio Enhance" endpoint="/api/tools/audio-enhance" {...props} /></MemoryRouter>,
)
// The page adopts a RUNNING job of its type; then the job finishes.
async function finished() {
  act(() => { useAppStore.getState().startJob(JOB, "tool:audio_enhance", "Enhancing...") })
  await screen.findByRole("button", { name: "Cancel" })
  act(() => { useAppStore.getState().completeJob(JOB) })
}

describe("Save to folder… on a finished tool result", () => {
  beforeEach(() => {
    http.post.mockReset()
    useAppStore.setState({ activeJobs: {}, snackbarQueue: [], snackbar: { open: false, message: "", severity: "info", action: null, key: 0 } })
  })

  it("asks for a folder, then has the backend copy the result there", async () => {
    http.post.mockImplementation((url) => Promise.resolve({ data: url.endsWith("pick-folder")
      ? { ok: true, path: "/Users/me/Clips" } : { ok: true, path: "/Users/me/Clips/viralmint_tool_audio_enhance.mp3" } }))
    page(); await finished()
    fireEvent.click(await screen.findByRole("button", { name: /save to folder/i }))
    await waitFor(() => expect(http.post).toHaveBeenCalledWith(`/api/tools/save/${JOB}`, { destination: "/Users/me/Clips" }))
    expect(http.post.mock.calls[0]).toEqual(["/api/videos/pick-folder", null, { timeout: 0 }])
    await waitFor(() => expect(useAppStore.getState().snackbar.message).toBe("Saved to /Users/me/Clips/viralmint_tool_audio_enhance.mp3"))
  })

  it("a cancelled picker saves nothing and says nothing; a broken picker says so", async () => {
    http.post.mockResolvedValueOnce({ data: { ok: false, path: null } })
    page(); await finished()
    const button = await screen.findByRole("button", { name: /save to folder/i })
    fireEvent.click(button)
    await waitFor(() => expect(button).toBeEnabled())
    expect(http.post).toHaveBeenCalledTimes(1)
    expect(useAppStore.getState().snackbar.open).toBe(false)

    http.post.mockResolvedValueOnce({ data: { ok: false, path: null, error: "zenity not found" } })
    fireEvent.click(button)
    await waitFor(() => expect(useAppStore.getState().snackbar.message).toMatch(/Couldn't open the folder picker: zenity not found/))
  })

  it("a refused save gives the server's reason", async () => {
    http.post.mockResolvedValueOnce({ data: { ok: true, path: "/ro" } })
      .mockRejectedValueOnce({ response: { data: { detail: "Couldn't write to that folder: Permission denied" } } })
    page(); await finished()
    fireEvent.click(await screen.findByRole("button", { name: /save to folder/i }))
    await waitFor(() => expect(useAppStore.getState().snackbar.message).toMatch(/Save failed: Couldn't write to that folder: Permission denied/))
  })

  it("a page with its own download link keeps just its Download", async () => {
    page({ downloadHref: (id) => `/custom/${id}` }); await finished()
    await screen.findByRole("button", { name: /run again/i })
    expect(screen.queryByRole("button", { name: /save to folder/i })).toBeNull()
  })
})
