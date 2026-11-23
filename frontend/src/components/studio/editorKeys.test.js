// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { describe, expect, it } from "vitest"

import { controlOf } from "./editorKeys"

function el(html) {
  const host = document.createElement("div")
  host.innerHTML = html
  document.body.appendChild(host)
  return host
}

describe("controlOf", () => {
  it("a real button and a focusable tile own their keys", () => {
    const host = el('<button id="b">Use</button><div role="button" id="t"><span id="in">tile</span></div>')
    expect(controlOf(host.querySelector("#b"))).toBe(host.querySelector("#b"))
    expect(controlOf(host.querySelector("#in"))).toBe(host.querySelector("#t"))
  })
  it("the selected scene's timeline block is the scene, not a control", () => {
    const host = el('<div role="button" data-scene="2" aria-pressed="true" id="sel"></div>' +
                    '<div role="button" data-scene="3" aria-pressed="false" id="other"></div>')
    expect(controlOf(host.querySelector("#sel"))).toBeNull()
    expect(controlOf(host.querySelector("#other"))).toBe(host.querySelector("#other"))
  })
  it("the page itself is nobody's control", () => {
    expect(controlOf(document.body)).toBeNull()
    expect(controlOf(null)).toBeNull()
  })
})
