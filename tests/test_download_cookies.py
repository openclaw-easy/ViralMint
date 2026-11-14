# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""CookieStore: every state, every cooldown, with a fake clock and a fake
browser. No real browser, no Keychain, no sleeping, no patched globals."""
import os
import threading

import pytest

from backend.download import cookie_jar
from backend.download.cookies import (
    EXTRACT_COOLDOWN_S, JAR_MAX_AGE_S, NO_BROWSER_TTL_S, REFRESH_COOLDOWN_S, UNREADABLE_TTL_S,
    CookieState, CookieStore,
)

T0 = 1_800_000_000.0
CHROME_EPOCH_EXPIRY = "13453825375953964"      # microseconds since 1601: a real cookie, wrong unit


def jar_text(n: int, expiry: str = "1999999999") -> str:
    lines = ["# Netscape HTTP Cookie File", ""]
    lines += [f".example{i}.com\tTRUE\t/\tTRUE\t{expiry}\tname{i}\tvalue{i}" for i in range(n)]
    return "\n".join(lines) + "\n"


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Browser:
    """Stands in for the real cookie read."""
    def __init__(self, n=25, error=None):
        self.n, self.error, self.calls = n, error, 0

    def __call__(self, browser, dest):
        self.calls += 1
        if self.error:
            raise self.error
        dest.write_text(jar_text(self.n))


COOKIE_DB_ERROR = Exception('could not find chrome cookies database in "/Users/x/Library/Application Support/Google/Chrome"')


def make(tmp_path, *, browser=None, detect="chrome", diagnose="no_access", disabled=False):
    clock = Clock()
    browser = browser or Browser()
    flags = {"disabled": disabled, "detect": detect}
    s = CookieStore(
        tmp_path / "browser_cookies.txt", clock=clock, extract=browser,
        detect=lambda: flags["detect"], diagnose=lambda b: diagnose,
        disabled=lambda: flags["disabled"],
    )
    return s, clock, browser, flags


def put_jar(s, clock, n=25, age=0.0, expiry="1999999999"):
    s.jar_path.write_text(jar_text(n, expiry))
    os.utime(s.jar_path, (clock.now - age, clock.now - age))


# ── The state table ──────────────────────────────────────────────────────────

def test_no_jar_extracts_once_and_then_is_fresh(tmp_path):
    s, clock, browser, _ = make(tmp_path)
    assert s.status().state is CookieState.NO_JAR and s.will_prompt()
    assert s.jar_for_attempt() == s.jar_path
    assert browser.calls == 1
    st = s.status()
    assert (st.state, st.browser, st.cookies) == (CookieState.JAR_FRESH, "chrome", 25)
    assert not s.will_prompt()
    assert s.jar_for_attempt() == s.jar_path and browser.calls == 1      # fresh: no second read


def test_fresh_jar_from_an_earlier_run_is_used_without_reading_the_browser(tmp_path):
    s, clock, browser, _ = make(tmp_path)
    put_jar(s, clock, age=600)
    assert s.status().state is CookieState.JAR_FRESH
    assert s.jar_for_attempt() == s.jar_path and browser.calls == 0


def test_stale_jar_is_re_extracted(tmp_path):
    s, clock, browser, _ = make(tmp_path)
    put_jar(s, clock, age=JAR_MAX_AGE_S + 1)
    assert s.status().state is CookieState.JAR_STALE
    assert s.jar_for_attempt() == s.jar_path and browser.calls == 1
    assert s.status().state is CookieState.JAR_FRESH


def test_a_jar_with_too_few_cookies_counts_as_stale(tmp_path):
    s, clock, browser, _ = make(tmp_path)
    put_jar(s, clock, n=3, age=10)
    assert s.status().state is CookieState.JAR_STALE
    s.jar_for_attempt()
    assert browser.calls == 1 and s.status().cookies == 25


def test_fresh_jar_goes_stale_after_30_minutes(tmp_path):
    s, clock, browser, _ = make(tmp_path)
    s.jar_for_attempt()
    clock.advance(JAR_MAX_AGE_S + 1)
    assert s.status().state is CookieState.JAR_STALE
    s.jar_for_attempt()
    assert browser.calls == 2


def test_disabled_gives_nothing_and_never_looks_for_a_browser(tmp_path):
    s, clock, browser, _ = make(tmp_path, disabled=True)
    put_jar(s, clock)
    assert s.status().state is CookieState.DISABLED
    assert s.jar_for_attempt() is None and s.refresh() is None
    assert not s.refresh_allowed() and not s.will_prompt() and browser.calls == 0



def test_no_browser_uses_a_leftover_jar_and_rechecks_after_30_minutes(tmp_path):
    s, clock, browser, flags = make(tmp_path, detect=None)
    assert s.status().state is CookieState.NO_BROWSER
    assert s.jar_for_attempt() is None and not s.refresh_allowed() and browser.calls == 0
    put_jar(s, clock, age=JAR_MAX_AGE_S * 3)
    assert s.jar_for_attempt() == s.jar_path          # old cookies beat none
    flags["detect"] = "brave"                         # installed after the app started
    assert s.status().state is CookieState.NO_BROWSER  # still inside the 30 minutes
    clock.advance(NO_BROWSER_TTL_S + 1)
    assert s.status().browser == "brave"


# ── The browser can't be read (2026-11-12) ───────────────────────────────────

def test_unreadable_browser_is_asked_once_not_on_every_download(tmp_path):
    s, clock, browser, _ = make(tmp_path, browser=Browser(error=COOKIE_DB_ERROR))
    assert s.jar_for_attempt() is None
    st = s.status()
    assert (st.state, st.browser, st.reason) == (CookieState.UNREADABLE, "chrome", "no_access")
    for _ in range(20):
        clock.advance(30)
        assert s.jar_for_attempt() is None
        assert s.refresh() is None
    assert browser.calls == 1
    assert not s.refresh_allowed() and not s.will_prompt()


def test_unreadable_heals_after_30_minutes(tmp_path):
    browser = Browser(error=COOKIE_DB_ERROR)
    s, clock, _, _ = make(tmp_path, browser=browser)
    s.jar_for_attempt()
    browser.error = None                              # access granted, app reopened
    clock.advance(UNREADABLE_TTL_S + 1)
    assert s.status().state is CookieState.NO_JAR
    assert s.jar_for_attempt() == s.jar_path


def test_unreadable_keeps_and_uses_the_jar_we_already_had(tmp_path):
    s, clock, browser, _ = make(tmp_path)
    put_jar(s, clock, age=JAR_MAX_AGE_S + 5)
    s.mark_unreadable(COOKIE_DB_ERROR)
    assert s.jar_for_attempt() == s.jar_path and s.refresh() == s.jar_path
    assert browser.calls == 0 and s.jar_path.exists()


def test_mark_unreadable_from_a_download_attempt_extends_the_episode(tmp_path):
    s, clock, _, _ = make(tmp_path)
    s.mark_unreadable(COOKIE_DB_ERROR)
    clock.advance(UNREADABLE_TTL_S - 10)
    s.mark_unreadable(COOKIE_DB_ERROR)                # still failing: same episode, clock restarts
    clock.advance(UNREADABLE_TTL_S - 10)
    assert s.status().state is CookieState.UNREADABLE


def test_the_person_is_told_once_per_episode_in_the_right_words(tmp_path):
    s, clock, _, _ = make(tmp_path, diagnose="no_access")
    assert s.hint() is None and s.take_notice() is None
    s.mark_unreadable(COOKIE_DB_ERROR)
    notice = s.take_notice()
    assert "Full Disk Access" in notice and "Chrome" in notice
    assert s.take_notice() is None and s.hint() == notice
    clock.advance(UNREADABLE_TTL_S + 1)
    assert s.hint() is None
    s.mark_unreadable(COOKIE_DB_ERROR)                # a new episode gets its own notice
    assert s.take_notice() is not None


def test_a_missing_profile_is_worded_without_the_privacy_steps(tmp_path):
    s, _, _, _ = make(tmp_path, diagnose="not_found")
    s.mark_unreadable(COOKIE_DB_ERROR)
    assert "Full Disk Access" not in s.hint() and "open Chrome once" in s.hint()


# ── Failures that are not "unreadable" ───────────────────────────────────────

def test_a_denied_keychain_prompt_is_not_retried_for_5_minutes(tmp_path):
    browser = Browser(error=Exception("user denied access to the keychain"))
    s, clock, _, _ = make(tmp_path, browser=browser)
    assert s.jar_for_attempt() is None
    assert s.status().state is CookieState.NO_JAR      # not UNREADABLE: it may work next time
    clock.advance(EXTRACT_COOLDOWN_S - 1)
    assert s.jar_for_attempt() is None and browser.calls == 1 and not s.will_prompt()
    clock.advance(2)
    browser.error = None
    assert s.jar_for_attempt() == s.jar_path and browser.calls == 2


def test_a_failed_extraction_never_costs_the_jar_we_had(tmp_path):
    browser = Browser(error=RuntimeError("boom"))
    s, clock, _, _ = make(tmp_path, browser=browser)
    put_jar(s, clock, age=JAR_MAX_AGE_S + 5)
    before = s.jar_path.read_text()
    assert s.jar_for_attempt() == s.jar_path
    assert s.jar_path.read_text() == before
    assert not list(tmp_path.glob("*.new"))


def test_an_empty_read_from_the_browser_keeps_the_old_jar(tmp_path):
    s, clock, browser, _ = make(tmp_path, browser=Browser(n=0))
    put_jar(s, clock, age=JAR_MAX_AGE_S + 5)
    assert s.jar_for_attempt() == s.jar_path and s.status().cookies == 25


# ── Forced refresh (sign-in wall, 403) ───────────────────────────────────────

def test_refresh_reads_the_browser_again_at_most_once_per_5_minutes(tmp_path):
    s, clock, browser, _ = make(tmp_path)
    s.jar_for_attempt()
    assert s.refresh_allowed()
    assert s.refresh() == s.jar_path and browser.calls == 2
    assert not s.refresh_allowed()
    assert s.refresh() == s.jar_path and browser.calls == 2            # inside the cooldown: same jar
    clock.advance(REFRESH_COOLDOWN_S + 1)
    assert s.refresh_allowed()
    s.refresh()
    assert browser.calls == 3


def test_refresh_is_not_blocked_by_the_extraction_cooldown(tmp_path):
    # A sign-in wall seconds after the first extraction still gets its one refresh.
    s, clock, browser, _ = make(tmp_path)
    s.jar_for_attempt()
    clock.advance(5)
    s.refresh()
    assert browser.calls == 2


# ── Concurrency ──────────────────────────────────────────────────────────────

def test_two_downloads_starting_together_read_the_browser_once(tmp_path):
    s, clock, _, _ = make(tmp_path)
    gate, calls = threading.Event(), []

    def slow_extract(browser, dest):
        calls.append(browser)
        gate.wait(5)
        dest.write_text(jar_text(25))

    s._extract = slow_extract
    results = []
    threads = [threading.Thread(target=lambda: results.append(s.jar_for_attempt())) for _ in range(6)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(10)
    assert len(calls) == 1
    assert results == [s.jar_path] * 6


# ── The jar file ─────────────────────────────────────────────────────────────

def test_chrome_epoch_expiries_are_converted_not_dropped_and_age_is_kept(tmp_path):
    s, clock, browser, _ = make(tmp_path)
    put_jar(s, clock, n=25, age=600, expiry=CHROME_EPOCH_EXPIRY)
    st = s.status()
    assert (st.state, st.cookies) == (CookieState.JAR_FRESH, 25)
    assert 599 <= st.jar_age_s <= 601                  # cleaning it did not make it look new
    assert CHROME_EPOCH_EXPIRY not in s.jar_path.read_text()
    assert browser.calls == 0


def test_unrecoverable_lines_are_dropped_and_httponly_lines_kept(tmp_path):
    p = tmp_path / "jar.txt"
    p.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.a.com\tTRUE\t/\tTRUE\t1999999999\tk\tv\n"
        ".b.com\tTRUE\t/\tTRUE\tnot-a-number\tk\tv\n"
        ".c.com\tTRUE\t/\tTRUE\t0\tk\tv\n"
        "garbage line\n"
    )
    assert cookie_jar.sanitize(p) == 2
    assert cookie_jar.count_cookies(p) == 2
    assert "#HttpOnly_.a.com" in p.read_text() and ".b.com" not in p.read_text()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_jar_is_private(tmp_path):
    s, _, _, _ = make(tmp_path)
    s.jar_for_attempt()
    assert (s.jar_path.stat().st_mode & 0o777) == 0o600


def test_a_missing_or_unreadable_jar_file_is_no_jar_not_a_crash(tmp_path):
    s, _, _, _ = make(tmp_path, detect=None)
    assert s.status().state is CookieState.NO_BROWSER and s.status().cookies == 0
    s.jar_path.mkdir()                                 # a directory where the jar should be
    assert s.jar_for_attempt() is None


def test_an_older_builds_jar_is_adopted_once_and_never_left_behind(tmp_path):
    from backend.download.cookies import _adopt_legacy_jar
    legacy, jar = tmp_path / "yt_cookies.txt", tmp_path / "browser_cookies.txt"
    legacy.write_text("old")
    _adopt_legacy_jar(legacy, jar)
    assert jar.read_text() == "old" and not legacy.exists()
    legacy.write_text("stale copy")
    _adopt_legacy_jar(legacy, jar)                    # a jar already exists: the old one just goes
    assert jar.read_text() == "old" and not legacy.exists()
    _adopt_legacy_jar(legacy, jar)                    # nothing to do
    assert jar.read_text() == "old"
