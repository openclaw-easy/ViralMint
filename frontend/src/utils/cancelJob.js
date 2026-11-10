// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
/** What a cancel did, in the words every surface uses. Cancellation is cooperative: the row flips at
 *  once, but a transfer or ffmpeg pass already in flight finishes on its own. Saying only
 *  "Job cancelled" reads as a completed stop and invites a duplicate. */
export const cancelNotice = (data) => (data?.best_effort
  ? "Cancelling — the step already running will finish on its own"
  : "Job cancelled")
