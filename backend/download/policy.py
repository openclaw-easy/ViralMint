# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""What to do after a failed attempt: one table, one budget.

`decide(error_class, state)` is pure. The engine (and the tests, with no
network) call it after every failed attempt, carry out the returned `Step`,
then call `state.record(error_class, step)`.

Every row ends: a recovery is tried at most once, waits come from a fixed
list, and the `Budget` bounds attempts and waiting across ALL formats and
recoveries of one download. When the budget is spent, the answer is the
class's final answer (escalate or stop), whatever the row would have said.

"Escalate" means the yt-dlp engine has no way into this page (no extractor,
a bot check, a 403 / 404 it couldn't get past); it ends the download with
ExtractorMissingError. A cancel is not in this table: the engine checks it before each step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from backend.download.classify import ErrorClass as E


class StepKind(str, Enum):
    RETRY = "retry"              # same format again, after `wait_s` and `do`
    NEXT_FORMAT = "next_format"  # move down the format chain
    ESCALATE = "escalate"        # yt-dlp can't; a browser might
    STOP = "stop"                # the download ends with `outcome`


class Outcome(str, Enum):
    COOKIES_UNREADABLE = "cookies_unreadable"
    UNAVAILABLE = "unavailable"
    SIGN_IN = "sign_in"
    RATE_LIMITED = "rate_limited"
    NETWORK = "network"
    FAILED = "failed"


# Things the engine does to the attempt's options before the next try.
DROP_COOKIES = "drop_cookies"
REFRESH_COOKIES = "refresh_cookies"
DROP_SUBTITLES = "drop_subtitles"
DROP_EXTRAS = "drop_extras"       # the optional embed postprocessors
MANUAL_MERGE = "manual_merge"     # try ffmpeg on the streams already on disk; success ends the download
MARK_WAITED_403 = "waited_403"    # a marker only: the engine does nothing for it


@dataclass(frozen=True)
class Step:
    kind: StepKind
    wait_s: float = 0.0
    do: tuple[str, ...] = ()
    outcome: Outcome | None = None


@dataclass
class Budget:
    max_attempts: int
    max_wait_s: float
    attempts: int = 1          # the attempt that just failed is the first
    waited_s: float = 0.0

    @classmethod
    def video(cls) -> "Budget":
        return cls(max_attempts=8, max_wait_s=120.0)

    @classmethod
    def audio(cls) -> "Budget":
        return cls(max_attempts=5, max_wait_s=60.0)

    @classmethod
    def probe(cls) -> "Budget":
        return cls(max_attempts=3, max_wait_s=0.0)

    def allows(self, wait_s: float = 0.0) -> bool:
        return self.attempts < self.max_attempts and self.waited_s + wait_s <= self.max_wait_s


@dataclass
class LadderState:
    """What one download has already tried."""
    budget: Budget
    formats_left: int = 0            # rungs BELOW the current one
    has_cookies: bool = False        # the attempt's options carry a cookie jar
    can_refresh_cookies: bool = False
    subtitles_on: bool = False
    extras_attached: bool = False
    done: set[str] = field(default_factory=set)        # recoveries already used
    seen: dict[E, int] = field(default_factory=dict)   # failures per class, this download
    unknown_on_format: int = 0       # UNKNOWN failures on the current format

    def record(self, error_class: E, step: Step) -> None:
        """Apply a step the engine just carried out."""
        self.seen[error_class] = self.seen.get(error_class, 0) + 1
        if error_class in (E.UNKNOWN, E.POSTPROCESSING) and not step.do:
            self.unknown_on_format += 1
        self.done.update(step.do)
        if DROP_COOKIES in step.do:
            self.has_cookies = False
        if DROP_SUBTITLES in step.do:
            self.subtitles_on = False
        if DROP_EXTRAS in step.do:
            self.extras_attached = False
        if step.kind in (StepKind.RETRY, StepKind.NEXT_FORMAT):
            self.budget.attempts += 1
            self.budget.waited_s += step.wait_s
        if step.kind is StepKind.NEXT_FORMAT:
            self.formats_left -= 1
            self.unknown_on_format = 0


_ESCALATES = frozenset({E.HTTP_403, E.HTTP_404, E.ANTI_BOT_CHALLENGE, E.UNSUPPORTED_URL, E.NO_FORMATS})
_OUTCOME = {
    E.COOKIE_SOURCE: Outcome.COOKIES_UNREADABLE,
    E.AGE_GATE: Outcome.UNAVAILABLE,
    E.UNAVAILABLE: Outcome.UNAVAILABLE,
    E.SIGN_IN_REQUIRED: Outcome.SIGN_IN,
    E.RATE_LIMITED: Outcome.RATE_LIMITED,
    E.RATE_LIMITED_SUBTITLES: Outcome.RATE_LIMITED,
    E.TRANSIENT_NETWORK: Outcome.NETWORK,
}
_TRANSIENT_WAITS = (8.0, 20.0, 45.0)


def _final(c: E, do: tuple[str, ...] = ()) -> Step:
    """The class's last word, when no recovery is left or the budget is spent."""
    if c in _ESCALATES:
        return Step(StepKind.ESCALATE, do=do)
    return Step(StepKind.STOP, do=do, outcome=_OUTCOME.get(c, Outcome.FAILED))


def _retry(c: E, st: LadderState, wait_s: float = 0.0, do: tuple[str, ...] = ()) -> Step:
    if not st.budget.allows(wait_s):
        return _final(c, tuple(d for d in do if d == MANUAL_MERGE))
    return Step(StepKind.RETRY, wait_s=wait_s, do=do)


def _next_format(c: E, st: LadderState, do: tuple[str, ...] = ()) -> Step:
    if st.formats_left <= 0 or not st.budget.allows():
        return _final(c, do)
    return Step(StepKind.NEXT_FORMAT, do=do)


def decide(c: E, st: LadderState) -> Step:
    """The one step to take after an attempt failed with class `c`."""
    n = st.seen.get(c, 0)   # earlier failures of this class in this download

    if c is E.COOKIE_SOURCE:
        if st.has_cookies:
            return _retry(c, st, do=(DROP_COOKIES,))
        return _final(c)

    if c is E.DISK_FULL:
        return _final(c)

    if c is E.MERGE_RENAME:
        if n < 2:
            return _retry(c, st, wait_s=(8.0, 20.0)[n], do=(MANUAL_MERGE,))
        return _next_format(c, st, do=(MANUAL_MERGE,))

    if c is E.POSTPROCESSING and st.extras_attached:
        return _retry(c, st, do=(DROP_EXTRAS,))

    if c is E.OUTPUT_INVALID:
        return _next_format(c, st)

    if c in (E.AGE_GATE, E.SIGN_IN_REQUIRED):
        if st.can_refresh_cookies and REFRESH_COOKIES not in st.done:
            return _retry(c, st, wait_s=3.0, do=(REFRESH_COOKIES,))
        return _final(c)

    if c is E.RATE_LIMITED_SUBTITLES and st.subtitles_on:
        return _retry(c, st, do=(DROP_SUBTITLES,))
    if c in (E.RATE_LIMITED, E.RATE_LIMITED_SUBTITLES, E.UNAVAILABLE):
        return _final(c)

    if c is E.HTTP_403:
        # Stale cookies first; then NO cookies (on YouTube any cookie rules out
        # the token-free player clients, see cookies.py); then one plain wait.
        if st.has_cookies and st.can_refresh_cookies and REFRESH_COOKIES not in st.done:
            return _retry(c, st, do=(REFRESH_COOKIES,))
        if st.has_cookies:
            return _retry(c, st, do=(DROP_COOKIES,))
        if MARK_WAITED_403 not in st.done:
            return _retry(c, st, wait_s=8.0, do=(MARK_WAITED_403,))
        # Still refused: the refusal can be of the FORMAT, not the video. YouTube answers 403 on a video's separate
        # https streams while the same video's single HLS stream (the chain's last rung, "best") downloads at once
        # (reproduced 2026-11-20, youtube.com/shorts/CLco-qq94Lo: rungs 1 and 2 → 403, rung 3 → 1080p in 10 s).
        return _next_format(c, st)

    if c is E.ANTI_BOT_CHALLENGE:
        if n == 0:
            return _retry(c, st, wait_s=8.0)
        return _final(c)

    if c in (E.HTTP_404, E.UNSUPPORTED_URL):
        return _final(c)

    if c is E.NO_FORMATS:
        return _next_format(c, st)

    if c is E.TRANSIENT_NETWORK:
        # Same format: a different format string doesn't fix a network.
        if n < len(_TRANSIENT_WAITS):
            return _retry(c, st, wait_s=_TRANSIENT_WAITS[n])
        return _final(c)

    # UNKNOWN (and POSTPROCESSING with nothing left to drop): one wait and
    # retry per format, then down the chain. Never minutes on one error.
    if st.unknown_on_format == 0:
        return _retry(E.UNKNOWN, st, wait_s=8.0)
    return _next_format(E.UNKNOWN, st)


class ProbeVerdict(str, Enum):
    """What a failed metadata probe means for the download that follows."""
    RETRY_WITHOUT_COOKIES = "retry_without_cookies"
    TRY_GENERIC = "try_generic"      # probe once more with the generic extractor, then escalate
    ESCALATE = "escalate"
    UNAVAILABLE = "unavailable"
    INCONCLUSIVE = "inconclusive"    # says nothing certain: let the download try


def probe_verdict(c: E) -> ProbeVerdict:
    """The probe never waits and never ends a download on a guess: only a
    certain answer (gone, or needs a browser) stops here."""
    if c is E.COOKIE_SOURCE:
        return ProbeVerdict.RETRY_WITHOUT_COOKIES
    if c is E.UNSUPPORTED_URL:
        return ProbeVerdict.TRY_GENERIC
    if c in (E.ANTI_BOT_CHALLENGE, E.HTTP_403, E.HTTP_404):
        return ProbeVerdict.ESCALATE
    if c is E.UNAVAILABLE:
        return ProbeVerdict.UNAVAILABLE
    return ProbeVerdict.INCONCLUSIVE
