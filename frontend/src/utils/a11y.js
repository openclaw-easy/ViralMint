// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
/**
 * Keyboard activation for a `role="button"` element that isn't a <button>.
 *
 * A div with `role="button" tabIndex={0}` and only an `onClick` is reachable
 * by Tab and impossible to press: the browser synthesises a click from Enter
 * and Space for real buttons ONLY, so a keyboard user can land on it and
 * nothing happens.
 *
 * Space is preventDefault'ed because its default action is scrolling the page,
 * and Enter for symmetry — matching what a real <button> does.
 *
 *   <Box role="button" tabIndex={0} onClick={use} onKeyDown={onActivate(use)} />
 */
export const onActivate = (fn) => (e) => {
  if (e.key !== "Enter" && e.key !== " ") return
  e.preventDefault()
  fn(e)
}

export default onActivate
