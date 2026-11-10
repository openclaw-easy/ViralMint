import { useEffect, useRef } from "react"
import { ws } from "../api/websocket"
import http from "../api/http"
import useAppStore from "../store/appStore"

// Smart Video Studio job types (backend/core/tasks/studio.py: studio_plan,
// studio_voice, …). They are editor housekeeping: the Activity panel tracks
// them and the open editor shows their result, so the chat transcript gets no
// card for each. The render is job type `generate` and keeps the regular path.
export const isStudioJob = (jobType) => typeof jobType === "string" && jobType.startsWith("studio_")

export default function useWebSocket() {
  const {
    setStreaming,
    appendStreamToken,
    finalizeStream,
    clearStreamingText,
    setActiveWizard,
    addMessage,
    prependSession,
    updateSession,
    setActiveSessionId,
    showSnackbar,
    startJob,
    updateJobProgress,
    completeJob,
    failJob,
    removeJob,
  } = useAppStore()

  // Chat tokens arrive far faster than the screen refreshes. Writing each one
  // straight to the store cost a store notify + a markdown re-parse of the
  // WHOLE partial response per token — quadratic over a long answer, and the
  // main thread felt it. Buffer instead and flush at most once per frame; the
  // rendered result is identical because appendStreamToken is a string concat.
  const tokenBufRef = useRef("")
  const rafRef = useRef(null)
  const streamOpenRef = useRef(false)

  useEffect(() => {
    ws.connect()

    // Restore running/pending jobs from API so active jobs survive page refresh
    const restoreJobs = async () => {
      try {
        // ONE request for both statuses (the endpoint takes a comma list).
        // This used to be two, on a load that already fetches jobs twice more
        // for the Activity panel. Limit is 40, not 20, because it now covers
        // what were two separate 20-row windows.
        const { data } = await http.get("/api/jobs", {
          params: { status: "running,pending", limit: 40 },
        })
        const allJobs = data.jobs || []
        // Restore active jobs from API on reconnect
        allJobs.forEach(j => {
          if (!useAppStore.getState().activeJobs[j.id]) {
            const msg = j.current_step || j.title || j.job_type
            // Parse input_json to preserve job metadata (e.g. type: "clip_extraction")
            let inputData = null
            try { inputData = j.input_json ? JSON.parse(j.input_json) : null } catch {}
            // Restore job into global store
            startJob(j.id, j.job_type, msg, { inputData })
            if (j.progress_pct > 0) {
              updateJobProgress(j.id, j.progress_pct, j.current_step || "")
            }
          }
        })
      } catch (err) {
        // Silent fail — jobs will be picked up on next WS message
      }
    }
    restoreJobs()

    // Drain the token buffer into the store. Safe to call at any time; a no-op
    // when empty. MUST be called synchronously before any handler that reads
    // streamingText (finalizeStream falls back to it when full_response is
    // missing) or clears it (chat_error) — otherwise a frame's worth of
    // buffered tokens is dropped, or a pending frame writes text back AFTER the
    // clear.
    const flushTokens = () => {
      if (rafRef.current != null) {
        cancelAnimationFrame(rafRef.current)
        rafRef.current = null
      }
      const buf = tokenBufRef.current
      if (!buf) return
      tokenBufRef.current = ""
      appendStreamToken(buf)
    }

    const endStream = () => {
      flushTokens()
      streamOpenRef.current = false
    }

    const unsubs = [
      ws.on("chat_token", (msg) => {
        // Flip the composer lock once per turn, not once per token.
        if (!streamOpenRef.current) {
          streamOpenRef.current = true
          setStreaming(true)
        }
        tokenBufRef.current += msg.token
        if (rafRef.current == null) {
          rafRef.current = requestAnimationFrame(() => {
            rafRef.current = null
            const buf = tokenBufRef.current
            if (!buf) return
            tokenBufRef.current = ""
            appendStreamToken(buf)
          })
        }
      }),

      ws.on("chat_done", (msg) => {
        endStream()  // land buffered tokens before finalizeStream reads them
        // quick_replies is an optional list emitted when the AI included a
        // <quick_replies> block. Bundling it onto chat_done keeps the chips
        // attached atomically to the message that just finished — no race
        // where the bubble renders before its chips arrive.
        finalizeStream(msg.full_response, msg.quick_replies)
      }),

      // Dispatch-time follow-up from the planner (a clarifying question, a
      // no-op note) — sent AFTER chat_done. Render it as a normal assistant
      // bubble with optional quick-reply chips. Crucially it does NOT call
      // setStreaming: the old path emitted these as chat_token, which flipped
      // isStreaming=true with no matching chat_done and locked the composer
      // forever ("which platform?" → user can't answer). Not persisted here —
      // the backend folds the text into the turn's assistant message.
      ws.on("assistant_message", (msg) => {
        const content = (msg.content || "").trim()
        if (!content) return
        const m = { role: "assistant", content }
        if (Array.isArray(msg.quick_replies) && msg.quick_replies.length > 0) {
          m.quickReplies = msg.quick_replies
        }
        addMessage(m)
      }),

      ws.on("chat_error", (msg) => {
        endStream()
        setStreaming(false)
        clearStreamingText()
        addMessage({ role: "system", content: `Error: ${msg.error}` })
      }),

      ws.on("smart_suggestions", () => {
        // Suggestions only shown on new chat screen, not mid-conversation
      }),

      ws.on("wizard_start", (msg) => {
        setActiveWizard({ id: msg.wizard_id, ...msg.wizard })
      }),

      ws.on("wizard_step_result", () => {
        // Handled by SetupWizard component directly
      }),

      // Session management
      ws.on("session_created", (msg) => {
        const newSession = {
          id: msg.session_id,
          title: msg.title,
          message_count: 0,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        }
        prependSession(newSession)
        setActiveSessionId(msg.session_id)
      }),

      ws.on("session_updated", (msg) => {
        updateSession(msg.session_id, {
          title: msg.title,
          message_count: msg.message_count,
          updated_at: new Date().toISOString(),
        })
      }),

      // Smart Video Studio: a job moved a project (voice take, plan, render).
      // The open editor listens and refetches that project.
      ws.on("studio_project_updated", (msg) => {
        if (!msg?.project_id) return
        window.dispatchEvent(new CustomEvent("vm:studio-updated", { detail: msg }))
      }),

      ws.on("job_started", (msg) => {
        startJob(msg.job_id, msg.job_type, msg.message, { inputData: msg.input_data || null })
        if (isStudioJob(msg.job_type)) return
        addMessage({
          role: "rich",
          type: "job_progress",
          data: { jobId: msg.job_id, jobType: msg.job_type, message: msg.message },
        })
      }),

      ws.on("job_progress", (msg) => {
        updateJobProgress(msg.job_id, msg.percent, msg.step)
      }),

      ws.on("job_complete", (msg) => {
        completeJob(msg.job_id)
        // Auto-remove from sidebar after 10s
        setTimeout(() => removeJob(msg.job_id), 10000)
        if (isStudioJob(msg.job_type || useAppStore.getState().activeJobs[msg.job_id]?.jobType)) return
        const result = msg.result || {}
        // Determine job type from active jobs store
        const jobInfo = useAppStore.getState().activeJobs[msg.job_id]
        const jobType = jobInfo?.type || msg.job_type || ""

        if (result.total_results !== undefined) {
          const total = result.total_results
          const newCount = result.new_results
          let text
          if (jobType === "news_scout") {
            text = total > 0
              ? `News research complete — found **${total}** article${total !== 1 ? "s" : ""}.`
              : "News research complete — no articles scored high enough. Try a different or more specific query."
          } else {
            text = newCount !== undefined && newCount < total
              ? `Scout complete — found **${total}** videos (${newCount} new, ${total - newCount} previously scouted).`
              : `Scout complete — found **${total}** trending videos.`
          }
          addMessage({ role: "system", content: text })
        } else if (result.video) {
          addMessage({
            role: "rich",
            type: "video_preview",
            data: { video: result.video },
          })
        } else if (result.insights) {
          addMessage({
            role: "rich",
            type: "insights",
            data: { videos: result.insights },
          })
        } else {
          addMessage({ role: "system", content: "Job complete!" })
        }
      }),

      ws.on("job_failed", (msg) => {
        const studioJob = isStudioJob(msg.job_type || useAppStore.getState().activeJobs[msg.job_id]?.jobType)
        failJob(msg.job_id, msg.error)
        if (studioJob) {
          showSnackbar(msg.error || "Something in your video didn't finish — try again", "error")
          return
        }
        addMessage({
          role: "system",
          content: `Job failed: ${msg.error || "Unknown error"}`,
        })
        showSnackbar(msg.error || "A job failed — check chat for details", "error")
      }),

      ws.on("channel_analysis", (msg) => {
        addMessage({
          role: "rich",
          type: "channel_summary",
          data: { summary: msg.summary },
        })
      }),

      ws.on("content_calendar", (msg) => {
        addMessage({
          role: "rich",
          type: "content_calendar",
          data: { calendar: msg.calendar || [] },
        })
      }),

      ws.on("downloaded_list", (msg) => {
        addMessage({
          role: "rich",
          type: "downloaded_list",
          data: { videos: msg.videos || [] },
        })
      }),

      ws.on("news_results", (msg) => {
        addMessage({
          role: "rich",
          type: "news_results",
          data: { results: msg.results, query: msg.query, jobId: msg.job_id },
        })
      }),

      ws.on("news_saved", (msg) => {
        addMessage({
          role: "system",
          content: msg.message || `${msg.count} article(s) saved to Library.`,
        })
      }),

      ws.on("scout_results", (msg) => {
        const store = useAppStore.getState()
        const existing = store.scoutResults || []
        const newResults = msg.results || []
        useAppStore.setState({ scoutResults: [...existing, ...newResults] })

        addMessage({
          role: "rich",
          type: "scout_results",
          data: { results: msg.results, platform: msg.platform, jobId: msg.job_id },
        })
      }),

      ws.on("constraint_warning", (msg) => {
        addMessage({
          role: "system",
          content: `${msg.message}`,
          severity: msg.severity,
          wizardId: msg.wizard_id,
        })
        // The backend's own severity: an "info" notice (e.g. "this video had no audio
        // to begin with") must not wear a warning icon. Anything else reads as a warning.
        showSnackbar(msg.message, ["error", "info"].includes(msg.severity) ? msg.severity : "warning")
      }),

      ws.on("morning_digest", (msg) => {
        addMessage({ role: "assistant", content: msg.message })
      }),

      ws.on("action", () => {
        // Actions dispatched by planner backend
      }),
    ]

    return () => {
      unsubs.forEach(fn => fn())
      if (rafRef.current != null) {
        cancelAnimationFrame(rafRef.current)
        rafRef.current = null
      }
      tokenBufRef.current = ""
      streamOpenRef.current = false
    }
  }, [])
}
