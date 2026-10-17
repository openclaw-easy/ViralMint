import { Typography, Alert } from "@mui/material"

export default function EstimatedCost({ ttsProvider, script }) {
  const wordCount = script ? script.trim().split(/\s+/).filter(Boolean).length : 0
  const estVideoSeconds = wordCount > 0 ? Math.ceil(wordCount / 2.5) : 0

  let ttsCost = 0
  if (ttsProvider === "openai_tts") ttsCost = 0.02

  const total = ttsCost
  return (
    <Alert severity={total === 0 ? "success" : "info"} sx={{ mt: 1.5 }}>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>
        Estimated cost: {total === 0 ? "Free" : `~$${total.toFixed(2)}`}
      </Typography>
      <Typography variant="caption" sx={{ color: "text.secondary", display: "block" }}>
        Voice: {ttsCost === 0 ? "Free (Edge TTS)" : `~$${ttsCost.toFixed(2)}`} · Video: Free (stock footage)
      </Typography>
      {estVideoSeconds > 0 && (
        <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mt: 0.25 }}>
          ~{estVideoSeconds}s video from {wordCount} words
        </Typography>
      )}
    </Alert>
  )
}
