// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors

// The words in a failed request, for a toast. FastAPI answers a refusal with
// `detail` as a string, and a validation error with a LIST of {msg, …}: put
// straight into a template that list reads "[object Object]".
export function errorDetailText(err) {
  const d = err?.response?.data?.detail
  if (typeof d === "string" && d) return d
  if (Array.isArray(d)) {
    const msgs = d.map((x) => x?.msg).filter(Boolean)
    if (msgs.length) return msgs.join("; ")
  }
  if (d && typeof d === "object" && typeof d.message === "string" && d.message) return d.message
  return err?.message || "Request failed"
}
