# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""policy.decide(): each row of the table, and the guarantee the table exists
for: whatever errors a download meets, in whatever order, it ends inside its
budget and never tries the same recovery twice."""
import itertools
import random

import pytest

from backend.download.classify import ErrorClass as E
from backend.download.policy import (
    DROP_COOKIES, DROP_EXTRAS, DROP_SUBTITLES, MANUAL_MERGE, MARK_WAITED_403, REFRESH_COOKIES,
    Budget, LadderState, Outcome, ProbeVerdict, Step, StepKind, decide, probe_verdict,
)


def video(**kw) -> LadderState:
    base = dict(formats_left=2, has_cookies=True, can_refresh_cookies=True,
                subtitles_on=True, extras_attached=False)
    base.update(kw)
    return LadderState(budget=Budget.video(), **base)


def run(st: LadderState, errors) -> list[Step]:
    """Feed errors to the table the way the engine will; the last error repeats."""
    steps, it, last = [], iter(errors), None
    for _ in range(100):
        last = next(it, last)
        step = decide(last, st)
        st.record(last, step)
        steps.append(step)
        if step.kind in (StepKind.STOP, StepKind.ESCALATE):
            return steps
    raise AssertionError(f"ladder did not end: {errors}")


def kinds(steps):
    return [(s.kind.value, s.wait_s, s.do) for s in steps]


# ── Rows ─────────────────────────────────────────────────────────────────────

def test_cookie_source_drops_cookies_once_then_says_so():
    steps = run(video(), [E.COOKIE_SOURCE])
    assert kinds(steps) == [("retry", 0.0, (DROP_COOKIES,)), ("stop", 0.0, ())]
    assert steps[-1].outcome is Outcome.COOKIES_UNREADABLE


def test_the_2026_11_12_incident_takes_no_waiting_at_all():
    # Unreadable cookies on every attempt used to wait 8+20+45+60 s on each
    # of three formats, then run the whole ladder again.
    st = video()
    run(st, [E.COOKIE_SOURCE])
    assert st.budget.waited_s == 0 and st.budget.attempts == 2


def test_merge_failure_tries_the_manual_merge_every_time():
    steps = run(video(formats_left=1), [E.MERGE_RENAME])
    assert kinds(steps) == [
        ("retry", 8.0, (MANUAL_MERGE,)), ("retry", 20.0, (MANUAL_MERGE,)),
        ("next_format", 0.0, (MANUAL_MERGE,)), ("stop", 0.0, (MANUAL_MERGE,)),
    ]
    assert steps[-1].outcome is Outcome.FAILED


def test_optional_extras_are_dropped_and_the_video_kept():
    steps = run(video(extras_attached=True), [E.POSTPROCESSING])
    assert kinds(steps)[0] == ("retry", 0.0, (DROP_EXTRAS,))
    # Still failing with nothing left to drop: treated as unknown, and it ends.
    assert steps[-1].kind is StepKind.STOP


def test_invalid_output_goes_down_the_chain_then_fails():
    steps = run(video(formats_left=2), [E.OUTPUT_INVALID])
    assert [s.kind for s in steps] == [StepKind.NEXT_FORMAT, StepKind.NEXT_FORMAT, StepKind.STOP]


@pytest.mark.parametrize("cls,outcome", [(E.SIGN_IN_REQUIRED, Outcome.SIGN_IN), (E.AGE_GATE, Outcome.UNAVAILABLE)])
def test_sign_in_wall_gets_one_cookie_refresh(cls, outcome):
    steps = run(video(), [cls])
    assert kinds(steps) == [("retry", 3.0, (REFRESH_COOKIES,)), ("stop", 0.0, ())]
    assert steps[-1].outcome is outcome


def test_sign_in_wall_stops_at_once_when_cookies_cannot_be_refreshed():
    steps = run(video(can_refresh_cookies=False), [E.SIGN_IN_REQUIRED])
    assert kinds(steps) == [("stop", 0.0, ())]


def test_subtitle_429_drops_subtitles_and_a_second_429_stops():
    steps = run(video(), [E.RATE_LIMITED_SUBTITLES])
    assert kinds(steps) == [("retry", 0.0, (DROP_SUBTITLES,)), ("stop", 0.0, ())]
    assert steps[-1].outcome is Outcome.RATE_LIMITED


@pytest.mark.parametrize("cls,outcome", [(E.RATE_LIMITED, Outcome.RATE_LIMITED), (E.UNAVAILABLE, Outcome.UNAVAILABLE)])
def test_certain_answers_stop_at_once(cls, outcome):
    steps = run(video(), [cls])
    assert len(steps) == 1 and steps[0].outcome is outcome


def test_403_refreshes_then_drops_cookies_then_waits_once_then_tries_each_format_then_escalates():
    steps = run(video(), [E.HTTP_403])
    assert kinds(steps) == [
        ("retry", 0.0, (REFRESH_COOKIES,)),
        ("retry", 0.0, (DROP_COOKIES,)),
        ("retry", 8.0, (MARK_WAITED_403,)),
        ("next_format", 0.0, ()),
        ("next_format", 0.0, ()),
        ("escalate", 0.0, ()),
    ]


def test_403_without_cookies_waits_once_then_tries_each_format_then_escalates():
    steps = run(video(has_cookies=False, can_refresh_cookies=False), [E.HTTP_403])
    assert [s.kind for s in steps] == [StepKind.RETRY, StepKind.NEXT_FORMAT, StepKind.NEXT_FORMAT, StepKind.ESCALATE]


def test_a_403_on_a_videos_separate_streams_reaches_the_single_stream_rung():
    """youtube.com/shorts/CLco-qq94Lo, 2026-11-20: rungs 1 and 2 (separate https streams) answer 403, rung 3 ("best", one HLS stream) downloads."""
    st = video(has_cookies=False, can_refresh_cookies=False)
    taken = []
    for _ in range(3):      # the three failures before the last rung is reached
        step = decide(E.HTTP_403, st)
        st.record(E.HTTP_403, step)
        taken.append(step.kind)
    assert taken == [StepKind.RETRY, StepKind.NEXT_FORMAT, StepKind.NEXT_FORMAT] and st.formats_left == 0 and st.budget.attempts == 4


def test_403_with_no_format_left_escalates_as_before():
    steps = run(video(has_cookies=False, can_refresh_cookies=False, formats_left=0), [E.HTTP_403])
    assert [s.kind for s in steps] == [StepKind.RETRY, StepKind.ESCALATE]


def test_challenge_gets_one_retry_then_escalates():
    assert kinds(run(video(), [E.ANTI_BOT_CHALLENGE])) == [("retry", 8.0, ()), ("escalate", 0.0, ())]


@pytest.mark.parametrize("cls", [E.HTTP_404, E.UNSUPPORTED_URL])
def test_no_extractor_escalates_at_once(cls):
    assert kinds(run(video(), [cls])) == [("escalate", 0.0, ())]


def test_no_formats_walks_the_chain_then_escalates():
    steps = run(video(formats_left=2), [E.NO_FORMATS])
    assert [s.kind for s in steps] == [StepKind.NEXT_FORMAT, StepKind.NEXT_FORMAT, StepKind.ESCALATE]


def test_network_error_waits_8_20_45_on_the_same_format_then_stops():
    steps = run(video(), [E.TRANSIENT_NETWORK])
    assert kinds(steps) == [("retry", 8.0, ()), ("retry", 20.0, ()), ("retry", 45.0, ()), ("stop", 0.0, ())]
    assert steps[-1].outcome is Outcome.NETWORK


def test_unknown_gets_one_retry_per_format():
    steps = run(video(formats_left=2), [E.UNKNOWN])
    assert kinds(steps) == [
        ("retry", 8.0, ()), ("next_format", 0.0, ()),
        ("retry", 8.0, ()), ("next_format", 0.0, ()),
        ("retry", 8.0, ()), ("stop", 0.0, ()),
    ]
    assert steps[-1].outcome is Outcome.FAILED


def test_a_recovered_403_then_a_network_blip_still_downloads():
    # Mixed sequence: each class keeps its own count.
    st = video()
    assert decide(E.HTTP_403, st).do == (REFRESH_COOKIES,)
    st.record(E.HTTP_403, decide(E.HTTP_403, st))
    step = decide(E.TRANSIENT_NETWORK, st)
    assert (step.kind, step.wait_s) == (StepKind.RETRY, 8.0)


# ── Budget ───────────────────────────────────────────────────────────────────

def test_budget_ends_the_ladder_whatever_the_row_says():
    st = LadderState(budget=Budget(max_attempts=2, max_wait_s=120.0), formats_left=2)
    steps = run(st, [E.TRANSIENT_NETWORK])
    assert [s.kind for s in steps] == [StepKind.RETRY, StepKind.STOP]


def test_audio_budget_caps_waiting_at_60_seconds():
    st = LadderState(budget=Budget.audio())
    steps = run(st, [E.TRANSIENT_NETWORK])
    # 8 + 20 fit in 60 s; 45 more would not.
    assert [s.wait_s for s in steps] == [8.0, 20.0, 0.0]
    assert st.budget.waited_s == 28.0


def test_probe_budget_never_waits():
    for cls in E:
        st = LadderState(budget=Budget.probe(), has_cookies=True, can_refresh_cookies=True)
        steps = run(st, [cls])
        assert all(s.wait_s == 0 for s in steps), cls
        assert st.budget.waited_s == 0


def test_spent_budget_escalates_what_a_browser_might_still_get():
    spent = LadderState(budget=Budget(max_attempts=1, max_wait_s=0.0))
    assert decide(E.HTTP_403, spent).kind is StepKind.ESCALATE
    assert decide(E.NO_FORMATS, spent).kind is StepKind.ESCALATE
    assert decide(E.UNKNOWN, spent).kind is StepKind.STOP


# ── The guarantee ────────────────────────────────────────────────────────────

_STARTS = [
    dict(formats_left=2, has_cookies=True, can_refresh_cookies=True, subtitles_on=True, extras_attached=True),
    dict(formats_left=2, has_cookies=False, can_refresh_cookies=False, subtitles_on=True, extras_attached=False),
    dict(formats_left=0, has_cookies=True, can_refresh_cookies=False, subtitles_on=False, extras_attached=False),
]
_BUDGETS = [Budget.video, Budget.audio, Budget.probe]
_RECOVERIES = {DROP_COOKIES, REFRESH_COOKIES, DROP_SUBTITLES, DROP_EXTRAS, MARK_WAITED_403}


def _check(errors, start, make_budget):
    st = LadderState(budget=make_budget(), **start)
    steps = run(st, errors)                       # raises if it never ends
    b = st.budget
    assert b.attempts <= b.max_attempts, (errors, start)
    assert b.waited_s <= b.max_wait_s, (errors, start)
    used = [d for s in steps for d in s.do if d in _RECOVERIES]
    assert len(used) == len(set(used)), f"a recovery ran twice: {used} for {errors}"
    for s in steps:
        if s.kind is StepKind.RETRY:
            assert s.wait_s > 0 or s.do, f"a retry that changes nothing and waits for nothing: {errors}"
        if s.kind is StepKind.STOP:
            assert s.outcome is not None
    assert st.formats_left >= 0


def test_every_error_sequence_up_to_length_3_ends_inside_its_budget():
    for n in (1, 2, 3):
        for errors in itertools.product(list(E), repeat=n):
            for start in _STARTS:
                for make_budget in _BUDGETS:
                    _check(errors, start, make_budget)


def test_random_long_error_sequences_end_inside_their_budget():
    rng = random.Random(20261113)
    classes = list(E)
    for _ in range(20_000):
        errors = [rng.choice(classes) for _ in range(rng.randint(4, 16))]
        _check(errors, rng.choice(_STARTS), rng.choice(_BUDGETS))


# ── Probe ────────────────────────────────────────────────────────────────────

def test_probe_verdicts():
    assert probe_verdict(E.COOKIE_SOURCE) is ProbeVerdict.RETRY_WITHOUT_COOKIES
    assert probe_verdict(E.UNSUPPORTED_URL) is ProbeVerdict.TRY_GENERIC
    assert probe_verdict(E.UNAVAILABLE) is ProbeVerdict.UNAVAILABLE
    for cls in (E.ANTI_BOT_CHALLENGE, E.HTTP_403, E.HTTP_404):
        assert probe_verdict(cls) is ProbeVerdict.ESCALATE
    # A guess never ends a download at the probe.
    for cls in (E.AGE_GATE, E.SIGN_IN_REQUIRED, E.RATE_LIMITED, E.NO_FORMATS,
                E.TRANSIENT_NETWORK, E.UNKNOWN, E.MERGE_RENAME, E.POSTPROCESSING):
        assert probe_verdict(cls) is ProbeVerdict.INCONCLUSIVE
