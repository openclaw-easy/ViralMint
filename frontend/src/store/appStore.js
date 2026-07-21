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

  // Jobs (polling from API — Dashboard uses this)
  jobs: [],
  setJobs: (j) => set({ jobs: j }),

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
