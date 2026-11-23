// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { useCallback, useEffect, useRef, useState } from "react"
import * as api from "./studioApi"

// The one owner of a Studio project's state: load, serialised edits with
// revision checks, undo/redo as a doc-snapshot stack (D19), and the background
// calls (voice, plan, render) whose jobs write back into the project.
//
// Jobs finish in the background. The backend announces `studio_project_updated`
// over the WebSocket (forwarded as the `vm:studio-updated` window event); this
// hook refetches on it, and polls while anything is generating or rendering in
// case an event is missed.
export const renderInProgress = (project) =>
  project?.status === "rendering" && project.render_alive !== false

export default function useStudioProject(projectId, { onError } = {}) {
  const [project, setProject] = useState(null)
  const [loading, setLoading] = useState(true)
  const [notFound, setNotFound] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState(null)
  const [, setHistTick] = useState(0)
  const projRef = useRef(null)
  const past = useRef([])
  const future = useRef([])
  const chain = useRef(Promise.resolve())
  const onErrorRef = useRef(onError)
  onErrorRef.current = onError

  // Responses (edits, job updates, refetches) can arrive out of order — never
  // let an older revision replace a newer one.
  const setP = useCallback((p) => {
    const cur = projRef.current
    if (!p || (cur && cur.id === p.id && p.revision < cur.revision)) return
    projRef.current = p
    setProject(p)
  }, [])

  // A load that failed for any reason OTHER than 404 (a 5xx, the network, a
  // timeout) is not "this project doesn't exist" — the page offers a retry
  // instead of telling the user their project is gone (audit 2026-09-19).
  const [loadError, setLoadError] = useState(null)
  const [loadAttempt, setLoadAttempt] = useState(0)
  const reload = useCallback(() => setLoadAttempt((n) => n + 1), [])
  useEffect(() => {
    let alive = true
    setLoading(true)
    setNotFound(false)
    setLoadError(null)
    projRef.current = null
    past.current = []
    future.current = []
    api.getProject(projectId)
      .then((p) => { if (alive) setP(p) })
      .catch((e) => {
        if (!alive) return
        if (e.status === 404) setNotFound(true)
        else { setLoadError(e.message || "Couldn't load this project."); onErrorRef.current?.(e.message, e) }
      })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [projectId, setP, loadAttempt])

  const refresh = useCallback(async () => {
    const cur = projRef.current
    if (!cur) return
    try {
      setP(await api.getProject(cur.id))
    } catch (e) {
      if (e.status === 404) setNotFound(true)
    }
  }, [setP])

  useEffect(() => {
    const onUpdate = (event) => {
      const detail = event.detail || {}
      const cur = projRef.current
      if (cur && detail.project_id === cur.id && (detail.revision == null || detail.revision > cur.revision)) refresh()
    }
    window.addEventListener("vm:studio-updated", onUpdate)
    return () => window.removeEventListener("vm:studio-updated", onUpdate)
  }, [refresh])

  // A `rendering` claim whose job is gone (the backend died mid-render) is not
  // work in progress: the envelope says so with `render_alive` (absent on older
  // backends → trust the status).
  // "My audio": while the app listens the project has no scenes at all (`derived.audio_listening`).
  const busy = !!project && (renderInProgress(project) || !!project.derived?.audio_listening || project.doc.scenes.some(
    (s) => s.voice?.state === "generating" || s.visual?.state === "generating"))
  // The poll asks "changed?" first (a few bytes) and fetches the whole project
  // only when the revision — or the render's liveness — moved.
  const poll = useCallback(async () => {
    const cur = projRef.current
    if (!cur) return
    try {
      const res = await api.pollProject(cur.id, cur.revision)
      if (!res.unchanged) { setP(res); return }
      // A listening that fails moves no revision: the cheap poll's `audio_alive` is what ends "Listening…".
      if (res.status !== cur.status || (res.render_alive ?? null) !== (cur.render_alive ?? null)
          || !!res.audio_alive !== !!cur.derived?.audio_listening) await refresh()
    } catch (e) {
      if (e.status === 404) setNotFound(true)
    }
  }, [refresh, setP])
  useEffect(() => {
    if (!busy) return undefined
    const timer = setInterval(poll, 1500)
    return () => clearInterval(timer)
  }, [busy, poll])

  const bumpHist = () => setHistTick((x) => x + 1)

  // One write, rebased once onto the server's latest if it moved meanwhile.
  // Returns the response and the document the ops were applied ON — after a
  // rebase that is the server's latest, not the stale local copy, so undo
  // restores what was really there (a take or visual landed meanwhile stays).
  const send = async (ops) => {
    const cur = projRef.current
    try {
      return { res: await api.applyOps(cur.id, cur.revision, ops), base: cur }
    } catch (e) {
      if (e.code !== "revision_conflict") throw e
      const latest = e.project || await api.getProject(cur.id)
      setP(latest)
      return { res: await api.applyOps(cur.id, latest.revision, ops), base: latest }
    }
  }

  const enqueue = (run) => {
    const p = chain.current.then(run, run)
    chain.current = p
    return p
  }

  const unsaved = useRef(null)
  const retryTimer = useRef(null)
  const retrySaveRef = useRef(null)
  useEffect(() => () => clearTimeout(retryTimer.current), [])
  const apply = useCallback((ops, { record = true } = {}) => enqueue(async () => {
    const cur = projRef.current
    if (!cur || !ops?.length) return null
    setSaving(true)
    try {
      const { res, base } = await send(ops)
      // A write that changed nothing (a blur echoing ⌘↵) is not an undo step.
      const moved = res.changes.length > 0 || res.created.length > 0
      if (record && moved) {
        past.current.push(base.doc)
        if (past.current.length > 50) past.current.shift()
        future.current = []
        bumpHist()
      }
      setSaveError(null)
      setP(res.project)
      return res
    } catch (e) {
      // A 4xx is a rejected edit (the message says why — a toast). Anything
      // else is a save that didn't happen: the TopBar's "Not saved" indicator
      // says so and stays until a save lands, so no toast on top of it — it
      // used to add a raw "Network Error" beside the indicator.
      if (!(e.status >= 400 && e.status < 500)) {
        setSaveError(e.message)
        // Kept to send again (TopBar "Retry", and once by itself shortly) —
        // a failed save used to drop the edit for good (2026-10-13 audit).
        unsaved.current = { ops, record }
        if (!retryTimer.current) retryTimer.current = setTimeout(() => { retryTimer.current = null; retrySaveRef.current?.() }, 3000)
      } else onErrorRef.current?.(e.message, e)
      return null
    } finally {
      setSaving(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [setP])

  // Send the edit that didn't save, once more (it rebases like any write).
  const retrySave = useCallback(() => {
    const pending = unsaved.current
    if (!pending) return null
    unsaved.current = null
    clearTimeout(retryTimer.current); retryTimer.current = null
    return apply(pending.ops, { record: pending.record })
  }, [apply])
  retrySaveRef.current = retrySave

  // Undo/redo run IN the queue and put the snapshot back if the server refuses it.
  const travel = useCallback((from, to) => enqueue(async () => {
    const target = from.current.pop()
    const cur = projRef.current
    if (!target || !cur) { if (target) from.current.push(target); return null }
    setSaving(true)
    try {
      // After a rebase the document the snapshot replaced is the server's
      // latest, not the local copy — that is what the opposite stack restores.
      const { res, base } = await send([{ op: "replace_doc", doc: target }])
      to.current.push(base.doc)
      setSaveError(null)
      setP(res.project)
      return res
    } catch (e) {
      from.current.push(target)
      onErrorRef.current?.(e.message, e)
      return null
    } finally {
      setSaving(false)
      bumpHist()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [setP])

  const undo = useCallback(() => travel(past, future), [travel])
  const redo = useCallback(() => travel(future, past), [travel])

  // Voice takes run as a job; the envelope that comes back marks the scenes
  // as generating, and the job writes each take into the project.
  const voice = useCallback(async (uids) => {
    try {
      const res = await api.voiceScenes(projRef.current.id, uids)
      if (res?.project) setP(res.project)
      return res
    } catch (e) {
      onErrorRef.current?.(e.message, e)
      return null
    }
  }, [setP])

  const plan = useCallback(async ({ firstDraft = false, sceneUids = null } = {}) => {
    try {
      return await api.planProject(projRef.current.id, { firstDraft, sceneUids })
    } catch (e) {
      onErrorRef.current?.(e.message, e)
      return null
    }
  }, [])

  const render = useCallback(async () => {
    try {
      const res = await api.renderProject(projRef.current.id)
      refresh()
      return res
    } catch (e) {
      if (e.code === "blocked") return { blocked: e.detail?.blockers || [e.message] }
      onErrorRef.current?.(e.message, e)
      return null
    }
  }, [refresh])

  return {
    project, loading, notFound, loadError, reload, saving, saveError,
    canUndo: past.current.length > 0,
    canRedo: future.current.length > 0,
    apply, undo, redo, voice, plan, render, refresh, retrySave,
    adopt: setP,
  }
}
