import { create } from "zustand"

const useAppStore = create((set, get) => ({
  // ── Chat sessions ───────────────────────────────────────────────────────
  sessions: [],          // [{id, title, message_count, created_at, updated_at}]
  activeSessionId: null,
  messages: [],
  isStreaming: false,
  streamingText: "",

  setSessions: (sessions) => set({ sessions }),
  setActiveSessionId: (id) => {
    if (id) sessionStorage.setItem("vm_active_session", id)
    else sessionStorage.removeItem("vm_active_session")
    set({ activeSessionId: id })
  },

  addMessage: (msg) => set((s) => ({ messages: [...s.messages, msg] })),
  setMessages: (msgs) => set({ messages: msgs }),
  setStreaming: (v) => set({ isStreaming: v }),
  appendStreamToken: (token) => set((s) => ({ streamingText: s.streamingText + token })),
  // quickReplies is optional — when the planner emits a <quick_replies> block,
  // the chips render below the just-finished assistant bubble. They live only
  // in the in-memory message (not persisted), so they vanish on session reload
  // (intended — the conversation has moved on).
  finalizeStream: (fullText, quickReplies) => {
    const cleaned = fullText || get().streamingText
    const msg = { role: "assistant", content: cleaned }
    if (Array.isArray(quickReplies) && quickReplies.length > 0) {
      msg.quickReplies = quickReplies
    }
    set((s) => ({
      messages: [...s.messages, msg],
      streamingText: "",
      isStreaming: false,
    }))
  },
  clearStreamingText: () => set({ streamingText: "" }),

  // Update a session in the list (title, message_count)
  updateSession: (sessionId, updates) => set((s) => ({
    sessions: s.sessions.map(sess =>
      sess.id === sessionId ? { ...sess, ...updates } : sess
    ),
  })),

  // Add a session to the top of the list
  prependSession: (session) => set((s) => ({
    sessions: [session, ...s.sessions.filter(ss => ss.id !== session.id)],
  })),

  // Remove a session from the list
  removeSession: (sessionId) => set((s) => ({
    sessions: s.sessions.filter(ss => ss.id !== sessionId),
    activeSessionId: s.activeSessionId === sessionId ? null : s.activeSessionId,
    messages: s.activeSessionId === sessionId ? [] : s.messages,
  })),

  // Smart suggestions
  suggestions: [],
  setSuggestions: (s) => set({ suggestions: s }),

  // Active wizard
  activeWizard: null,
  setActiveWizard: (w) => set({ activeWizard: w }),

  // Motion Graphics plugin opt-in state. Set by the Settings card and by the
  // route gate, so the sidebar entry and the Tools card can reflect "installed"
  // without each of them polling the same endpoint. `null` = not yet known,
  // which is deliberately distinct from `false`: an entry that flashes
  // "not installed" for one frame on every page load reads as broken.
  motionInstalled: null,
  setMotionInstalled: (v) => set({ motionInstalled: v }),

  // Jobs (polling from API — Dashboard uses this)
  jobs: [],
  setJobs: (j) => set({ jobs: j }),

  // ── Activity panel ────────────────────────────────────────────────────────
  // The job log lives in a panel any surface can open, because jobs start from
  // Stock Video, a tool page, the Clipper and the Motion studio alike — it used
  // to be a Library tab, reachable from exactly one route. The sidebar's
  // running-jobs pill and the Library's Activity button both flip this.
  activityOpen: false,
  openActivity: () => set({ activityOpen: true }),
  closeActivity: () => set({ activityOpen: false }),

  // Active jobs — real-time via WebSocket, persists across page nav
  // { [jobId]: { jobId, jobType, status, percent, step, message, startedAt } }
  activeJobs: {},
  startJob: (jobId, jobType, message, meta) => set((s) => ({
    activeJobs: {
      ...s.activeJobs,
      [jobId]: { jobId, jobType, status: "running", percent: 0, step: message || "", message, startedAt: Date.now(), ...(meta || {}) },
    },
  })),
  updateJobProgress: (jobId, percent, step) => set((s) => {
    const existing = s.activeJobs[jobId]
    if (!existing) return {}
    return { activeJobs: { ...s.activeJobs, [jobId]: { ...existing, percent, step } } }
  }),
  completeJob: (jobId) => set((s) => {
    const existing = s.activeJobs[jobId]
    if (!existing) return {}
    return { activeJobs: { ...s.activeJobs, [jobId]: { ...existing, status: "success", percent: 100 } } }
  }),
  failJob: (jobId, error) => set((s) => {
    const existing = s.activeJobs[jobId]
    if (!existing) return {}
    return { activeJobs: { ...s.activeJobs, [jobId]: { ...existing, status: "failed", step: error || "Failed" } } }
  }),
  removeJob: (jobId) => set((s) => {
    const { [jobId]: _, ...rest } = s.activeJobs
    return { activeJobs: rest }
  }),

  // Poll-driven safety net for a MISSED terminal WS event. A job whose
  // job_failed / job_complete never arrived (socket blip, or a runner that
  // wrote "failed" without announcing it) stayed "running" here — a chat
  // card, the Activity pill, a busy button — until a page reload. useJobs
  // calls this on every poll. Quiet by design — state only, no toasts or chat
  // lines (the live WS handlers own those; a reconnect must not replay a
  // burst of them).
  //   rows    — job rows the poll just saw (any status)
  //   liveIds — Set of ids the server reports as running/pending/queued, from
  //             an AUTHORITATIVE (uncapped) listing; null when the caller only
  //             has a capped window, and then a missing row is never treated
  //             as gone (it may just be outside the window)
  reconcileActiveJobs: (rows, liveIds = null, now = Date.now()) => {
    const state = get()
    const byId = new Map((rows || []).map((r) => [r.id, r]))
    Object.values(state.activeJobs).forEach((job) => {
      if (job.status !== "running") return
      // Grace: a just-dispatched job's row may not be queryable yet.
      if (now - (job.startedAt || 0) < 15000) return
      if (liveIds && liveIds.has(job.jobId)) return
      const row = byId.get(job.jobId)
      if (row?.status === "failed") state.failJob(job.jobId, row.error_message)
      else if (row?.status === "success") state.completeJob(job.jobId)
      else if (row?.status === "cancelled") state.removeJob(job.jobId)
      else if (!row && liveIds) state.removeJob(job.jobId)
    })
  },


  // Settings
  settings: null,
  setSettings: (s) => set({ settings: s }),

  // Scout results
  scoutResults: [],
  setScoutResults: (r) => set({ scoutResults: r }),

  // Global snackbar (action: optional { label, href } for a clickable link)
  snackbar: { open: false, message: "", severity: "info", action: null },
  showSnackbar: (message, severity = "info", action = null) => set({ snackbar: { open: true, message, severity, action } }),
  closeSnackbar: () => set((s) => ({ snackbar: { ...s.snackbar, open: false } })),
}))

export default useAppStore
