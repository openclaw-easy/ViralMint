import { useEffect } from "react"
import { ws } from "../api/websocket"
import http from "../api/http"
import useAppStore from "../store/appStore"

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

  useEffect(() => {
    ws.connect()

    // Restore running/pending jobs from API so active jobs survive page refresh
    const restoreJobs = async () => {
      try {
        const [runningRes, pendingRes] = await Promise.all([
          http.get("/api/jobs", { params: { status: "running", limit: 20 } }),
          http.get("/api/jobs", { params: { status: "pending", limit: 20 } }),
        ])
        const allJobs = [...(runningRes.data.jobs || []), ...(pendingRes.data.jobs || [])]
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

    const unsubs = [
      ws.on("chat_token", (msg) => {
        setStreaming(true)
        appendStreamToken(msg.token)
      }),

      ws.on("chat_done", (msg) => {
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

      ws.on("job_started", (msg) => {
        startJob(msg.job_id, msg.job_type, msg.message, { inputData: msg.input_data || null })
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
        failJob(msg.job_id, msg.error)
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
        showSnackbar(msg.message, msg.severity === "error" ? "error" : "warning")
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
    }
  }, [])
}
