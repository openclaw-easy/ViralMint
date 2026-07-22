// Vertical scrollbar hidden at rest, revealed on hover — for the studio
// side-rail columns (Source Videos, sample strip, templates, script panel)
// and the nav sidebar (Layout.jsx).
//
// THE ONE RULE: never toggle scrollbar PRESENCE on hover — only its COLOR.
// The original recipe flipped `scrollbar-width: none → thin` on hover as a
// "Firefox-only" trick, but Chrome 121+ and Safari 18.2+ implement the
// standard property too (and once it's set, engines ignore the
// ::-webkit-scrollbar styles below) — so in any classic-scrollbar
// environment (macOS with a mouse / "Show scroll bars: Always", Windows,
// Linux) the scrollbar popped in and out of the layout and the column's
// cards resized on every hover enter/leave.
//
// Now the gutter is reserved permanently (`thin`) and only the thumb color
// toggles: invisible at rest, gray while the pointer is over the column.
// Overlay-scrollbar environments never reserved space either way. Engines
// without the standard properties fall back to the ::-webkit-* pseudos —
// a constant 6px rail with the same color-only reveal. Spread LAST in an
// sx so it overrides the global (theme.js) always-visible thumb.
export const scrollOnHover = {
  scrollbarWidth: "thin",
  scrollbarColor: "transparent transparent",
  "&:hover": { scrollbarColor: "rgba(128,128,128,0.35) transparent" },
  "&::-webkit-scrollbar": { width: 6 },
  "&::-webkit-scrollbar-track": { background: "transparent" },
  "&::-webkit-scrollbar-thumb": { backgroundColor: "transparent", borderRadius: 3 },
  "&:hover::-webkit-scrollbar-thumb": { backgroundColor: "rgba(128,128,128,0.35)" },
}
