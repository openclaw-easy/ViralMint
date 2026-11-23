// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
// "My audio": the editor's rules for a project made from the user's own recording.
// The server enforces every rule here too (studio_ops refuses split / merge / insert / a voice change).
export const isAudioMode = (doc) => doc?.input?.mode === "audio"
export const isOwnAudio = (scene) => scene?.voice?.provider === "own_audio"
export const audioListening = (project) => !!project?.derived?.audio_listening
export const AUDIO_VOICE_LABEL = "Your audio"
export const AUDIO_LINE_HINT = "Edits change the captions, not the audio."
export const AUDIO_NO_SPLIT = "Your audio is cut at its sentences, so scenes can't be split or merged here. Edit the words, reorder or delete a scene instead."
export const AUDIO_NO_INSERT = "Every scene here is a sentence of your audio, so a new scene can't be added."
export const fmtSeconds = (s) => {
  const t = Math.max(0, Math.round(Number(s) || 0))
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`
}
