// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { describe, it, expect } from "vitest"
import {
  aspectCss, autoRevoices, clipInUse, hashString,
  musicLabel, musicTrackName, queryGradient, stockLabel, voiceKindLabel, withTimeline,
} from "./studioModel"

describe("musicLabel — one label for every surface", () => {
  it("names the track the bed actually plays", () => {
    expect(musicLabel({ enabled: false, track: "a.mp3" })).toBe("Off")
    expect(musicLabel({ enabled: true, source: "library", track: "lofi_a.mp3", track_name: "a" })).toBe("a")
    expect(musicLabel({ enabled: true, source: "library" })).toBe("No track picked")
  })
  it("no track name while the bed is off or nothing is picked", () => {
    expect(musicTrackName({ enabled: false, track: "a.mp3" })).toBeNull()
    expect(musicTrackName({ enabled: true })).toBeNull()
    expect(musicTrackName({ enabled: true, track: "lofi_a.mp3" })).toBe("lofi_a.mp3")
  })
})

describe("voices — which provider re-records by itself", () => {
  it("Edge (local) re-voices after an edit; OpenAI (your key) waits to be asked", () => {
    expect(autoRevoices("edge_tts")).toBe(true)
    expect(autoRevoices(undefined)).toBe(true)
    expect(autoRevoices("openai_tts")).toBe(false)
    expect(voiceKindLabel("openai_tts")).toBe("OpenAI voice")
    expect(voiceKindLabel("edge_tts")).toBe("Edge voice")
  })
})

describe("withTimeline", () => {
  const doc = {
    settings: { tail_gap_s: 0.15 },
    scenes: [
      { uid: "a", line: "One two three four five six.", voice: { state: "none" }, visual: {} },
      { uid: "b", line: "Seven.", voice: { state: "ready", duration: 2 }, visual: {} },
    ],
  }
  it("uses the server's derived durations for the same document (one estimate)", () => {
    const t = withTimeline(doc, { durations: [3.15, 2.15], total: 5.3 })
    expect(t.scenes.map((s) => s.dur)).toEqual([3.15, 2.15])
    expect(t.scenes[1].start).toBeCloseTo(3.15)
    expect(t.scenes[0].estimated).toBe(true)
  })
  it("falls back to its own estimate when the server's timeline doesn't match the scenes", () => {
    const t = withTimeline(doc, { durations: [9] })
    expect(t.scenes[1].dur).toBeCloseTo(2.15)
  })
})

describe("presentation helpers", () => {
  it("stock labels, gradients and aspect CSS", () => {
    expect(stockLabel({ provider: "pexels", creator: "Jo" })).toBe("Pexels · Jo")
    expect(stockLabel({ provider: "pexels" })).toBe("Pexels")
    expect(stockLabel({ provider: "pixabay", creator: "Mo" })).toBe("Pixabay · Mo")
    expect(hashString("abc")).toBe(hashString("abc"))
    expect(queryGradient("x")).toMatch(/^linear-gradient\(135deg, hsl\(/)
    expect(aspectCss("16:9")).toBe("16 / 9")
    expect(aspectCss("4:3")).toBe("9 / 16")
  })
})

describe("clipInUse — is this card what the scene already shows", () => {
  const stock = (asset) => ({ visual: { kind: "stock", asset } })
  it("stock: same provider and id, whatever the id's type", () => {
    expect(clipInUse(stock({ provider: "pexels", provider_id: 7 }), { provider: "pexels", provider_id: "7" })).toBe(true)
    expect(clipInUse(stock({ provider: "pexels", provider_id: 7 }), { provider: "pixabay", provider_id: 7 })).toBe(false)
    expect(clipInUse(stock({ provider: "pexels", provider_id: 7 }), { provider: "pexels", provider_id: 8 })).toBe(false)
  })
  it("your own clip: same asset id, and never a stock card", () => {
    const mine = { visual: { kind: "user_clip", asset: { asset_id: "a1" } } }
    expect(clipInUse(mine, { asset_id: "a1" })).toBe(true)
    expect(clipInUse(mine, { asset_id: "a2" })).toBe(false)
    expect(clipInUse(mine, { provider: "pexels", provider_id: 7 })).toBe(false)
  })
  it("nothing bound, or no scene, is never in use", () => {
    expect(clipInUse(null, { provider: "pexels", provider_id: 7 })).toBe(false)
    expect(clipInUse(stock(null), { provider: "pexels", provider_id: 7 })).toBe(false)
    expect(clipInUse(stock({ provider: "pexels" }), { provider: "pexels" })).toBe(false)
  })
})
