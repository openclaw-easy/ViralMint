// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { useState } from "react"
import { Button } from "@mui/material"
import http from "../../api/http"
import useAppStore from "../../store/appStore"
import { cancelNotice } from "../../utils/cancelJob"

/** Cancel on a running tool page: the same DELETE and the same words as the Activity panel. */
export default function CancelJobButton({ jobId, onCancelled, variant = "text", sx }) {
  const [busy, setBusy] = useState(false)
  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const removeJob = useAppStore((s) => s.removeJob)
  const cancel = async () => {
    setBusy(true)
    try {
      const { data } = await http.delete(`/api/jobs/${jobId}`)
      removeJob(jobId)
      showSnackbar(cancelNotice(data), "info")
      onCancelled?.()
    } catch (e) {
      showSnackbar(e.response?.data?.detail || "Could not cancel that job", "error")
      setBusy(false)
    }
  }
  return (
    <Button size={variant === "text" ? "small" : "medium"} variant={variant} color="inherit" onClick={cancel} disabled={busy || !jobId} sx={{ textTransform: "none", ...sx }}>
      {busy ? "Cancelling…" : "Cancel"}
    </Button>
  )
}
