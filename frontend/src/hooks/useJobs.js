import { useEffect, useRef, useState } from "react"
import http from "../api/http"
import useAppStore from "../store/appStore"

// Jobs already arrive over the WebSocket (job_progress / job_complete /
// job_failed). This poll is only a safety net for events missed while the
// socket was down, so it does NOT need to run flat-out forever: it skips while
// the tab is hidden, and backs off hard once nothing is in flight.
const ACTIVE_STATUSES = new Set(["running", "pending", "queued"])
const IDLE_MULTIPLIER = 6   // 5s → 30s when no job is in flight

// Cheap identity check so an unchanged payload doesn't hand the store a fresh
// array and re-render every subscriber on a timer.
function sameJobs(a, b) {
  if (a === b) return true
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    const x = a[i], y = b[i]
    if (x.id !== y.id || x.status !== y.status ||
        x.progress_pct !== y.progress_pct || x.current_step !== y.current_step) {
      return false
    }
  }
  return true
}

/**
 * @param {number} pollInterval  ms between ticks (backs off when idle)
 * @param {number} pageSize      rows per fetch. The Activity panel states
 *   counts and offers "clear all of these", so it asks for a WIDE window —
 *   computing either over the default 20 would quietly lie the moment someone
 *   had more than 20 jobs. It is a parameter rather than a per-call argument
 *   because the interval tick refetches on its own, and a one-off wide fetch
 *   would be overwritten by the next narrow one.
 */
export default function useJobs(pollInterval = 5000, pageSize = 20) {
  const jobs = useAppStore((s) => s.jobs)
  const setJobs = useAppStore((s) => s.setJobs)
  const [jobTotal, setJobTotal] = useState(0)
  const timerRef = useRef(null)
  const tickRef = useRef(0)

  const fetchJobs = async (limit = pageSize, offset = 0) => {
    try {
      const { data } = await http.get("/api/jobs", { params: { limit, offset } })
      const next = data.jobs || []
      const prev = useAppStore.getState().jobs
      if (!sameJobs(prev, next)) setJobs(next)
      setJobTotal(data.total || 0)
      // Repair a tracked job whose terminal WS event never arrived. Only a
      // listing that holds EVERY row (first page, total within it) may
      // declare a tracked job gone; a capped window can only finalize one
      // whose terminal row it actually holds (liveIds=null).
      const complete = offset === 0 && (data.total || 0) <= next.length
      const liveIds = complete
        ? new Set(next.filter((j) => ACTIVE_STATUSES.has(j.status)).map((j) => j.id))
        : null
      useAppStore.getState().reconcileActiveJobs(next, liveIds)
      return next
    } catch (err) {
      console.error("Failed to fetch jobs:", err)
      return null
    }
  }

  useEffect(() => {
    fetchJobs()

    const tick = () => {
      if (document.visibilityState === "hidden") return
      // When nothing is in flight, only poll every IDLE_MULTIPLIER-th tick.
      // "In flight" includes a job the UI is still SHOWING as running (a card,
      // a busy button) — exactly the state a missed terminal event leaves
      // behind, and it should clear in seconds, not at the idle rate.
      const st = useAppStore.getState()
      const anyActive = st.jobs.some((j) => ACTIVE_STATUSES.has(j.status)) ||
        Object.values(st.activeJobs).some((j) => j.status === "running")
      tickRef.current = anyActive ? 0 : tickRef.current + 1
      if (anyActive || tickRef.current >= IDLE_MULTIPLIER) {
        tickRef.current = 0
        fetchJobs()
      }
    }
    timerRef.current = setInterval(tick, pollInterval)

    // Catch up immediately on tab re-focus rather than waiting out a tick.
    const onVisible = () => {
      if (document.visibilityState === "visible") {
        tickRef.current = 0
        fetchJobs()
      }
    }
    document.addEventListener("visibilitychange", onVisible)

    return () => {
      clearInterval(timerRef.current)
      document.removeEventListener("visibilitychange", onVisible)
    }
  }, [pollInterval])

  return { jobs, jobTotal, fetchJobs }
}

// activityFromJob JSON.parses input_json + output_json per row; mapping 200
// rows on every store change re-parsed all of them. Cache per row, keyed on
// the fields the row's display can depend on, so only rows that changed are
// re-derived.
const _activityCache = new Map()  // id -> { sig, value, fn }
function rowSig(j) {
  return [j.status, j.progress_pct, j.current_step, j.title, j.error_message,
    j.started_at, j.completed_at, j.job_type,
    j.input_json?.length ?? -1, j.output_json?.length ?? -1].join("\u0001")
}
export function mapActivity(jobs, toActivity) {
  const seen = new Set()
  const out = jobs.map((j) => {
    seen.add(j.id)
    const sig = rowSig(j)
    const hit = _activityCache.get(j.id)
    if (hit && hit.sig === sig && hit.fn === toActivity) return hit.value
    const value = toActivity(j)
    _activityCache.set(j.id, { sig, value, fn: toActivity })
    return value
  })
  for (const id of _activityCache.keys()) if (!seen.has(id)) _activityCache.delete(id)
  return out
}
