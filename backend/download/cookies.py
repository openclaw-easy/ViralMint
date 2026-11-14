# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Browser cookies for downloads: one object, explicit states.

`CookieStore` answers the two questions the old code answered in six
functions and three cooldowns: what does this attempt get, and may we ask
the browser again?

    DISABLED     CANARY_NO_BROWSER_COOKIES=1            nothing, ever
    NO_BROWSER   none found (re-checked after 30 min)   a jar left on disk, else nothing
    UNREADABLE   the browser's cookies can't be read    a jar left on disk, else nothing
                 (30 min, then re-checked)
    JAR_FRESH    extracted under 30 min ago, 10+ cookies  the jar
    JAR_STALE    older, or too few cookies              one extraction, else the old jar
    NO_JAR       nothing on disk                        one extraction, else nothing

Rules that hold in every state:

  * The jar or nothing. An attempt is never handed a live
    `cookiesfrombrowser`: when the jar can't be made, a live read fails the
    same way, on every attempt (2026-11-12: 7+ minutes at 0%).
  * At most one extraction per 5 minutes, and one forced refresh per 5
    minutes. On macOS each extraction can raise a Keychain prompt outside
    the app; a site that keeps answering "sign in" must not raise one per
    download.
  * An extraction writes beside the jar and replaces it only on success, so
    a failed refresh never costs the cookies we had.
  * One extraction at a time: two downloads starting together share it.

On macOS the usual UNREADABLE cause is privacy protection: the packaged app
may not list ~/Library/Application Support/Google/Chrome, yt-dlp sees an
empty folder and says "could not find chrome cookies database" though the
file is there. Most videos download fine without cookies, so downloads go
on, and the person is told once (`take_notice`).
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from backend.download import cookie_jar
from backend.download.classify import ErrorClass, classify

logger = logging.getLogger(__name__)

JAR_MAX_AGE_S = 1800
MIN_USEFUL_COOKIES = 10
EXTRACT_COOLDOWN_S = 300
REFRESH_COOLDOWN_S = 300
NO_BROWSER_TTL_S = 1800
UNREADABLE_TTL_S = 1800

BROWSERS = ("chrome", "brave", "edge", "firefox")
_BROWSER_NAMES = {"chrome": "Chrome", "brave": "Brave", "edge": "Edge", "firefox": "Firefox"}
_MAC_APPS = {
    "chrome": "/Applications/Google Chrome.app",
    "brave": "/Applications/Brave Browser.app",
    "edge": "/Applications/Microsoft Edge.app",
    "firefox": "/Applications/Firefox.app",
}
_BINARIES = {"chrome": "google-chrome", "brave": "brave-browser", "edge": "microsoft-edge", "firefox": "firefox"}
# Profile roots yt-dlp searches on macOS; read only to word the reason.
_MAC_PROFILE_DIRS = {
    "chrome": "Google/Chrome",
    "brave": "BraveSoftware/Brave-Browser",
    "edge": "Microsoft Edge",
    "firefox": "Firefox",
}


class CookieState(str, Enum):
    DISABLED = "disabled"
    NO_BROWSER = "no_browser"
    UNREADABLE = "unreadable"
    JAR_FRESH = "jar_fresh"
    JAR_STALE = "jar_stale"
    NO_JAR = "no_jar"


@dataclass(frozen=True)
class CookieStatus:
    state: CookieState
    browser: str | None = None
    reason: str = ""                 # for UNREADABLE: "no_access" | "not_found" | "error"
    jar_age_s: float | None = None
    cookies: int = 0


def detect_browser() -> str | None:
    """The first installed browser we can take cookies from."""
    if platform.system() == "Darwin":
        return next((b for b in BROWSERS if Path(_MAC_APPS[b]).exists()), None)
    return next((b for b in BROWSERS if shutil.which(_BINARIES[b]) or shutil.which(b)), None)


def diagnose_access(browser: str) -> str:
    """Why can't `browser`'s cookies be read: "no_access", "not_found" or "error"."""
    if platform.system() != "Darwin" or browser not in _MAC_PROFILE_DIRS:
        return "error"
    root = Path.home() / "Library" / "Application Support" / _MAC_PROFILE_DIRS[browser]
    try:
        entries = os.listdir(root)
    except PermissionError:
        return "no_access"
    except FileNotFoundError:
        return "not_found"
    except OSError:
        return "error"
    # The root may list while a profile inside does not: privacy rules apply per folder.
    for entry in entries:
        if entry == "Default" or entry.startswith("Profile"):
            try:
                os.listdir(root / entry)
            except PermissionError:
                return "no_access"
            except OSError:
                pass
    return "error"


def extract_from_browser(browser: str, dest: Path) -> None:
    """Write `browser`'s cookies to `dest` as a Netscape jar. Raises on failure.

    yt-dlp's own reader, with no network request (the old code ran a metadata
    request against YouTube only to make yt-dlp write the jar on exit).
    """
    from yt_dlp.cookies import extract_cookies_from_browser
    jar = extract_cookies_from_browser(browser)
    jar.save(str(dest), ignore_discard=True, ignore_expires=True)


class CookieStore:
    def __init__(
        self,
        jar_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        extract: Callable[[str, Path], None] = extract_from_browser,
        detect: Callable[[], str | None] = detect_browser,
        diagnose: Callable[[str], str] = diagnose_access,
        disabled: Callable[[], bool] = lambda: os.getenv("CANARY_NO_BROWSER_COOKIES") == "1",
    ):
        self.jar_path = Path(jar_path)
        self._clock, self._extract, self._detect, self._diagnose = clock, extract, detect, diagnose
        self._disabled = disabled
        self._lock = threading.Lock()                 # one extraction at a time
        self._browser: str | None = None
        self._no_browser_at: float | None = None
        self._unreadable_at: float | None = None
        self._unreadable_browser = ""
        self._unreadable_reason = ""
        self._noticed = False
        self._last_extract_try: float | None = None
        self._last_refresh: float | None = None
        self._cleaned_mtime: float | None = None      # the jar's mtime when we last sanitized it

    # ── What is true right now ───────────────────────────────────────────────

    def status(self) -> CookieStatus:
        """The current state. Reads the jar file; never reads the browser's cookies."""
        if self._disabled():
            return CookieStatus(CookieState.DISABLED)
        age, count = self._jar()
        if self._is_unreadable():
            return CookieStatus(CookieState.UNREADABLE, self._unreadable_browser,
                                self._unreadable_reason, age, count)
        browser = self._find_browser()
        if not browser:
            return CookieStatus(CookieState.NO_BROWSER, None, "", age, count)
        if age is None or count == 0:
            return CookieStatus(CookieState.NO_JAR, browser)
        fresh = age < JAR_MAX_AGE_S and count >= MIN_USEFUL_COOKIES
        return CookieStatus(CookieState.JAR_FRESH if fresh else CookieState.JAR_STALE, browser, "", age, count)

    def will_prompt(self) -> bool:
        """Would `jar_for_attempt` read the browser now (a Keychain prompt on macOS)?"""
        return self.status().state in (CookieState.JAR_STALE, CookieState.NO_JAR) and self._may_extract()

    def refresh_allowed(self) -> bool:
        """May a recovery (sign-in wall, 403) force a new extraction now?"""
        if self.status().state not in (CookieState.JAR_FRESH, CookieState.JAR_STALE, CookieState.NO_JAR):
            return False
        return self._last_refresh is None or self._clock() - self._last_refresh >= REFRESH_COOLDOWN_S

    # ── What an attempt gets ─────────────────────────────────────────────────

    def jar_for_attempt(self) -> Path | None:
        """The jar this attempt should use, extracting it once if it is stale
        or missing. None means: go without cookies."""
        state = self.status().state
        if state is CookieState.DISABLED:
            return None
        if state in (CookieState.JAR_STALE, CookieState.NO_JAR):
            with self._lock:
                # Another download may have extracted while we waited.
                if self.status().state in (CookieState.JAR_STALE, CookieState.NO_JAR) and self._may_extract():
                    self._extract_into_jar()
        return self._usable_jar()

    def refresh(self) -> Path | None:
        """Force one new extraction (a recovery step), then return the jar an
        attempt should use. Inside the cooldown, or when the browser can't be
        read, it returns the jar we have, untouched."""
        if self.refresh_allowed():
            with self._lock:
                self._last_refresh = self._clock()
                self._extract_into_jar()
        state = self.status().state
        if state is CookieState.DISABLED:
            return None
        return self._usable_jar()

    # ── The browser can't be read ────────────────────────────────────────────

    def mark_unreadable(self, error: object, browser: str | None = None) -> None:
        """A cookie read failed for a local reason. Stop asking for 30 minutes."""
        starting = not self._is_unreadable()
        self._unreadable_at = self._clock()
        if starting:
            self._unreadable_browser = browser or self._find_browser() or ""
            self._unreadable_reason = self._diagnose(self._unreadable_browser) if self._unreadable_browser else "error"
            self._noticed = False
            logger.warning(
                "[COOKIES_UNREADABLE] browser=%s reason=%s — downloads continue without browser cookies for %d min: %s",
                self._unreadable_browser, self._unreadable_reason, UNREADABLE_TTL_S // 60, str(error)[:200],
            )

    def hint(self) -> str | None:
        """Words for the person while the browser can't be read, else None."""
        if not self._is_unreadable():
            return None
        name = _BROWSER_NAMES.get(self._unreadable_browser) or "your browser"
        if self._unreadable_reason == "no_access":
            return (
                f"macOS is blocking ViralMint from reading {name}'s cookies, so downloads run without "
                f"your sign-in. Most videos don't need it. For ones that do (age-restricted, members-only, "
                f"bot checks): System Settings → Privacy & Security → Full Disk Access → turn on "
                f"ViralMint, then quit and reopen ViralMint."
            )
        return (
            f"ViralMint couldn't read {name}'s cookies, so downloads run without your sign-in. "
            f"Most videos don't need it; if one asks you to sign in, open {name} once, then retry."
        )

    def take_notice(self) -> str | None:
        """The hint, once per unreadable episode: for the one warning the person gets."""
        if self._noticed:
            return None
        hint = self.hint()
        if hint:
            self._noticed = True
        return hint

    # ── Internals ────────────────────────────────────────────────────────────

    def _is_unreadable(self) -> bool:
        return self._unreadable_at is not None and self._clock() - self._unreadable_at < UNREADABLE_TTL_S

    def _find_browser(self) -> str | None:
        if self._browser:
            return self._browser          # a found browser doesn't move
        now = self._clock()
        if self._no_browser_at is not None and now - self._no_browser_at < NO_BROWSER_TTL_S:
            return None
        self._browser = self._detect()
        self._no_browser_at = None if self._browser else now
        if self._browser:
            logger.info("Browser for cookies: %s", self._browser)
        else:
            logger.warning("No browser found for cookies — sign-in-walled downloads may fail "
                           "(checked again in %d min)", NO_BROWSER_TTL_S // 60)
        return self._browser

    def _may_extract(self) -> bool:
        return self._last_extract_try is None or self._clock() - self._last_extract_try >= EXTRACT_COOLDOWN_S

    def _jar(self) -> tuple[float | None, int]:
        """(age in seconds, cookie count) of the jar on disk; (None, 0) when there is none."""
        try:
            mtime = self.jar_path.stat().st_mtime
        except OSError:
            return None, 0
        if self._cleaned_mtime != mtime:
            # First look at this file (a jar from an earlier run, or just
            # extracted): clean it before anything counts or uses it.
            cookie_jar.sanitize(self.jar_path)
            try:
                mtime = self.jar_path.stat().st_mtime
            except OSError:
                return None, 0
            self._cleaned_mtime = mtime
        return max(0.0, self._clock() - mtime), cookie_jar.count_cookies(self.jar_path)

    def _usable_jar(self) -> Path | None:
        _, count = self._jar()
        return self.jar_path if count > 0 else None

    def _extract_into_jar(self) -> None:
        """One extraction (caller holds the lock). The jar is replaced only on success."""
        browser = self._find_browser()
        if not browser or self._is_unreadable():
            return
        self._last_extract_try = self._clock()        # before the read: a crash still spends the budget
        tmp = self.jar_path.with_suffix(self.jar_path.suffix + ".new")
        try:
            self.jar_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("Reading cookies from %s (a Keychain prompt may appear)", browser)
            self._extract(browser, tmp)
            cookie_jar.sanitize(tmp)
            if cookie_jar.count_cookies(tmp) == 0:
                raise OSError("the browser returned no cookies")
            cookie_jar.harden_permissions(tmp)
            now = self._clock()
            os.utime(tmp, (now, now))                 # the jar's age is this store's clock
            tmp.replace(self.jar_path)
            self._cleaned_mtime = self.jar_path.stat().st_mtime
            logger.info("Browser cookies saved (%d cookies)", cookie_jar.count_cookies(self.jar_path))
        except Exception as e:  # noqa: BLE001 — any failure means "go on with what we have"
            logger.warning("Couldn't read cookies from %s: %s", browser, str(e)[:200])
            if classify(e) is ErrorClass.COOKIE_SOURCE:
                self.mark_unreadable(e, browser)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _adopt_legacy_jar(legacy: Path, jar: Path) -> None:
    """Older builds kept the jar as `yt_cookies.txt`. Move it to the new name
    once, so an upgrade neither asks the browser again nor leaves a copy of
    the person's sessions behind under a name nothing reads any more."""
    try:
        if not legacy.exists():
            return
        if jar.exists():
            legacy.unlink()
        else:
            legacy.replace(jar)
            cookie_jar.harden_permissions(jar)
    except OSError as e:
        logger.debug("Couldn't move the old cookie jar (non-fatal): %s", e)


_store: CookieStore | None = None
_store_lock = threading.Lock()


def store() -> CookieStore:
    """The process's one CookieStore (the jar lives in the app's temp folder)."""
    global _store
    with _store_lock:
        if _store is None:
            from backend.config import settings
            jar = settings.TMP_DIR / "browser_cookies.txt"
            _adopt_legacy_jar(settings.TMP_DIR / "yt_cookies.txt", jar)
            _store = CookieStore(jar)
        return _store
