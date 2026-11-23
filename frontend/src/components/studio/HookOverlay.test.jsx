// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"

vi.mock("./studioMedia", () => ({
  useSceneMedia: () => ({ url: null, image: false, failed: false, retry: vi.fn() }),
  useCandidateUrl: () => ({ url: null, failed: false, retry: vi.fn() }),
}))
vi.mock("./studioApi", () => ({
  mediaUrl: (p, a) => `/media/${p}/${a}`,
  voiceUrl: (k) => `/voice/${k}`,
  takeMeta: vi.fn(() => Promise.resolve({ words: null })),
}))

import SequencePlayer from "./SequencePlayer"
import { HOOK_SECONDS, autoTitle, clipTitle, hookTitle, splitSentences } from "./studioModel"

const timeline = {
  total: 6,
  scenes: [
    { uid: "a", index: 0, start: 0, dur: 3, line: "Stop guessing what to post", voice: { state: "none" }, visual: { kind: "stock", query: "q" } },
    { uid: "b", index: 1, start: 3, dur: 3, line: "Second line here", voice: { state: "none" }, visual: { kind: "stock", query: "q" } },
  ],
}
const props = (captions, playhead = 0.5) => ({
  timeline, settings: { captions }, aspectRatio: "9:16", playhead, playing: false,
  onTick: vi.fn(), onPlayingChange: vi.fn(), onSeek: vi.fn(), preview: null, onClosePreview: vi.fn(),
  onUsePreview: vi.fn(), selectedIndex: 0, voiceMuted: false, onToggleVoice: vi.fn(), musicMuted: false,
  onToggleMusic: vi.fn(), loopScene: false, onToggleLoop: vi.fn(), onPrev: vi.fn(), onNext: vi.fn(),
  onTogglePlay: vi.fn(), projectId: "p1",
})

beforeEach(() => {
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    width: 360, height: 640, top: 0, left: 0, right: 360, bottom: 640, x: 0, y: 0,
  })
})
afterEach(() => vi.restoreAllMocks())

describe("hookTitle — mirrors studio_doc.hook_title", () => {
  it("uses the user's text, else the first spoken line, only while on", () => {
    const scenes = [{ line: "  " }, { line: " First   real line " }]   // no clause break: whole line
    expect(hookTitle({ captions: { enabled: true, hook: true } }, scenes)).toBe("First real line")
    expect(hookTitle({ captions: { enabled: true, hook: true, hook_text: " Mine " } }, scenes)).toBe("Mine")
    expect(hookTitle({ captions: { enabled: true, hook: false } }, scenes)).toBeNull()
    expect(hookTitle({ captions: { enabled: false, hook: true } }, scenes)).toBeNull()
    expect(hookTitle({ captions: { enabled: true, hook: true, hook_text: "x".repeat(200) } }, scenes)).toHaveLength(80)
  })

  it("a title made from the first line is its opening clause (studio_doc.auto_title cases)", () => {
    const cases = [
      ["Most people save money the violet84 wrong way, because they wait until the end of the month.",
        "Most people save money the violet84 wrong way"],
      ["Stop guessing what to post.", "Stop guessing what to post"],
      ["Why do cats purr?", "Why do cats purr?"],
      ["Your bread isn't the problem, your yeast is. Here is why.", "Your bread isn't the problem, your yeast is"],
      ["Here are three amazing tips to grow your video channel fast and easy today",
        "Here are three amazing tips to grow your video…"],
      ["5 habits.", "5 habits"],
      ["你是不是也在浪费钱，每个月都存不下来？", "你是不是也在浪费钱，每个月都存不下来？"],
    ]
    for (const [line, title] of cases) expect(autoTitle(line)).toBe(title)
  })
  it("never cuts the title at a comma or point inside a number (studio_doc._clause_end)", () => {
    expect(autoTitle("Honeybees live in colonies of up to 60,000 workers.")).toBe("Honeybees live in colonies of up to 60,000…")
    expect(autoTitle("It costs 3.5 dollars a day to keep a hive alive, and here is why.")).toBe("It costs 3.5 dollars a day to keep a hive alive")
  })
  it("splits sentences only before whitespace, so numbers and links stay whole (studio_doc.split_sentences)", () => {
    expect(splitSentences("It costs 3.5 dollars a day. The hive holds 60,000 bees."))
      .toEqual(["It costs 3.5 dollars a day.", "The hive holds 60,000 bees."])
    expect(splitSentences("Wait... what? I paid $9.99, not $10. Go to example.com now!"))
      .toEqual(["Wait...", "what?", "I paid $9.99, not $10.", "Go to example.com now!"])
    expect(splitSentences("你好。世界！好吗？ OK.")).toEqual(["你好。", "世界！", "好吗？", "OK."])
  })

  it("cuts a long line at a word, exactly like studio_doc.clip_title", () => {
    const line = "Most people save money the wrong way, because they wait until the end of the month to see what is left"
    expect(clipTitle(line)).toBe("Most people save money the wrong way, because they wait until the end of the…")
    expect(clipTitle("x".repeat(300))).toBe("x".repeat(79) + "…")
  })
})

describe("SequencePlayer — the opening title", () => {
  it("shows the title over the first seconds, as the render burns it", () => {
    render(<SequencePlayer {...props({ enabled: true, style: "viral", hook: true })} />)
    expect(screen.getByTestId("hook-overlay").textContent).toBe("Stop guessing what to post")
  })

  it("is fully visible at 0:00, where the editor opens paused", () => {
    render(<SequencePlayer {...props({ enabled: true, style: "viral", hook: true }, 0)} />)
    expect(Number(getComputedStyle(screen.getByTestId("hook-overlay")).opacity)).toBe(1)
  })

  it("is gone after HOOK_SECONDS and absent when the hook is off", () => {
    const { unmount } = render(<SequencePlayer {...props({ enabled: true, style: "viral", hook: true }, HOOK_SECONDS + 1)} />)
    expect(screen.queryByTestId("hook-overlay")).toBeNull()
    unmount()
    render(<SequencePlayer {...props({ enabled: true, style: "viral", hook: false })} />)
    expect(screen.queryByTestId("hook-overlay")).toBeNull()
  })
})
