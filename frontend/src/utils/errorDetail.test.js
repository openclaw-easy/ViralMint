// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
import { describe, expect, it } from "vitest"
import { errorDetailText } from "./errorDetail"

describe("errorDetailText", () => {
  it("passes a string detail through", () => {
    expect(errorDetailText({ response: { data: { detail: "Couldn't write to that folder: Permission denied" } } }))
      .toBe("Couldn't write to that folder: Permission denied")
  })
  it("joins a validation list's messages instead of printing [object Object]", () => {
    const err = { response: { data: { detail: [{ msg: "field required" }, { msg: "not a folder" }] } } }
    expect(errorDetailText(err)).toBe("field required; not a folder")
  })
  it("reads a {code, message} detail", () => {
    expect(errorDetailText({ response: { data: { detail: { code: "x", message: "Nope" } } } })).toBe("Nope")
  })
  it("falls back to the error's own message", () => {
    expect(errorDetailText({ message: "Network Error" })).toBe("Network Error")
    expect(errorDetailText({ response: { data: { detail: [] } }, message: "Bad" })).toBe("Bad")
  })
})
